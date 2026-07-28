"""Disk capacity, inode exhaustion, read-only mounts, and space hogs.

A full disk is the single most common cause of "my computer suddenly broke"
- it makes applications fail in confusing, unrelated-looking ways - so
these checks run in the quick profile.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from ..core.base import Check
from ..core.context import Context
from ..core.model import Finding
from ..core.probe import filesystems
from ..core.registry import register_check
from ..core.util import dir_size, human_bytes

CLEANUP_FIXES = ["clean.package-cache", "clean.user-cache", "clean.tmp", "clean.journal"]


@register_check
class DiskSpace(Check):
    id = "disk.space"
    name = "Disk space"
    description = "Warns when a filesystem is close to full."
    category = "disk"

    def run(self, ctx: Context) -> Iterable[Finding]:
        mounts = filesystems(ctx)
        if not mounts:
            yield self.unknown("Could not read filesystem usage")
            return

        config = ctx.config
        for filesystem in sorted(mounts, key=lambda item: -item.percent_used):
            # A full read-only filesystem is not a problem you can act on:
            # nothing can write to it, and nothing can be freed from it.
            if filesystem.read_only:
                continue
            used_percent = filesystem.percent_used
            detail = (
                f"{filesystem.mountpoint}  "
                f"{human_bytes(filesystem.used)} used of {human_bytes(filesystem.total)}  "
                f"({used_percent:.0f}%), {human_bytes(filesystem.free)} free"
            )
            evidence = {
                "device": filesystem.device,
                "fstype": filesystem.fstype,
                "total_bytes": filesystem.total,
                "free_bytes": filesystem.free,
                "percent_used": round(used_percent, 1),
            }

            starved = filesystem.free < config.disk_min_free_bytes
            if used_percent >= config.disk_critical_percent or (
                starved and used_percent >= config.disk_warn_percent
            ):
                yield self.critical(
                    f"Filesystem {filesystem.mountpoint} is {used_percent:.0f}% full",
                    detail=detail,
                    evidence=evidence,
                    fix_ids=CLEANUP_FIXES,
                    advice=(
                        "Free space now - below a few percent, writes start failing and "
                        "applications behave unpredictably."
                    ),
                )
            elif used_percent >= config.disk_warn_percent:
                yield self.warn(
                    f"Filesystem {filesystem.mountpoint} is {used_percent:.0f}% full",
                    detail=detail,
                    evidence=evidence,
                    fix_ids=CLEANUP_FIXES,
                    advice="Reclaim space before it becomes urgent.",
                )


@register_check
class DiskInodes(Check):
    id = "disk.inodes"
    name = "Inode usage"
    description = "Detects filesystems that will fail to create files despite free space."
    category = "disk"
    platforms = ("linux", "darwin")

    def run(self, ctx: Context) -> Iterable[Finding]:
        for filesystem in filesystems(ctx):
            if not filesystem.inodes_total:
                continue
            used_percent = filesystem.inodes_percent_used
            if used_percent < ctx.config.inode_warn_percent:
                continue

            detail = (
                f"{filesystem.mountpoint}  {filesystem.inodes_used:,} of "
                f"{filesystem.inodes_total:,} inodes used ({used_percent:.0f}%)"
            )
            evidence = {
                "inodes_total": filesystem.inodes_total,
                "inodes_used": filesystem.inodes_used,
                "percent_used": round(used_percent, 1),
            }
            advice = (
                "The disk has free space but is running out of inodes, usually from a "
                "directory holding a very large number of small files. Find it with: "
                f"du --inodes -x -d3 {filesystem.mountpoint} | sort -rn | head"
            )

            if used_percent >= ctx.config.inode_critical_percent:
                yield self.critical(
                    f"Inodes nearly exhausted on {filesystem.mountpoint} ({used_percent:.0f}%)",
                    detail=detail,
                    evidence=evidence,
                    advice=advice,
                    fix_ids=["clean.tmp", "clean.user-cache"],
                )
            else:
                yield self.warn(
                    f"High inode usage on {filesystem.mountpoint} ({used_percent:.0f}%)",
                    detail=detail,
                    evidence=evidence,
                    advice=advice,
                )


@register_check
class ReadOnlyMounts(Check):
    id = "disk.readonly"
    name = "Read-only filesystems"
    description = "Finds filesystems the kernel remounted read-only after I/O errors."
    category = "disk"
    platforms = ("linux", "darwin")

    #: Filesystems and locations that are read-only by design.
    EXPECTED_TYPES = frozenset({"squashfs", "iso9660", "cd9660", "erofs", "romfs"})
    EXPECTED_PREFIXES = ("/snap", "/nix/store", "/System", "/var/lib/snapd", "/opt/")

    def unavailable_reason(self, ctx: Context) -> str:
        reason = super().unavailable_reason(ctx)
        if reason:
            return reason
        if ctx.in_container:
            # Read-only bind mounts are how containers are built; the state
            # says nothing about the health of the underlying storage.
            return "read-only mounts are normal inside a container"
        return ""

    def run(self, ctx: Context) -> Iterable[Finding]:
        candidates = [
            filesystem
            for filesystem in filesystems(ctx)
            if filesystem.read_only
            and filesystem.fstype not in self.EXPECTED_TYPES
            and not filesystem.mountpoint.startswith(self.EXPECTED_PREFIXES)
        ]
        if not candidates:
            return

        errors = self._error_log(ctx)

        for filesystem in candidates:
            device = os.path.basename(filesystem.device or "")
            # The kernel logs a remount before it flips a filesystem to
            # read-only. With that evidence this is an emergency; without
            # it, the mount was almost certainly deliberate.
            forced = bool(device) and any(
                device in line or filesystem.mountpoint in line for line in errors
            )

            detail = f"device {filesystem.device or '?'}  fstype {filesystem.fstype or '?'}"
            evidence = {
                "mountpoint": filesystem.mountpoint,
                "device": filesystem.device,
                "kernel_errors": forced,
            }

            if forced:
                yield self.critical(
                    f"{filesystem.mountpoint} was forced read-only after I/O errors",
                    detail=detail + "\n" + "\n".join(errors[:3]),
                    evidence=evidence,
                    advice=(
                        "The kernel does this to stop further damage. Back up anything you "
                        "care about from this filesystem before rebooting or running fsck - "
                        "the drive may be failing."
                    ),
                )
            else:
                yield self.info(
                    f"{filesystem.mountpoint} is mounted read-only",
                    detail=detail + "\nNo matching I/O errors in the kernel log, so this "
                    "looks intentional.",
                    evidence=evidence,
                )

    def _error_log(self, ctx: Context) -> list[str]:
        """Kernel lines indicating a filesystem was forced read-only."""
        text = ""
        if ctx.has("journalctl"):
            result = ctx.run(["journalctl", "-k", "--no-pager", "--since", "-7 days"],
                             timeout=max(ctx.timeout, 20.0))
            if result.ok:
                text = result.stdout
        if not text and ctx.has("dmesg"):
            result = ctx.run(["dmesg"])
            if result.ok:
                text = result.stdout

        markers = (
            "Remounting filesystem read-only",
            "remounting filesystem read-only",
            "I/O error",
            "critical medium error",
            "failed command: WRITE",
        )
        return [line for line in text.splitlines() if any(m in line for m in markers)]


@register_check
class SpaceHogs(Check):
    id = "disk.hogs"
    name = "Largest directories in home"
    description = "Points at the directories using the most space in your home folder."
    category = "disk"
    profiles = ("full",)

    #: relative path -> (what it is, fix id that handles it or '').
    #: Directories with no fix get a manual suggestion instead, so the report
    #: never points at a repair that would not actually touch them.
    INTERESTING: dict[str, tuple[str, str]] = {
        ".cache": ("application caches", "clean.user-cache"),
        "Library/Caches": ("application caches", "clean.user-cache"),
        ".local/share/Trash": ("deleted files awaiting purge", "clean.trash"),
        ".Trash": ("deleted files awaiting purge", "clean.trash"),
        ".npm": ("npm package cache", "clean.package-cache"),
        ".cargo/registry": ("Rust crate sources; `cargo cache -a` prunes them", ""),
        ".rustup": ("Rust toolchains; `rustup toolchain list` shows what you have", ""),
        ".m2/repository": ("Maven artifacts; safe to delete, slow to re-download", ""),
        ".gradle/caches": ("Gradle build caches; safe to delete", ""),
        ".nvm": ("Node versions; `nvm ls` shows what you have", ""),
        "go/pkg": ("Go module cache; `go clean -modcache` clears it", ""),
        "Library/Developer/Xcode/DerivedData": ("Xcode build products; safe to delete", ""),
        ".docker": ("Docker data", "clean.docker"),
        "Downloads": ("your downloads folder - review before deleting", ""),
    }

    MIN_INTERESTING = 512 * 1024**2

    def run(self, ctx: Context) -> Iterable[Finding]:
        home = ctx.home
        if not home or not os.path.isdir(home):
            yield self.unknown("Home directory not found")
            return

        found: list[tuple[str, int, str, str]] = []
        for relative, (what, fix_id) in self.INTERESTING.items():
            path = os.path.join(home, relative)
            if not os.path.isdir(path):
                continue
            size = dir_size(path)
            if size > self.MIN_INTERESTING:
                found.append((path, size, what, fix_id))

        if not found:
            return

        found.sort(key=lambda item: -item[1])
        total = sum(size for _path, size, _what, _fix in found)

        detail = "\n".join(
            f"{human_bytes(size):>10}  {path.replace(home, '~', 1)}  -  {what}"
            for path, size, what, _fix in found
        )
        fix_ids = sorted({fix_id for _p, _s, _w, fix_id in found if fix_id})

        yield self.info(
            f"{human_bytes(total)} in caches and build artefacts under your home directory",
            detail=detail,
            evidence={
                "total_bytes": total,
                "paths": {path: size for path, size, _w, _f in found},
            },
            fix_ids=fix_ids,
            advice=(
                "These regenerate on demand. medic only removes the ones listed with a "
                "fix above; the rest are yours to clear if you want the space."
            ),
        )
