"""System-level repairs: time synchronisation and package updates."""

from __future__ import annotations

from ..checks.updates import detect_package_manager
from ..core.base import Fix
from ..core.context import Context
from ..core.model import FixPlan, Risk
from ..core.registry import register_fix


@register_fix
class EnableTimeSync(Fix):
    id = "time.enable-sync"
    name = "Turn on automatic time synchronisation"
    description = "Keeps the clock correct, which HTTPS and authentication depend on."
    category = "system"
    risk = Risk.SAFE
    requires_root = True
    platforms = ("linux", "darwin")
    addresses = ("time.sync",)

    def unavailable_reason(self, ctx: Context) -> str:
        reason = super().unavailable_reason(ctx)
        if reason:
            return reason
        if ctx.in_container:
            return "the clock belongs to the host, not this container"
        return ""

    def plan(self, ctx: Context) -> FixPlan:
        plan = self.new_plan()

        if ctx.is_macos:
            current = ctx.run(["systemsetup", "-getusingnetworktime"])
            if current.ok and "On" in current.stdout:
                return plan  # already enabled
            plan.actions.append(
                self.action(
                    "Enable network time",
                    argv=["systemsetup", "-setusingnetworktime", "on"],
                    undo_note="disable with `systemsetup -setusingnetworktime off`",
                    requires_root=True,
                )
            )
            return plan

        if not ctx.which("timedatectl"):
            return self.blocked("timedatectl not found")

        # If the current state cannot be read, do not guess - a fix that
        # cannot verify what it is changing should not run at all.
        current = ctx.run(["timedatectl", "show", "--property=NTP"])
        if not current.ok:
            return self.blocked(f"timedatectl is not working here: {current.failure_reason}")
        if current.stdout.strip().endswith("=yes"):
            return plan  # already enabled

        plan.actions.append(
            self.action(
                "Enable NTP synchronisation",
                argv=["timedatectl", "set-ntp", "true"],
                undo_note="disable with `timedatectl set-ntp false`",
                requires_root=True,
            )
        )
        plan.notes.append("The clock may jump once synchronisation completes.")
        return plan


@register_fix
class ApplyUpdates(Fix):
    id = "updates.apply"
    name = "Install pending package updates"
    description = "Upgrades installed packages using the system package manager."
    category = "updates"
    risk = Risk.RISKY
    requires_root = True
    platforms = ("linux", "darwin")
    addresses = ("updates.pending",)

    def plan(self, ctx: Context) -> FixPlan:
        manager = detect_package_manager(ctx)
        if not manager:
            return self.blocked("no supported package manager found")

        commands = {
            "apt": [
                (["apt-get", "update"], "Refresh the package list"),
                (["apt-get", "upgrade", "-y"], "Upgrade installed packages"),
            ],
            "dnf": [(["dnf", "upgrade", "-y"], "Upgrade installed packages")],
            "yum": [(["yum", "update", "-y"], "Upgrade installed packages")],
            "pacman": [(["pacman", "-Syu", "--noconfirm"], "Upgrade installed packages")],
            "zypper": [(["zypper", "--non-interactive", "update"], "Upgrade installed packages")],
            "apk": [(["apk", "upgrade"], "Upgrade installed packages")],
            "brew": [(["brew", "upgrade"], "Upgrade installed formulae")],
        }

        if manager not in commands:
            return self.blocked(f"updates via {manager} are not automated")

        plan = self.new_plan()
        needs_root = manager != "brew"
        for argv, description in commands[manager]:
            plan.actions.append(
                self.action(
                    description,
                    argv=argv,
                    undo_note="package downgrades are manual and version-specific",
                    requires_root=needs_root,
                )
            )

        plan.notes.append(
            "This is the riskiest fix medic offers: upgrades can change behaviour, and "
            "rolling back is manual. Review what is pending first with "
            "`medic diagnose --only updates.pending --verbose`."
        )
        plan.notes.append("This can take several minutes and will download data.")
        return plan

    def unavailable_reason(self, ctx: Context) -> str:
        if ctx.os_name not in self.platforms:
            return f"not supported on {ctx.os_name}"
        manager = detect_package_manager(ctx)
        if not manager:
            return "no supported package manager found"
        # Homebrew is the exception: it manages a user-owned prefix and
        # actively refuses to run with elevated privileges.
        if manager == "brew":
            return "Homebrew refuses to run as root" if ctx.is_root else ""
        if not ctx.is_root:
            return "needs root; re-run this fix with sudo"
        return ""
