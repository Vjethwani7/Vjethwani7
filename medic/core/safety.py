"""Guardrails for destructive operations.

Nothing in medic deletes a path without going through :func:`safe_delete`,
and :func:`safe_delete` refuses anything that is not demonstrably inside an
allowed root. The rules are deliberately paranoid, because the failure mode
of a "clean up my disk" tool getting a path wrong is losing someone's data.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass, field

from .util import dir_size

#: Paths that must never be deleted even if some caller thinks otherwise.
FORBIDDEN = frozenset(
    {
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib32",
        "/lib64",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/usr",
        "/usr/bin",
        "/usr/lib",
        "/usr/local",
        "/usr/sbin",
        "/usr/share",
        "/var",
        "/var/lib",
        "/var/log",
        "/Applications",
        "/Library",
        "/System",
        "/Users",
        "/Volumes",
        "C:\\",
        "C:\\Windows",
        "C:\\Program Files",
    }
)


class UnsafePathError(Exception):
    """Raised when a delete target fails a guardrail."""


@dataclass
class DeleteReport:
    deleted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def is_within(path: str, root: str) -> bool:
    """True when ``path`` resolves to somewhere inside ``root``.

    Both sides are fully resolved first, so a symlink pointing out of the
    root cannot sneak past.
    """
    real_path = _real(path)
    real_root = _real(root)
    try:
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:
        # Different drives on Windows, or a mix of relative/absolute.
        return False


def assert_safe(path: str, allowed_roots: list[str]) -> None:
    """Raise :class:`UnsafePathError` unless ``path`` is safe to delete.

    A path is safe when it exists, is not a system directory, is not a
    mount point, and resolves inside at least one allowed root. The home
    directory itself is never deletable, only things beneath it.
    """
    if not path or not path.strip():
        raise UnsafePathError("empty path")

    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        raise UnsafePathError(f"{path}: not an absolute path")

    real = _real(expanded)

    if real in FORBIDDEN or real.rstrip(os.sep) in FORBIDDEN:
        raise UnsafePathError(f"{path}: refusing to touch system directory")

    if real == _real("~"):
        raise UnsafePathError(f"{path}: refusing to delete the home directory itself")

    # A two-component path like /var or /usr is essentially always a system
    # root; require some depth before we will remove anything.
    depth = len([part for part in real.split(os.sep) if part])
    if depth < 2:
        raise UnsafePathError(f"{path}: too close to the filesystem root")

    if not allowed_roots:
        raise UnsafePathError(f"{path}: no allowed roots configured")

    if not any(is_within(real, root) for root in allowed_roots if root):
        roots = ", ".join(allowed_roots)
        raise UnsafePathError(f"{path}: outside permitted roots ({roots})")

    if os.path.ismount(real):
        raise UnsafePathError(f"{path}: is a mount point")


def measure(path: str) -> int:
    """Size of a file or directory in bytes, symlinks not followed."""
    try:
        info = os.lstat(path)
    except OSError:
        return 0
    if stat.S_ISLNK(info.st_mode):
        return info.st_size
    if stat.S_ISDIR(info.st_mode):
        return dir_size(path)
    return info.st_size


def safe_delete(
    paths: list[str],
    allowed_roots: list[str],
    *,
    dry_run: bool = True,
) -> DeleteReport:
    """Delete paths that pass every guardrail; skip and report the rest.

    In ``dry_run`` mode nothing is removed but sizes are still measured, so
    a preview can honestly state how much space would be reclaimed.
    """
    report = DeleteReport()

    for path in paths:
        try:
            assert_safe(path, allowed_roots)
        except UnsafePathError as exc:
            report.skipped.append((path, str(exc)))
            continue

        if not os.path.lexists(path):
            report.skipped.append((path, "no longer exists"))
            continue

        size = measure(path)

        if dry_run:
            report.deleted.append(path)
            report.bytes_freed += size
            continue

        try:
            if os.path.islink(path) or os.path.isfile(path):
                os.unlink(path)
            else:
                _rmtree(path)
            report.deleted.append(path)
            report.bytes_freed += size
        except OSError as exc:
            report.errors.append(f"{path}: {exc.strerror or exc}")

    return report


def _rmtree(path: str) -> None:
    """``shutil.rmtree`` with a retry handler, across Python versions.

    The error-callback parameter was renamed from ``onerror`` to ``onexc``
    in 3.12 and the old name warns, so pick whichever this interpreter has.
    """
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda func, target, exc: _retry_removal(target))
    else:
        shutil.rmtree(path, onerror=lambda func, target, info: _retry_removal(target))


def _retry_removal(path: str) -> None:
    """Retry a failed removal once after clearing the read-only bit."""
    try:
        os.chmod(path, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
        if os.path.isdir(path) and not os.path.islink(path):
            os.rmdir(path)
        else:
            os.unlink(path)
    except OSError:
        # Leave it; safe_delete reports what it could not remove.
        pass


def collect_stale(
    root: str,
    *,
    older_than_days: float,
    top_level_only: bool = True,
    skip_names: frozenset[str] = frozenset(),
) -> list[str]:
    """List entries under ``root`` unmodified for ``older_than_days``.

    Only the top level is considered by default: removing a whole stale
    cache subdirectory is predictable, whereas pruning individual files out
    of a live cache tree can leave it inconsistent.

    Staleness is judged by modification time alone. Access time looks like
    the better signal - "when was this cache last used?" - but it is not
    usable here: most filesystems are mounted ``relatime`` or ``noatime``
    so it is unreliable, and worse, simply measuring a directory to build a
    preview updates it. Using atime made previewing a cleanup erase the
    very finding it had just reported.
    """
    import time

    if not os.path.isdir(root):
        return []

    cutoff = time.time() - (older_than_days * 86400.0)
    stale: list[str] = []

    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return []

    for entry in entries:
        if entry.name in skip_names or entry.name.startswith(".nfs"):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        newest = info.st_mtime
        if entry.is_dir(follow_symlinks=False) and not top_level_only:
            newest = max(newest, _newest_mtime(entry.path))
        if newest < cutoff:
            stale.append(entry.path)

    return stale


def _newest_mtime(path: str, *, max_entries: int = 50_000) -> float:
    newest = 0.0
    seen = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            seen += 1
            if seen > max_entries:
                return newest
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
            except OSError:
                continue
    return newest
