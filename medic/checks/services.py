"""Failed background services and crash loops."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..core.base import Check
from ..core.context import Context
from ..core.model import Finding
from ..core.registry import register_check
from ..core.util import truncate


@register_check
class FailedUnits(Check):
    id = "services.failed"
    name = "Failed system services"
    description = "Services that failed to start or crashed."
    category = "services"
    platforms = ("linux",)

    def unavailable_reason(self, ctx: Context) -> str:
        reason = super().unavailable_reason(ctx)
        if reason:
            return reason
        if not ctx.has_systemd:
            return "no systemd on this system"
        if not ctx.has("systemctl"):
            return "systemctl not found"
        return ""

    def run(self, ctx: Context) -> Iterable[Finding]:
        yield from self._scan(ctx, user_scope=False)
        yield from self._scan(ctx, user_scope=True)

    def _scan(self, ctx: Context, *, user_scope: bool) -> Iterable[Finding]:
        argv = ["systemctl"]
        if user_scope:
            argv.append("--user")
        argv += ["--failed", "--no-legend", "--plain", "list-units"]

        result = ctx.run(argv)
        if not result.ok:
            if not user_scope:
                yield self.unknown(
                    "Could not query systemd",
                    detail=result.failure_reason,
                )
            return

        units = [
            line.split()[0]
            for line in result.lines()
            if line.split() and line.split()[0].endswith((".service", ".mount", ".timer", ".socket"))
        ]
        if not units:
            return

        scope = "user" if user_scope else "system"
        detail_lines = [f"{unit}  -  {self._why(ctx, unit, user_scope)}" for unit in units[:10]]

        yield self.warn(
            f"{len(units)} failed {scope} service(s)",
            detail="\n".join(detail_lines),
            evidence={"scope": scope, "units": units},
            fix_ids=["services.restart-failed"],
            advice=(
                f"Inspect one with `systemctl {'--user ' if user_scope else ''}status "
                f"{units[0]}`. Restarting clears transient failures; a service that fails "
                "again needs its logs read."
            ),
        )

    def _why(self, ctx: Context, unit: str, user_scope: bool) -> str:
        """One-line reason a unit failed, from its most recent log entry."""
        argv = ["systemctl"]
        if user_scope:
            argv.append("--user")
        argv += ["show", unit, "--property=Result", "--property=ExecMainStatus"]
        result = ctx.run(argv)
        fields = dict(
            line.split("=", 1) for line in result.lines() if "=" in line
        )
        outcome = fields.get("Result", "")
        status = fields.get("ExecMainStatus", "")
        if outcome and outcome != "success":
            return f"{outcome}" + (f" (exit {status})" if status not in ("", "0") else "")
        return "failed"


@register_check
class CrashLoops(Check):
    id = "services.restarts"
    name = "Restart loops"
    description = "Services restarting repeatedly, which points at a persistent fault."
    category = "services"
    platforms = ("linux",)
    profiles = ("full",)

    def unavailable_reason(self, ctx: Context) -> str:
        reason = super().unavailable_reason(ctx)
        if reason:
            return reason
        if not ctx.has_systemd or not ctx.has("systemctl"):
            return "needs systemd"
        return ""

    def run(self, ctx: Context) -> Iterable[Finding]:
        result = ctx.run(
            ["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain"]
        )
        if not result.ok:
            return

        loops: list[tuple[str, int]] = []
        for line in result.lines():
            parts = line.split()
            if not parts:
                continue
            unit = parts[0]
            counter = ctx.run(["systemctl", "show", unit, "--property=NRestarts"])
            match = re.search(r"NRestarts=(\d+)", counter.stdout)
            if match and int(match.group(1)) >= 5:
                loops.append((unit, int(match.group(1))))

        for unit, restarts in sorted(loops, key=lambda item: -item[1])[:5]:
            yield self.warn(
                f"{unit} has restarted {restarts} times",
                detail=(
                    "systemd keeps restarting it, which means it keeps failing. The restart "
                    "counter resets on reboot, so this is recent."
                ),
                evidence={"unit": unit, "restarts": restarts},
                advice=f"Read why with `journalctl -u {unit} -n 50 --no-pager`.",
            )


@register_check
class LaunchdErrors(Check):
    id = "services.launchd"
    name = "Failed launchd agents"
    description = "macOS background agents exiting with an error status."
    category = "services"
    platforms = ("darwin",)

    def unavailable_reason(self, ctx: Context) -> str:
        reason = super().unavailable_reason(ctx)
        if reason:
            return reason
        if not ctx.has("launchctl"):
            return "launchctl not found"
        return ""

    def run(self, ctx: Context) -> Iterable[Finding]:
        result = ctx.run(["launchctl", "list"])
        if not result.ok:
            yield self.unknown("Could not query launchctl", detail=result.failure_reason)
            return

        failing: list[tuple[str, str]] = []
        for line in result.lines()[1:]:
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            _pid, status, label = parts
            if status not in ("0", "-") and status.lstrip("-").isdigit():
                failing.append((label.strip(), status))

        if not failing:
            return

        # Signal-terminated agents (negative status) are usually just stopped.
        errored = [(label, status) for label, status in failing if not status.startswith("-")]
        if not errored:
            return

        yield self.warn(
            f"{len(errored)} launchd agent(s) exited with an error",
            detail="\n".join(f"{truncate(label, 60)}  exit {status}" for label, status in errored[:10]),
            evidence={"agents": dict(errored)},
            advice="Inspect one with `launchctl print gui/$UID/<label>`.",
        )
