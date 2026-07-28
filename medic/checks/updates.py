"""Pending package updates and clock synchronisation."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..core.base import Check
from ..core.context import Context
from ..core.model import Finding
from ..core.registry import register_check


def detect_package_manager(ctx: Context) -> str:
    """Name of the system package manager, or '' when unrecognised."""
    for name in ("apt-get", "dnf", "yum", "pacman", "zypper", "apk", "brew"):
        if ctx.which(name):
            return "apt" if name == "apt-get" else name
    return ""


@register_check
class PendingUpdates(Check):
    id = "updates.pending"
    name = "Pending updates"
    description = "Counts available package updates, highlighting security ones."
    category = "updates"
    platforms = ("linux", "darwin")
    profiles = ("full",)

    def unavailable_reason(self, ctx: Context) -> str:
        reason = super().unavailable_reason(ctx)
        if reason:
            return reason
        if not detect_package_manager(ctx):
            return "no supported package manager found"
        return ""

    def run(self, ctx: Context) -> Iterable[Finding]:
        manager = detect_package_manager(ctx)
        packages, security = self._count(ctx, manager)

        if packages is None:
            yield self.unknown(
                f"Could not query {manager} for updates",
                advice=(
                    "The package list may be stale. Refresh it manually, e.g. "
                    "`sudo apt-get update`."
                ),
            )
            return

        if security and security >= ctx.config.security_updates_warn_count:
            yield self.warn(
                f"{security} security update(s) available",
                detail=f"{packages} package update(s) pending in total ({manager}).",
                evidence={"manager": manager, "total": packages, "security": security},
                fix_ids=["updates.apply"],
                advice="Security updates are worth applying promptly.",
            )
        elif packages >= ctx.config.updates_warn_count:
            yield self.info(
                f"{packages} package update(s) available",
                detail=f"Package manager: {manager}",
                evidence={"manager": manager, "total": packages},
                fix_ids=["updates.apply"],
            )

    def _count(self, ctx: Context, manager: str) -> tuple[int | None, int]:
        """(total updates, security updates). Total is None when unknown."""
        if manager == "apt":
            # --just-print works without root and does not touch the system.
            result = ctx.run(
                ["apt-get", "--just-print", "upgrade"], timeout=max(ctx.timeout, 40.0)
            )
            if not result.ok:
                return None, 0
            lines = [line for line in result.lines() if line.startswith("Inst ")]
            security = sum(1 for line in lines if re.search(r"security", line, re.I))
            return len(lines), security

        if manager in ("dnf", "yum"):
            result = ctx.run([manager, "-q", "check-update"], timeout=max(ctx.timeout, 60.0))
            # check-update exits 100 when updates exist - that is not a failure.
            if result.returncode not in (0, 100):
                return None, 0
            lines = [
                line
                for line in result.lines()
                if line and not line.startswith(("Last metadata", "Obsoleting"))
                and len(line.split()) >= 3
            ]
            security_result = ctx.run(
                [manager, "-q", "--security", "check-update"], timeout=max(ctx.timeout, 60.0)
            )
            security = (
                len([line for line in security_result.lines() if len(line.split()) >= 3])
                if security_result.returncode in (0, 100)
                else 0
            )
            return len(lines), security

        if manager == "pacman":
            result = ctx.run(["pacman", "-Qu"], timeout=max(ctx.timeout, 30.0))
            if result.returncode not in (0, 1):
                return None, 0
            return len(result.lines()), 0

        if manager == "zypper":
            result = ctx.run(["zypper", "--quiet", "list-updates"], timeout=max(ctx.timeout, 60.0))
            if not result.ok:
                return None, 0
            return max(0, len(result.lines()) - 2), 0

        if manager == "apk":
            result = ctx.run(["apk", "version", "-l", "<"], timeout=max(ctx.timeout, 30.0))
            if not result.ok:
                return None, 0
            return max(0, len(result.lines()) - 1), 0

        if manager == "brew":
            result = ctx.run(["brew", "outdated", "--quiet"], timeout=max(ctx.timeout, 60.0))
            if not result.ok:
                return None, 0
            return len(result.lines()), 0

        return None, 0


@register_check
class ClockSync(Check):
    id = "time.sync"
    name = "Clock synchronisation"
    description = "A wrong clock breaks TLS, logins, and package downloads."
    category = "updates"
    platforms = ("linux", "darwin")

    def unavailable_reason(self, ctx: Context) -> str:
        reason = super().unavailable_reason(ctx)
        if reason:
            return reason
        if ctx.is_linux and not ctx.has("timedatectl"):
            return "needs timedatectl"
        if ctx.in_container:
            return "the clock belongs to the host, not this container"
        return ""

    def run(self, ctx: Context) -> Iterable[Finding]:
        if ctx.is_linux and ctx.has("timedatectl"):
            result = ctx.run(["timedatectl", "show"])
            if not result.ok:
                # timedatectl is installed but cannot talk to systemd, which
                # happens in minimal images. Say so rather than implying the
                # clock was checked and found fine.
                yield self.unknown(
                    "Could not determine whether the clock is synchronised",
                    detail=f"timedatectl failed: {result.failure_reason}",
                    evidence={"error": result.failure_reason},
                )
                return
            if result.ok:
                fields = dict(
                    line.split("=", 1) for line in result.lines() if "=" in line
                )
                synced = fields.get("NTPSynchronized", "").lower() == "yes"
                enabled = fields.get("NTP", "").lower() == "yes"
                if not enabled:
                    yield self.warn(
                        "Automatic time synchronisation is disabled",
                        detail="timedatectl reports NTP=no.",
                        evidence=fields,
                        fix_ids=["time.enable-sync"],
                        advice=(
                            "Clock drift eventually breaks HTTPS certificate validation and "
                            "authentication."
                        ),
                    )
                elif not synced:
                    yield self.info(
                        "Time synchronisation is enabled but has not synced yet",
                        detail="This is normal shortly after boot or a network change.",
                        evidence=fields,
                        fix_ids=["time.enable-sync"],
                    )
                return

        if ctx.is_macos:
            result = ctx.run(["systemsetup", "-getusingnetworktime"])
            if result.ok and "Off" in result.stdout:
                yield self.warn(
                    "Automatic time synchronisation is off",
                    detail=result.stdout.strip(),
                    fix_ids=["time.enable-sync"],
                )
            return

