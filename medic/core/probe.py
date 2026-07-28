"""Cross-platform readers for raw system state.

Checks stay readable by asking these functions for structured data instead
of parsing command output themselves. Every probe degrades gracefully:
when a platform cannot answer, it returns an empty result rather than
raising, and the calling check reports that it could not determine the
answer.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import string
from dataclasses import dataclass, field

from .context import Context

# Filesystem types that are not real storage and would only add noise.
PSEUDO_FS = frozenset(
    {
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fuse.gvfsd-fuse",
        "fuse.portal",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "nsfs",
        "overlay",
        "proc",
        "pstore",
        "ramfs",
        "securityfs",
        "selinuxfs",
        "squashfs",
        "sysfs",
        "tracefs",
    }
)

PSEUDO_MOUNT_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/snap", "/System/Volumes/VM")


@dataclass
class Filesystem:
    device: str
    mountpoint: str
    fstype: str = ""
    total: int = 0
    used: int = 0
    free: int = 0
    inodes_total: int = 0
    inodes_used: int = 0
    read_only: bool = False

    @property
    def percent_used(self) -> float:
        if not self.total:
            return 0.0
        # Match df: usage is measured against space usable by this user,
        # which excludes the root-reserved blocks.
        usable = self.used + self.free
        return (self.used / usable * 100.0) if usable else 0.0

    @property
    def inodes_percent_used(self) -> float:
        if not self.inodes_total:
            return 0.0
        return self.inodes_used / self.inodes_total * 100.0


@dataclass
class Memory:
    total: int = 0
    available: int = 0
    swap_total: int = 0
    swap_free: int = 0

    @property
    def used(self) -> int:
        return max(0, self.total - self.available)

    @property
    def percent_used(self) -> float:
        if not self.total:
            return 0.0
        return self.used / self.total * 100.0

    @property
    def swap_used(self) -> int:
        return max(0, self.swap_total - self.swap_free)

    @property
    def swap_percent_used(self) -> float:
        if not self.swap_total:
            return 0.0
        return self.swap_used / self.swap_total * 100.0


@dataclass
class Process:
    pid: int
    ppid: int = 0
    user: str = ""
    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    rss_bytes: int = 0
    state: str = ""
    command: str = ""


@dataclass
class SystemInfo:
    uptime_seconds: float | None = None
    boot_time: float | None = None
    load_average: tuple[float, float, float] | None = None
    cpu_count: int = field(default_factory=lambda: os.cpu_count() or 1)


# -- filesystems -----------------------------------------------------------


def filesystems(ctx: Context) -> list[Filesystem]:
    """Real, mounted filesystems with usage and inode counts."""
    if ctx.is_windows:
        return _filesystems_windows()
    found = _filesystems_posix(ctx)
    if not found:
        found = _filesystems_fallback()
    return found


def _filesystems_posix(ctx: Context) -> list[Filesystem]:
    result = ctx.run(["df", "-kP"])
    if not result.ok:
        return []

    by_mount: dict[str, Filesystem] = {}
    for line in result.lines()[1:]:
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        device, blocks, used, available, _capacity, mountpoint = parts
        try:
            total = int(blocks) * 1024
            used_bytes = int(used) * 1024
            free_bytes = int(available) * 1024
        except ValueError:
            continue
        if total <= 0:
            continue
        if _is_pseudo_mount(mountpoint, device):
            continue
        by_mount[mountpoint] = Filesystem(
            device=device,
            mountpoint=mountpoint,
            total=total,
            used=used_bytes,
            free=free_bytes,
        )

    inode_result = ctx.run(["df", "-iP"])
    if inode_result.ok:
        for line in inode_result.lines()[1:]:
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            _device, itotal, iused, _ifree, _pct, mountpoint = parts
            filesystem = by_mount.get(mountpoint)
            if filesystem is None:
                continue
            try:
                filesystem.inodes_total = int(itotal)
                filesystem.inodes_used = int(iused)
            except ValueError:
                continue

    for mountpoint, fstype, options in _mount_table(ctx):
        filesystem = by_mount.get(mountpoint)
        if filesystem is not None:
            filesystem.fstype = fstype
            filesystem.read_only = "ro" in options

    return [fs for fs in by_mount.values() if fs.fstype not in PSEUDO_FS]


def _mount_table(ctx: Context) -> list[tuple[str, str, set[str]]]:
    """(mountpoint, fstype, options) for each mount."""
    entries: list[tuple[str, str, set[str]]] = []

    content = ctx.read_text("/proc/mounts")
    if content:
        for line in content.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            mountpoint = parts[1].replace("\\040", " ")
            entries.append((mountpoint, parts[2], set(parts[3].split(","))))
        return entries

    result = ctx.run(["mount"])
    if result.ok:
        # BSD/macOS: "/dev/disk1s1 on / (apfs, local, journaled)"
        for line in result.lines():
            match = re.match(r"^(\S+) on (.+?) \((.+)\)$", line)
            if not match:
                continue
            options = {opt.strip() for opt in match.group(3).split(",")}
            fstype = next(iter(match.group(3).split(",")), "").strip()
            entries.append((match.group(2), fstype, options))
    return entries


def _is_pseudo_mount(mountpoint: str, device: str) -> bool:
    if mountpoint in ("/", "/home"):
        return False
    if any(mountpoint.startswith(prefix) for prefix in PSEUDO_MOUNT_PREFIXES):
        return True
    return device in ("tmpfs", "devtmpfs", "none", "udev", "map")


def _filesystems_fallback() -> list[Filesystem]:
    """Last resort: just report the root filesystem via shutil."""
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return []
    return [
        Filesystem(
            device="",
            mountpoint="/",
            total=usage.total,
            used=usage.used,
            free=usage.free,
        )
    ]


def _filesystems_windows() -> list[Filesystem]:
    found: list[Filesystem] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.exists(drive):
            continue
        try:
            usage = shutil.disk_usage(drive)
        except OSError:
            continue
        found.append(
            Filesystem(
                device=drive,
                mountpoint=drive,
                fstype="ntfs",
                total=usage.total,
                used=usage.used,
                free=usage.free,
            )
        )
    return found


# -- memory ----------------------------------------------------------------


def memory(ctx: Context) -> Memory | None:
    if ctx.is_linux:
        return _memory_linux(ctx)
    if ctx.is_macos:
        return _memory_macos(ctx)
    if ctx.is_windows:
        return _memory_windows()
    return None


def _memory_linux(ctx: Context) -> Memory | None:
    content = ctx.read_text("/proc/meminfo")
    if not content:
        return None
    values: dict[str, int] = {}
    for line in content.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            values[key.strip()] = int(parts[0]) * 1024
        except ValueError:
            continue
    if "MemTotal" not in values:
        return None
    available = values.get("MemAvailable")
    if available is None:
        available = values.get("MemFree", 0) + values.get("Cached", 0) + values.get("Buffers", 0)
    return Memory(
        total=values["MemTotal"],
        available=available,
        swap_total=values.get("SwapTotal", 0),
        swap_free=values.get("SwapFree", 0),
    )


def _memory_macos(ctx: Context) -> Memory | None:
    total_result = ctx.run(["sysctl", "-n", "hw.memsize"])
    if not total_result.ok:
        return None
    try:
        total = int(total_result.stdout.strip())
    except ValueError:
        return None

    available = 0
    vm_result = ctx.run(["vm_stat"])
    if vm_result.ok:
        page_size = 4096
        header = re.search(r"page size of (\d+) bytes", vm_result.stdout)
        if header:
            page_size = int(header.group(1))
        pages: dict[str, int] = {}
        for line in vm_result.lines():
            match = re.match(r'^"?([^":]+)"?:\s+(\d+)\.?', line)
            if match:
                pages[match.group(1).strip()] = int(match.group(2))
        free_pages = pages.get("Pages free", 0) + pages.get("Pages inactive", 0)
        available = free_pages * page_size

    swap_total = swap_free = 0
    swap_result = ctx.run(["sysctl", "-n", "vm.swapusage"])
    if swap_result.ok:
        # "total = 2048.00M  used = 512.00M  free = 1536.00M"
        scale = {"K": 1024, "M": 1024**2, "G": 1024**3}
        for key, value, unit in re.findall(r"(\w+) = ([\d.]+)([KMG])", swap_result.stdout):
            if key == "total":
                swap_total = int(float(value) * scale[unit])
            elif key == "free":
                swap_free = int(float(value) * scale[unit])

    return Memory(total=total, available=available, swap_total=swap_total, swap_free=swap_free)


def _memory_windows() -> Memory | None:
    try:
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return None
        return Memory(
            total=int(status.ullTotalPhys),
            available=int(status.ullAvailPhys),
            swap_total=int(status.ullTotalPageFile),
            swap_free=int(status.ullAvailPageFile),
        )
    except Exception:
        return None


# -- processes -------------------------------------------------------------


def processes(ctx: Context, *, exclude_self: bool = True) -> list[Process]:
    """Running processes.

    medic's own process tree is excluded by default: scanning the system
    costs CPU, so without this the tool reliably reports itself as the
    busiest thing on the machine. Callers that need to tell "the process
    list is empty" apart from "listing failed" should pass
    ``exclude_self=False`` and filter with :func:`without_self`.
    """
    found = _processes_windows(ctx) if ctx.is_windows else _processes_posix(ctx)
    return without_self(found) if exclude_self else found


def without_self(found: list[Process]) -> list[Process]:
    """Drop medic's own process and its immediate parent."""
    mine = {os.getpid(), os.getppid()}
    return [item for item in found if item.pid not in mine and item.ppid not in mine]


def _processes_posix(ctx: Context) -> list[Process]:
    result = ctx.run(
        ["ps", "-eo", "pid=,ppid=,user=,pcpu=,pmem=,rss=,state=,args="],
        timeout=max(ctx.timeout, 20.0),
    )
    if not result.ok:
        result = ctx.run(["ps", "aux"])
        if not result.ok:
            return []
        return _parse_ps_aux(result.stdout)

    found: list[Process] = []
    for line in result.lines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            found.append(
                Process(
                    pid=int(parts[0]),
                    ppid=int(parts[1]),
                    user=parts[2],
                    cpu_percent=float(parts[3]),
                    mem_percent=float(parts[4]),
                    rss_bytes=int(parts[5]) * 1024,
                    state=parts[6],
                    command=parts[7].strip(),
                )
            )
        except ValueError:
            continue
    return found


def _parse_ps_aux(text: str) -> list[Process]:
    found: list[Process] = []
    for line in text.splitlines()[1:]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        try:
            found.append(
                Process(
                    pid=int(parts[1]),
                    user=parts[0],
                    cpu_percent=float(parts[2]),
                    mem_percent=float(parts[3]),
                    rss_bytes=int(parts[5]) * 1024,
                    state=parts[7],
                    command=parts[10].strip(),
                )
            )
        except ValueError:
            continue
    return found


def _processes_windows(ctx: Context) -> list[Process]:
    result = ctx.run(["tasklist", "/fo", "csv", "/nh"])
    if not result.ok:
        return []
    import csv
    import io

    found: list[Process] = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) < 5:
            continue
        try:
            memory_kb = int(re.sub(r"[^\d]", "", row[4]) or 0)
            found.append(
                Process(
                    pid=int(row[1]),
                    command=row[0],
                    rss_bytes=memory_kb * 1024,
                )
            )
        except ValueError:
            continue
    return found


# -- general system --------------------------------------------------------


def journal_bytes(ctx: Context) -> int | None:
    """Bytes used by the systemd journal, or None when it cannot be read.

    ``journalctl --disk-usage`` prints e.g. "Archived and active journals
    take up 96.0M in the file system." - the units and the trailing 'B'
    both vary between systemd versions.
    """
    if not ctx.which("journalctl"):
        return None
    result = ctx.run(["journalctl", "--disk-usage"])
    if not result.ok:
        return None
    match = re.search(r"take up\s+([\d.]+)\s*([KMGTP]?)i?B?", result.stdout)
    if not match:
        return None
    scale = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    try:
        return int(float(match.group(1)) * scale[match.group(2).upper()])
    except (ValueError, KeyError):
        return None


def system_info(ctx: Context) -> SystemInfo:
    info = SystemInfo()

    if hasattr(os, "getloadavg"):
        with contextlib.suppress(OSError):
            info.load_average = os.getloadavg()

    content = ctx.read_text("/proc/uptime")
    if content:
        with contextlib.suppress(ValueError, IndexError):
            info.uptime_seconds = float(content.split()[0])

    if info.uptime_seconds is None and ctx.is_macos:
        result = ctx.run(["sysctl", "-n", "kern.boottime"])
        if result.ok:
            match = re.search(r"sec\s*=\s*(\d+)", result.stdout)
            if match:
                import time

                info.boot_time = float(match.group(1))
                info.uptime_seconds = time.time() - info.boot_time

    if info.uptime_seconds is not None and info.boot_time is None:
        import time

        info.boot_time = time.time() - info.uptime_seconds

    return info
