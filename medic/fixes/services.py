"""Restarting services that have failed."""

from __future__ import annotations

from ..core.base import Fix
from ..core.context import Context
from ..core.model import FixPlan, Risk
from ..core.registry import register_fix


@register_fix
class RestartFailedUnits(Fix):
    id = "services.restart-failed"
    name = "Restart failed services"
    description = (
        "Restarts systemd units currently in the failed state. Transient failures "
        "clear; a genuinely broken service will simply fail again."
    )
    category = "services"
    risk = Risk.MODERATE
    platforms = ("linux",)
    addresses = ("services.failed",)

    #: Restarting these mid-session disconnects you or interrupts the boot chain.
    NEVER_RESTART = frozenset(
        {
            "systemd-logind.service",
            "dbus.service",
            "systemd-journald.service",
            "systemd-udevd.service",
            "networking.service",
            "NetworkManager.service",
            "sshd.service",
            "ssh.service",
        }
    )

    def plan(self, ctx: Context) -> FixPlan:
        if not ctx.has_systemd or not ctx.which("systemctl"):
            return self.blocked("needs systemd")

        plan = self.new_plan()

        for user_scope in (False, True):
            units = self._failed_units(ctx, user_scope)
            if not units:
                continue
            if not user_scope and not ctx.is_root:
                plan.notes.append(
                    f"{len(units)} failed system service(s) need root to restart; "
                    "re-run with sudo to include them."
                )
                continue

            for unit in units:
                if unit in self.NEVER_RESTART:
                    plan.notes.append(
                        f"Skipping {unit}: restarting it would disrupt the running session. "
                        "Restart it deliberately if you mean to."
                    )
                    continue
                argv = ["systemctl"]
                if user_scope:
                    argv.append("--user")
                argv += ["restart", unit]
                plan.actions.append(
                    self.action(
                        f"Restart {unit}" + (" (user)" if user_scope else ""),
                        argv=argv,
                        undo_note=f"stop again with `systemctl {'--user ' if user_scope else ''}stop {unit}`",
                        requires_root=not user_scope,
                    )
                )

        if plan.actions:
            plan.notes.append(
                "If a service fails again immediately, the restart is not the answer - "
                "read its log with `journalctl -u <unit> -n 50`."
            )
        return plan

    def _failed_units(self, ctx: Context, user_scope: bool) -> list[str]:
        argv = ["systemctl"]
        if user_scope:
            argv.append("--user")
        argv += ["--failed", "--no-legend", "--plain", "list-units", "--type=service"]
        result = ctx.run(argv)
        if not result.ok:
            return []
        return [
            line.split()[0]
            for line in result.lines()
            if line.split() and line.split()[0].endswith(".service")
        ]
