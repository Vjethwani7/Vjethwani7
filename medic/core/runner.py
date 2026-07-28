"""Execution engines for checks and fixes."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Callable

from .base import Check, Fix
from .context import Context
from .model import (
    Action,
    ActionOutcome,
    ActionResult,
    CheckReport,
    Finding,
    FixPlan,
    FixResult,
    Risk,
    Severity,
)
from .util import human_bytes


@dataclass
class Diagnosis:
    """The result of a full diagnostic run."""

    reports: list[CheckReport] = field(default_factory=list)
    started_at: float = 0.0
    duration_ms: int = 0
    host: str = ""
    platform: str = ""

    @property
    def findings(self) -> list[Finding]:
        return [finding for report in self.reports for finding in report.findings]

    @property
    def severity(self) -> Severity:
        actionable = [
            report.severity
            for report in self.reports
            if not report.skipped and report.severity is not Severity.OK
        ]
        return max(actionable) if actionable else Severity.OK

    def by_severity(self, minimum: Severity) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity >= minimum]

    def suggested_fix_ids(self) -> list[str]:
        seen: list[str] = []
        for finding in sorted(self.findings, key=lambda f: -f.severity):
            for fix_id in finding.fix_ids:
                if fix_id not in seen:
                    seen.append(fix_id)
        return seen

    def counts(self) -> dict[str, int]:
        tally = {level.label: 0 for level in Severity}
        for finding in self.findings:
            tally[finding.severity.label] += 1
        tally["checks_run"] = sum(1 for report in self.reports if not report.skipped)
        tally["checks_skipped"] = sum(1 for report in self.reports if report.skipped)
        tally["checks_failed"] = sum(1 for report in self.reports if report.error)
        return tally

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "host": self.host,
            "platform": self.platform,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "severity": self.severity.label,
            "counts": self.counts(),
            "suggested_fixes": self.suggested_fix_ids(),
            "reports": [report.to_dict() for report in self.reports],
        }


ProgressHook = Callable[[Check, int, int], None]


def run_checks(
    ctx: Context,
    checks: Iterable[Check],
    *,
    on_start: ProgressHook | None = None,
) -> Diagnosis:
    """Run each check, isolating failures so one bad check cannot abort the run."""
    checks = list(checks)
    diagnosis = Diagnosis(
        started_at=time.time(),
        host=ctx.hostname,
        platform=ctx.describe(),
    )
    run_start = time.perf_counter()

    for index, check in enumerate(checks):
        if on_start:
            on_start(check, index, len(checks))

        report = CheckReport(check_id=check.id, name=check.name, category=check.category)
        started = time.perf_counter()

        reason = ""
        try:
            reason = check.unavailable_reason(ctx)
        except Exception as exc:  # a broken availability test is still a bug
            report.error = f"{type(exc).__name__}: {exc}"

        if not report.error:
            if reason:
                report.skipped_reason = reason
            else:
                try:
                    report.findings = [f for f in check.run(ctx) if f is not None]
                except Exception as exc:
                    report.error = f"{type(exc).__name__}: {exc}"

        report.duration_ms = int((time.perf_counter() - started) * 1000)
        diagnosis.reports.append(report)

    diagnosis.duration_ms = int((time.perf_counter() - run_start) * 1000)

    if ctx.journal:
        ctx.journal.record(
            "diagnose",
            severity=diagnosis.severity.label,
            counts=diagnosis.counts(),
            duration_ms=diagnosis.duration_ms,
        )

    return diagnosis


def plan_fix(ctx: Context, fix: Fix) -> FixPlan:
    """Build a fix plan, converting planning errors into a blocked plan."""
    reason = fix.unavailable_reason(ctx)
    if reason:
        return FixPlan(fix_id=fix.id, name=fix.name, risk=fix.risk, blocked_reason=reason)
    try:
        return fix.plan(ctx)
    except Exception as exc:
        return FixPlan(
            fix_id=fix.id,
            name=fix.name,
            risk=fix.risk,
            blocked_reason=f"planning failed: {type(exc).__name__}: {exc}",
        )


Confirmer = Callable[[Fix, FixPlan], bool]


def apply_plan(
    ctx: Context,
    fix: Fix,
    plan: FixPlan,
    *,
    dry_run: bool,
    confirm: Confirmer | None = None,
) -> FixResult:
    """Execute a plan's actions, or preview them when ``dry_run``.

    Confirmation is asked once per fix, after the full plan is known, so
    the user always sees the complete set of actions before agreeing to
    any of them.
    """
    result = FixResult(fix_id=fix.id, name=fix.name, applied=False)

    if plan.blocked:
        result.blocked_reason = plan.blocked_reason
        return result

    if plan.empty:
        result.skipped_reason = "nothing to do"
        return result

    if not dry_run and confirm is not None and not confirm(fix, plan):
        result.skipped_reason = "declined by user"
        return result

    if ctx.journal and not dry_run:
        ctx.journal.record(
            "fix.begin",
            fix_id=fix.id,
            risk=fix.risk.label,
            actions=[action.description for action in plan.actions],
        )

    for action in plan.actions:
        result.results.append(_run_action(ctx, action, dry_run=dry_run))

    result.applied = not dry_run

    if ctx.journal and not dry_run:
        ctx.journal.record(
            "fix.end",
            fix_id=fix.id,
            ok=result.ok,
            bytes_freed=result.bytes_freed,
            results=[item.to_dict() for item in result.results],
        )

    return result


def _run_action(ctx: Context, action: Action, *, dry_run: bool) -> ActionResult:
    if action.requires_root and not ctx.is_root:
        return ActionResult(
            description=action.description,
            ok=False,
            applied=False,
            message="needs root",
        )

    if dry_run:
        message = "would run: " + " ".join(action.argv) if action.argv else "would apply"
        if action.est_bytes:
            message += f" (frees about {human_bytes(action.est_bytes)})"
        return ActionResult(
            description=action.description,
            ok=True,
            applied=False,
            message=message,
            bytes_freed=action.est_bytes,
        )

    if action.argv is not None:
        command = ctx.run(action.argv, cache=False, timeout=max(ctx.timeout, 120.0))
        return ActionResult(
            description=action.description,
            ok=command.ok,
            applied=True,
            message="" if command.ok else command.failure_reason,
            bytes_freed=action.est_bytes if command.ok else 0,
        )

    assert action.func is not None
    try:
        outcome = action.func(ctx)
    except Exception as exc:
        return ActionResult(
            description=action.description,
            ok=False,
            applied=True,
            message=f"{type(exc).__name__}: {exc}",
        )

    if not isinstance(outcome, ActionOutcome):
        outcome = ActionOutcome(ok=True, message=str(outcome))

    return ActionResult(
        description=action.description,
        ok=outcome.ok,
        applied=True,
        message=outcome.message,
        bytes_freed=outcome.bytes_freed,
    )


def select_fixes(
    ctx: Context,
    fixes: Iterable[Fix],
    *,
    max_risk: Risk,
) -> tuple[list[Fix], list[tuple[Fix, str]]]:
    """Split fixes into runnable and excluded-with-reason."""
    runnable: list[Fix] = []
    excluded: list[tuple[Fix, str]] = []
    for fix in fixes:
        if fix.risk > max_risk:
            excluded.append((fix, f"risk {fix.risk.label} exceeds limit {max_risk.label}"))
            continue
        reason = fix.unavailable_reason(ctx)
        if reason:
            excluded.append((fix, reason))
            continue
        runnable.append(fix)
    return runnable, excluded
