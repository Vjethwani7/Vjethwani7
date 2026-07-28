"""Fixes that reclaim disk space.

Every one of these is reversible in the sense that matters: the data
removed is regenerable cache, not user content. Nothing here touches
documents, and every delete goes through the guardrails in
:mod:`medic.core.safety`.
"""

from __future__ import annotations

import os

from ..core.base import Fix
from ..core.context import Context
from ..core.model import FixPlan, Risk
from ..core.probe import journal_bytes
from ..core.registry import register_fix
from ..core.safety import collect_stale, measure
from ..core.util import human_bytes, parse_size
from ._common import delete_action


@register_fix
class CleanUserCache(Fix):
    id = "clean.user-cache"
    name = "Remove stale application caches"
    description = (
        "Deletes cache directories in your home folder that have not been touched "
        "recently. Applications rebuild these on demand."
    )
    category = "disk"
    risk = Risk.SAFE
    addresses = ("disk.space", "disk.hogs", "disk.inodes")

    #: Caches that are expensive to rebuild or that tools rely on being present.
    KEEP = frozenset({"fontconfig", "mesa_shader_cache", "nvidia", "ibus"})

    def plan(self, ctx: Context) -> FixPlan:
        plan = self.new_plan()
        roots = self._roots(ctx)
        existing = [root for root in roots if os.path.isdir(root)]

        if not existing:
            return plan

        for root in existing:
            stale = collect_stale(
                root,
                older_than_days=ctx.config.cache_stale_days,
                skip_names=self.KEEP,
            )
            if not stale:
                continue
            plan.actions.append(
                delete_action(
                    f"Clear {len(stale)} stale cache director(ies) in {self._short(ctx, root)}",
                    stale,
                    [root],
                )
            )

        if plan.actions:
            plan.notes.append(
                f"Only entries untouched for {ctx.config.cache_stale_days:.0f}+ days are "
                "removed; active caches are left alone."
            )
        return plan

    def _roots(self, ctx: Context) -> list[str]:
        home = ctx.home
        roots = [os.path.join(home, ".cache")]
        if ctx.is_macos:
            roots.append(os.path.join(home, "Library", "Caches"))
        return roots

    @staticmethod
    def _short(ctx: Context, path: str) -> str:
        return path.replace(ctx.home, "~", 1)


@register_fix
class CleanTemp(Fix):
    id = "clean.tmp"
    name = "Remove old temporary files"
    description = "Deletes files in the system temp directory older than a week."
    category = "disk"
    risk = Risk.MODERATE
    platforms = ("linux", "darwin")
    addresses = ("disk.space", "disk.inodes")

    def plan(self, ctx: Context) -> FixPlan:
        plan = self.new_plan()
        root = "/tmp"
        if not os.path.isdir(root):
            return plan

        stale = collect_stale(root, older_than_days=ctx.config.tmp_stale_days)
        # Never remove sockets or lock files belonging to running sessions.
        stale = [path for path in stale if not self._in_use(path)]
        if not stale:
            return plan

        plan.actions.append(
            delete_action(
                f"Remove {len(stale)} item(s) in /tmp older than "
                f"{ctx.config.tmp_stale_days:.0f} days",
                stale,
                [root],
            )
        )
        plan.notes.append(
            "Temp files older than a week are almost always abandoned, but a "
            "long-running program could still be using one."
        )
        return plan

    @staticmethod
    def _in_use(path: str) -> bool:
        name = os.path.basename(path)
        if name.startswith((".X11", ".ICE", ".font-unix", "systemd-", "snap.")):
            return True
        try:
            import stat

            return stat.S_ISSOCK(os.lstat(path).st_mode)
        except OSError:
            return True


@register_fix
class EmptyTrash(Fix):
    id = "clean.trash"
    name = "Empty old items from the trash"
    description = "Permanently deletes trashed files that have been there a while."
    category = "disk"
    risk = Risk.MODERATE
    platforms = ("linux", "darwin")
    addresses = ("disk.space", "disk.hogs")

    def plan(self, ctx: Context) -> FixPlan:
        plan = self.new_plan()
        for root in self._trash_dirs(ctx):
            if not os.path.isdir(root):
                continue
            stale = collect_stale(root, older_than_days=ctx.config.trash_stale_days)
            if not stale:
                continue
            plan.actions.append(
                delete_action(
                    f"Permanently delete {len(stale)} trashed item(s) from "
                    f"{root.replace(ctx.home, '~', 1)}",
                    stale,
                    [root],
                    undo_note="not reversible - this is the point of no return for these files",
                )
            )

        if plan.actions:
            plan.notes.append(
                "These are files you already deleted once. After this they are gone for good."
            )
        return plan

    def _trash_dirs(self, ctx: Context) -> list[str]:
        home = ctx.home
        if ctx.is_macos:
            return [os.path.join(home, ".Trash")]
        data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        trash = os.path.join(data_home, "Trash")
        return [os.path.join(trash, "files"), os.path.join(trash, "info")]


@register_fix
class VacuumJournal(Fix):
    id = "clean.journal"
    name = "Trim the systemd journal"
    description = "Discards old journal entries, keeping recent history."
    category = "logs"
    risk = Risk.SAFE
    requires_root = True
    platforms = ("linux",)
    addresses = ("disk.space", "logs.size")

    def plan(self, ctx: Context) -> FixPlan:
        if not ctx.has("journalctl"):
            return self.blocked("journalctl not found")

        current = journal_bytes(ctx)
        if current is None:
            return self.blocked("could not determine the journal size")

        target = ctx.config.journal_vacuum_target
        limit = parse_size(target) or 0
        reclaim = current - limit

        # Nothing to reclaim is not a failure - it means the journal is
        # already within the size we would trim it to.
        if reclaim <= 0:
            return self.new_plan()

        plan = self.new_plan()
        plan.actions.append(
            self.action(
                f"Vacuum the journal down to {target}",
                argv=["journalctl", f"--vacuum-size={target}"],
                est_bytes=reclaim,
                undo_note="not reversible - discarded log entries cannot be recovered",
                requires_root=True,
            )
        )
        plan.notes.append(f"Journal currently uses {human_bytes(current)}.")
        plan.notes.append("Recent logs are kept; only the oldest entries are discarded.")
        return plan


@register_fix
class CleanPackageCache(Fix):
    id = "clean.package-cache"
    name = "Clear the package manager cache"
    description = "Removes downloaded package archives that are no longer needed."
    category = "disk"
    risk = Risk.SAFE
    platforms = ("linux", "darwin")
    addresses = ("disk.space",)

    def plan(self, ctx: Context) -> FixPlan:
        plan = self.new_plan()

        if ctx.which("apt-get"):
            if not ctx.is_root:
                return self.blocked("needs root; re-run with sudo")

            size = measure("/var/cache/apt/archives")
            if size > 1024**2:
                plan.actions.append(
                    self.action(
                        f"Delete cached .deb archives ({human_bytes(size)})",
                        argv=["apt-get", "clean"],
                        est_bytes=size,
                        undo_note="packages are re-downloaded if needed",
                        requires_root=True,
                    )
                )

            # Only offer autoremove when it would actually remove something.
            removable = self._apt_autoremovable(ctx)
            if removable:
                plan.actions.append(
                    self.action(
                        f"Remove {removable} package(s) no longer required by anything installed",
                        argv=["apt-get", "autoremove", "--purge", "-y"],
                        undo_note="reinstall with apt-get install <package>",
                        requires_root=True,
                    )
                )

        elif ctx.which("dnf") or ctx.which("yum"):
            manager = "dnf" if ctx.which("dnf") else "yum"
            if not ctx.is_root:
                return self.blocked("needs root; re-run with sudo")
            size = measure("/var/cache/dnf") or measure("/var/cache/yum")
            if size > 1024**2:
                plan.actions.append(
                    self.action(
                        f"Clear the {manager} cache ({human_bytes(size)})",
                        argv=[manager, "clean", "packages"],
                        est_bytes=size,
                        requires_root=True,
                    )
                )

        elif ctx.which("pacman"):
            if not ctx.is_root:
                return self.blocked("needs root; re-run with sudo")
            plan.actions.append(
                self.action(
                    "Remove cached packages that are no longer installed",
                    argv=["paccache", "-r", "-k2"] if ctx.which("paccache") else
                         ["pacman", "-Sc", "--noconfirm"],
                    est_bytes=measure("/var/cache/pacman/pkg"),
                    requires_root=True,
                )
            )

        elif ctx.which("brew"):
            plan.actions.append(
                self.action(
                    "Clear the Homebrew download cache",
                    argv=["brew", "cleanup", "--prune=all"],
                )
            )

        # Language package managers, which are frequently the real space hogs.
        if ctx.which("npm"):
            npm_cache = os.path.join(ctx.home, ".npm", "_cacache")
            size = measure(npm_cache)
            if size > 256 * 1024**2:
                plan.actions.append(
                    self.action(
                        "Clear the npm download cache",
                        argv=["npm", "cache", "clean", "--force"],
                        est_bytes=size,
                        undo_note="npm re-downloads packages as needed",
                    )
                )

        if ctx.which("pip") or ctx.which("pip3"):
            pip = ctx.which("pip3") or ctx.which("pip")
            cache = os.path.join(ctx.home, ".cache", "pip")
            size = measure(cache)
            if size > 256 * 1024**2 and pip:
                plan.actions.append(
                    self.action(
                        "Purge the pip wheel cache",
                        argv=[pip, "cache", "purge"],
                        est_bytes=size,
                    )
                )

        return plan

    def _apt_autoremovable(self, ctx: Context) -> int:
        """How many packages `apt-get autoremove` would remove.

        ``--just-print`` performs no changes, so this is safe to run while
        only planning.
        """
        result = ctx.run(
            ["apt-get", "--just-print", "autoremove", "--purge"],
            timeout=max(ctx.timeout, 40.0),
        )
        if not result.ok:
            return 0
        return sum(1 for line in result.lines() if line.startswith(("Remv ", "Purg ")))


@register_fix
class PruneDocker(Fix):
    id = "clean.docker"
    name = "Prune unused Docker data"
    description = "Removes stopped containers, dangling images, and unused build cache."
    category = "disk"
    risk = Risk.MODERATE
    platforms = ("linux", "darwin")
    addresses = ("disk.space",)

    def plan(self, ctx: Context) -> FixPlan:
        if not ctx.which("docker"):
            return self.blocked("docker not installed")

        probe = ctx.run(["docker", "info", "--format", "{{.ServerVersion}}"])
        if not probe.ok:
            return self.blocked("cannot talk to the Docker daemon")

        reclaimable = self._reclaimable(ctx)
        plan = self.new_plan()
        plan.actions.append(
            self.action(
                "Remove stopped containers, unused networks, dangling images and build cache",
                argv=["docker", "system", "prune", "-f"],
                est_bytes=reclaimable,
                undo_note="images are re-pulled and layers rebuilt on next use",
            )
        )
        plan.notes.append(
            "Named volumes are NOT touched, so database data in volumes is safe. "
            "Stopped containers you meant to restart will be gone."
        )
        return plan

    def _reclaimable(self, ctx: Context) -> int:
        result = ctx.run(["docker", "system", "df", "--format", "{{.Reclaimable}}"])
        if not result.ok:
            return 0
        total = 0
        for line in result.lines():
            size = parse_size(line.split("(")[0].strip())
            if size:
                total += size
        return total
