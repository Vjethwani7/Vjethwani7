"""Tests for check and fix execution."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from medic.core.base import Check, Fix
from medic.core.model import Action, ActionOutcome, Finding, FixPlan, Risk, Severity
from medic.core.runner import apply_plan, plan_fix, run_checks


class HealthyCheck(Check):
    id = "test.healthy"
    name = "Healthy"
    category = "test"

    def run(self, ctx) -> Iterable[Finding]:
        return []


class WarningCheck(Check):
    id = "test.warning"
    name = "Warning"
    category = "test"

    def run(self, ctx) -> Iterable[Finding]:
        yield self.warn("something is off", fix_ids=["test.fix"])


class CriticalCheck(Check):
    id = "test.critical"
    name = "Critical"
    category = "test"

    def run(self, ctx) -> Iterable[Finding]:
        yield self.critical("very bad")


class ExplodingCheck(Check):
    id = "test.explode"
    name = "Exploding"
    category = "test"

    def run(self, ctx) -> Iterable[Finding]:
        raise RuntimeError("boom")


class SkippedCheck(Check):
    id = "test.skipped"
    name = "Skipped"
    category = "test"

    def unavailable_reason(self, ctx) -> str:
        return "not applicable here"

    def run(self, ctx) -> Iterable[Finding]:
        raise AssertionError("must not run when unavailable")


class TestRunChecks:
    def test_healthy_check_yields_ok(self, ctx):
        diagnosis = run_checks(ctx, [HealthyCheck()])
        assert diagnosis.severity is Severity.OK
        assert diagnosis.reports[0].severity is Severity.OK

    def test_overall_severity_is_the_worst_finding(self, ctx):
        diagnosis = run_checks(ctx, [HealthyCheck(), WarningCheck(), CriticalCheck()])
        assert diagnosis.severity is Severity.CRITICAL

    def test_a_crashing_check_does_not_abort_the_run(self, ctx):
        diagnosis = run_checks(ctx, [ExplodingCheck(), WarningCheck()])

        assert len(diagnosis.reports) == 2
        exploded = next(r for r in diagnosis.reports if r.check_id == "test.explode")
        assert "boom" in exploded.error
        assert exploded.severity is Severity.UNKNOWN
        # The healthy check still ran and reported.
        assert any(r.check_id == "test.warning" for r in diagnosis.reports)

    def test_unavailable_check_is_skipped_not_run(self, ctx):
        diagnosis = run_checks(ctx, [SkippedCheck()])
        report = diagnosis.reports[0]
        assert report.skipped
        assert report.skipped_reason == "not applicable here"

    def test_suggested_fixes_come_from_findings(self, ctx):
        diagnosis = run_checks(ctx, [WarningCheck()])
        assert diagnosis.suggested_fix_ids() == ["test.fix"]

    def test_counts_tally_by_severity(self, ctx):
        diagnosis = run_checks(ctx, [WarningCheck(), CriticalCheck(), SkippedCheck()])
        counts = diagnosis.counts()
        assert counts["warn"] == 1
        assert counts["critical"] == 1
        assert counts["checks_skipped"] == 1

    def test_progress_hook_is_called_for_each_check(self, ctx):
        seen = []
        run_checks(ctx, [HealthyCheck(), WarningCheck()], on_start=lambda c, i, t: seen.append(c.id))
        assert seen == ["test.healthy", "test.warning"]

    def test_run_is_journalled(self, ctx, journal):
        ctx.journal = journal
        run_checks(ctx, [WarningCheck()])
        entries = journal.read()
        assert entries and entries[-1]["event"] == "diagnose"


# -- fixes -----------------------------------------------------------------


class RecordingFix(Fix):
    id = "test.fix"
    name = "Recording fix"
    category = "test"
    risk = Risk.SAFE

    def __init__(self) -> None:
        self.calls: list[str] = []

    def plan(self, ctx) -> FixPlan:
        def do(context) -> ActionOutcome:
            self.calls.append("ran")
            return ActionOutcome(ok=True, message="done", bytes_freed=100)

        return self.new_plan(actions=[Action("do the thing", func=do, est_bytes=100)])


class EmptyFix(Fix):
    id = "test.empty"
    name = "Nothing to do"
    risk = Risk.SAFE

    def plan(self, ctx) -> FixPlan:
        return self.new_plan()


class RootFix(Fix):
    id = "test.root"
    name = "Needs root"
    risk = Risk.SAFE
    requires_root = True

    def plan(self, ctx) -> FixPlan:
        return self.new_plan(actions=[Action("privileged", argv=["true"])])


class BrokenPlanFix(Fix):
    id = "test.broken"
    name = "Planning explodes"
    risk = Risk.SAFE

    def plan(self, ctx) -> FixPlan:
        raise ValueError("cannot plan")


class TestApplyPlan:
    def test_dry_run_does_not_execute_actions(self, ctx):
        fix = RecordingFix()
        result = apply_plan(ctx, fix, fix.plan(ctx), dry_run=True)

        assert fix.calls == [], "dry run must not invoke the action"
        assert not result.applied
        assert result.bytes_freed == 100, "estimate is still reported"

    def test_apply_executes_actions(self, ctx):
        fix = RecordingFix()
        result = apply_plan(ctx, fix, fix.plan(ctx), dry_run=False)

        assert fix.calls == ["ran"]
        assert result.applied and result.ok
        assert result.bytes_freed == 100

    def test_declining_confirmation_prevents_execution(self, ctx):
        fix = RecordingFix()
        result = apply_plan(
            ctx, fix, fix.plan(ctx), dry_run=False, confirm=lambda f, p: False
        )

        assert fix.calls == []
        assert result.skipped_reason == "declined by user"

    def test_confirmation_is_not_asked_during_dry_run(self, ctx):
        fix = RecordingFix()
        asked = []
        apply_plan(
            ctx, fix, fix.plan(ctx), dry_run=True,
            confirm=lambda f, p: asked.append(f.id) or True,
        )
        assert asked == []

    def test_empty_plan_is_skipped(self, ctx):
        fix = EmptyFix()
        result = apply_plan(ctx, fix, fix.plan(ctx), dry_run=False)
        assert result.skipped_reason == "nothing to do"

    def test_root_fix_is_blocked_without_root(self, ctx):
        ctx.is_root = False
        plan = plan_fix(ctx, RootFix())
        assert plan.blocked and "root" in plan.blocked_reason

    def test_planning_failure_becomes_a_blocked_plan(self, ctx):
        plan = plan_fix(ctx, BrokenPlanFix())
        assert plan.blocked
        assert "cannot plan" in plan.blocked_reason

    def test_failing_action_is_reported_not_raised(self, ctx):
        class Failing(Fix):
            id = "test.failing"
            name = "Failing"

            def plan(self, context) -> FixPlan:
                def do(c) -> ActionOutcome:
                    raise OSError("permission denied")

                return self.new_plan(actions=[Action("try", func=do)])

        fix = Failing()
        result = apply_plan(ctx, fix, fix.plan(ctx), dry_run=False)

        assert not result.ok
        assert "permission denied" in result.results[0].message

    def test_applied_fix_is_journalled(self, ctx, journal):
        ctx.journal = journal
        fix = RecordingFix()
        apply_plan(ctx, fix, fix.plan(ctx), dry_run=False)

        events = [entry["event"] for entry in journal.read()]
        assert "fix.begin" in events and "fix.end" in events

    def test_dry_run_is_not_journalled(self, ctx, journal):
        ctx.journal = journal
        fix = RecordingFix()
        apply_plan(ctx, fix, fix.plan(ctx), dry_run=True)

        assert journal.read() == []


class TestActionValidation:
    def test_action_requires_exactly_one_of_argv_or_func(self):
        with pytest.raises(ValueError):
            Action("neither")
        with pytest.raises(ValueError):
            Action("both", argv=["true"], func=lambda c: ActionOutcome())
