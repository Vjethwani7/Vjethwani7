"""Run context: what machine are we on, what may we touch, and how loudly.

A single :class:`Context` is built once per invocation and threaded through
every check and fix. It owns the command cache, so a run that inspects
``systemctl --failed`` from three different checks only pays for it once.
"""

from __future__ import annotations

import getpass
import os
import platform
import socket
import sys
from dataclasses import dataclass, field
from typing import Any

from . import shell
from .config import Config
from .journal import Journal


def _detect_distro() -> str:
    """Human-readable distro name on Linux, else the platform release."""
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            fields: dict[str, str] = {}
            for line in handle:
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                fields[key.strip()] = value.strip().strip('"')
        return fields.get("PRETTY_NAME") or fields.get("NAME") or ""
    except OSError:
        return ""


def _detect_init() -> str:
    """Which service manager is in charge: systemd, launchd, openrc, or ''."""
    if sys.platform == "darwin":
        return "launchd"
    if os.path.isdir("/run/systemd/system"):
        return "systemd"
    if shell.which("openrc") or os.path.isdir("/run/openrc"):
        return "openrc"
    return ""


def _detect_container() -> str:
    """Best-effort container/VM hint. Empty string when we look like bare metal."""
    if os.path.exists("/.dockerenv"):
        return "docker"
    # systemd sets this variable in lowercase; that is not a typo.
    if os.environ.get("container"):  # noqa: SIM112
        return os.environ["container"]  # noqa: SIM112
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as handle:
            content = handle.read()
        for marker in ("docker", "containerd", "kubepods", "lxc", "podman"):
            if marker in content:
                return marker
    except OSError:
        pass
    if os.environ.get("WSL_DISTRO_NAME"):
        return "wsl"
    return ""


@dataclass
class Context:
    """Everything a check or fix needs to know about the current run."""

    os_name: str
    distro: str = ""
    init_system: str = ""
    container: str = ""
    arch: str = ""
    hostname: str = ""
    user: str = ""
    home: str = ""
    is_root: bool = False
    dry_run: bool = True
    assume_yes: bool = False
    verbose: bool = False
    #: When set, no check may make an outbound network connection.
    offline: bool = False
    timeout: float = shell.DEFAULT_TIMEOUT
    config: Config = field(default_factory=Config)
    journal: Journal | None = None
    _cmd_cache: dict[tuple[str, ...], shell.CommandResult] = field(
        default_factory=dict, repr=False
    )
    _which_cache: dict[str, str | None] = field(default_factory=dict, repr=False)

    # -- construction -----------------------------------------------------

    @classmethod
    def detect(cls, **overrides: Any) -> Context:
        """Build a context by inspecting the current machine."""
        try:
            user = getpass.getuser()
        except Exception:  # getpass raises when there is no passwd entry
            user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"

        ctx = cls(
            os_name=_os_name(),
            distro=_detect_distro(),
            init_system=_detect_init(),
            container=_detect_container(),
            arch=platform.machine(),
            hostname=socket.gethostname(),
            user=user,
            home=os.path.expanduser("~"),
            is_root=(hasattr(os, "geteuid") and os.geteuid() == 0),
        )
        for key, value in overrides.items():
            setattr(ctx, key, value)
        return ctx

    # -- platform predicates ----------------------------------------------

    @property
    def is_linux(self) -> bool:
        return self.os_name == "linux"

    @property
    def is_macos(self) -> bool:
        return self.os_name == "darwin"

    @property
    def is_windows(self) -> bool:
        return self.os_name == "windows"

    @property
    def is_posix(self) -> bool:
        return self.os_name in ("linux", "darwin")

    @property
    def has_systemd(self) -> bool:
        return self.init_system == "systemd"

    @property
    def in_container(self) -> bool:
        return bool(self.container)

    def describe(self) -> str:
        bits = [self.distro or platform.platform(terse=True), self.arch]
        if self.container:
            bits.append(f"in {self.container}")
        return "  ".join(part for part in bits if part)

    # -- command helpers ---------------------------------------------------

    def which(self, name: str) -> str | None:
        """Cached executable lookup."""
        if name not in self._which_cache:
            self._which_cache[name] = shell.which(name)
        return self._which_cache[name]

    def has(self, *names: str) -> bool:
        """True when every named executable is present."""
        return all(self.which(name) for name in names)

    def run(
        self,
        argv: list[str],
        *,
        cache: bool = True,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> shell.CommandResult:
        """Run a command, reusing an identical earlier result when caching.

        Only read-only commands should be cached; mutating commands pass
        ``cache=False``.
        """
        key = tuple(argv)
        if cache and key in self._cmd_cache:
            return self._cmd_cache[key]
        result = shell.run(argv, timeout=timeout or self.timeout, **kwargs)
        if cache:
            self._cmd_cache[key] = result
        return result

    def read_text(self, path: str, *, limit: int = 1_000_000) -> str | None:
        """Read a small text file, returning None instead of raising."""
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read(limit)
        except OSError:
            return None


def _os_name() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform in ("win32", "cygwin"):
        return "windows"
    return sys.platform
