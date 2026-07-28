"""Network repairs: DNS cache flushing."""

from __future__ import annotations

from ..core.base import Fix
from ..core.context import Context
from ..core.model import FixPlan, Risk
from ..core.registry import register_fix


@register_fix
class FlushDns(Fix):
    id = "net.flush-dns"
    name = "Flush the DNS cache"
    description = (
        "Clears cached name lookups. Fixes the case where a site moved and your "
        "machine is still trying the old address."
    )
    category = "network"
    risk = Risk.SAFE
    requires_root = True
    platforms = ("linux", "darwin")
    addresses = ("net.connectivity", "net.dns")

    def plan(self, ctx: Context) -> FixPlan:
        plan = self.new_plan()

        if ctx.is_macos:
            plan.actions.append(
                self.action(
                    "Flush the macOS DNS cache",
                    argv=["dscacheutil", "-flushcache"],
                    requires_root=True,
                )
            )
            plan.actions.append(
                self.action(
                    "Restart the mDNSResponder service",
                    argv=["killall", "-HUP", "mDNSResponder"],
                    undo_note="the service restarts itself immediately",
                    requires_root=True,
                )
            )
            return plan

        if ctx.which("resolvectl"):
            plan.actions.append(
                self.action(
                    "Flush systemd-resolved caches",
                    argv=["resolvectl", "flush-caches"],
                    requires_root=True,
                )
            )
        elif ctx.which("systemd-resolve"):
            plan.actions.append(
                self.action(
                    "Flush systemd-resolved caches",
                    argv=["systemd-resolve", "--flush-caches"],
                    requires_root=True,
                )
            )
        elif ctx.which("nscd"):
            plan.actions.append(
                self.action(
                    "Invalidate the nscd hosts cache",
                    argv=["nscd", "-i", "hosts"],
                    requires_root=True,
                )
            )
        else:
            return self.blocked("no DNS cache service found (nothing to flush)")

        # A stub resolver that is dead needs starting, not just flushing.
        if ctx.has_systemd and ctx.which("systemctl"):
            status = ctx.run(["systemctl", "is-active", "systemd-resolved"])
            if status.stdout.strip() not in ("active", "activating"):
                plan.actions.append(
                    self.action(
                        "Start systemd-resolved, which is not running",
                        argv=["systemctl", "restart", "systemd-resolved"],
                        undo_note="stop with `systemctl stop systemd-resolved`",
                        requires_root=True,
                    )
                )

        return plan
