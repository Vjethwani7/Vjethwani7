"""Uptime, boot health, firewall state, and broken PATH entries."""

from __future__ import annotations

import os
from collections.abc import Iterable

from ..core.base import Check
from ..core.context import Context
from ..core.model import Finding
from ..core.probe import system_info
from ..core.registry import register_check
from ..core.util import human_duration


@register_check
class Uptime(Check):
    id = "sys.uptime"
    name = "Uptime"
    description = "Long uptime means pending kernel and security updates are not active."
    category = "system"

    def run(self, ctx: Context) -> Iterable[Finding]:
        info = system_info(ctx)
        if info.uptime_seconds is None:
            return

        days = info.uptime_seconds / 86400.0
        if days < ctx.config.uptime_warn_days:
            if ctx.verbose:
                yield self.info(
                    f"Up for {human_duration(info.uptime_seconds)}",
                    evidence={"uptime_seconds": int(info.uptime_seconds)},
                )
            return

        yield self.info(
            f"Up for {human_duration(info.uptime_seconds)} without a reboot",
            detail=(
                "Kernel and core library updates only take effect after a restart, so a "
                "long-running machine can be fully patched on disk and still vulnerable "
                "in memory."
            ),
            evidence={"uptime_seconds": int(info.uptime_seconds), "days": round(days, 1)},
            advice="A restart is the single cheapest fix for a machine that has got slowly worse.",
        )


@register_check
class PendingReboot(Check):
    id = "sys.reboot-required"
    name = "Reboot required"
    description = "Detects when the system itself says a restart is needed."
    category = "system"
    platforms = ("linux",)

    def run(self, ctx: Context) -> Iterable[Finding]:
        if os.path.exists("/var/run/reboot-required"):
            packages = ctx.read_text("/var/run/reboot-required.pkgs") or ""
            names = sorted({line.strip() for line in packages.splitlines() if line.strip()})
            yield self.warn(
                "A reboot is required to finish applying updates",
                detail=(
                    ("Waiting on: " + ", ".join(names[:12])) if names else "/var/run/reboot-required exists"
                ),
                evidence={"packages": names},
                advice="Restart when convenient; until then the old code is still running.",
            )
            return

        # Red Hat family equivalent.
        if ctx.has("needs-restarting"):
            result = ctx.run(["needs-restarting", "-r"])
            if result.returncode == 1:
                yield self.warn(
                    "A reboot is required to finish applying updates",
                    detail=result.stdout.strip()[:400],
                    advice="Restart when convenient.",
                )


@register_check
class Firewall(Check):
    id = "sec.firewall"
    name = "Firewall"
    description = "Whether inbound traffic is being filtered."
    category = "security"
    platforms = ("linux", "darwin")
    profiles = ("full",)

    def run(self, ctx: Context) -> Iterable[Finding]:
        if ctx.is_macos:
            result = ctx.run(
                ["defaults", "read", "/Library/Preferences/com.apple.alf", "globalstate"]
            )
            if result.ok and result.stdout.strip() == "0":
                yield self.info(
                    "The macOS firewall is off",
                    detail="Inbound connections are not being filtered.",
                    advice=(
                        "Turn it on in System Settings > Network > Firewall if you use "
                        "untrusted networks."
                    ),
                )
            return

        if ctx.in_container:
            return

        if ctx.has("ufw"):
            result = ctx.run(["ufw", "status"])
            if result.ok and "inactive" in result.stdout.lower():
                yield self.info(
                    "ufw firewall is installed but inactive",
                    detail=result.stdout.strip()[:200],
                    advice="Enable it with `sudo ufw enable` if this machine is on shared networks.",
                )
            return

        if ctx.has("firewall-cmd"):
            result = ctx.run(["firewall-cmd", "--state"])
            if result.stdout.strip() and "not running" in result.stdout:
                yield self.info(
                    "firewalld is installed but not running",
                    advice="Start it with `sudo systemctl start firewalld`.",
                )


@register_check
class BrokenPath(Check):
    id = "sys.path"
    name = "PATH sanity"
    description = "Missing or world-writable directories in PATH."
    category = "system"
    platforms = ("linux", "darwin")
    profiles = ("full",)

    def run(self, ctx: Context) -> Iterable[Finding]:
        raw = os.environ.get("PATH", "")
        entries = [entry for entry in raw.split(os.pathsep) if entry]

        missing = [entry for entry in entries if not os.path.isdir(entry)]
        writable: list[str] = []
        for entry in entries:
            try:
                mode = os.stat(entry).st_mode
            except OSError:
                continue
            # World-writable and not sticky: anyone can drop a binary in here.
            if mode & 0o002 and not mode & 0o1000:
                writable.append(entry)

        if writable:
            yield self.warn(
                f"{len(writable)} world-writable director(ies) in PATH",
                detail="\n".join(writable),
                evidence={"paths": writable},
                advice=(
                    "Any user on this machine can place an executable there and have you "
                    "run it. Tighten with `chmod o-w <dir>`."
                ),
            )

        if missing and ctx.verbose:
            yield self.info(
                f"{len(missing)} PATH entr(ies) do not exist",
                detail="\n".join(missing),
                evidence={"paths": missing},
                advice="Harmless, but they slow down every command lookup slightly.",
            )


@register_check
class Environment(Check):
    id = "sys.environment"
    name = "System summary"
    description = "Basic facts about the machine, always shown with --verbose."
    category = "system"
    profiles = ("full",)

    def run(self, ctx: Context) -> Iterable[Finding]:
        if not ctx.verbose:
            return
        info = system_info(ctx)
        details = [
            f"host: {ctx.hostname}",
            f"platform: {ctx.describe()}",
            f"cores: {info.cpu_count}",
            f"uptime: {human_duration(info.uptime_seconds)}",
            f"user: {ctx.user}{' (root)' if ctx.is_root else ''}",
        ]
        if ctx.init_system:
            details.append(f"init: {ctx.init_system}")
        yield self.info("System summary", detail="\n".join(details))
