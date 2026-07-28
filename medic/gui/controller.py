"""UI-agnostic driver for the desktop app.

Tk is single-threaded and hates being touched from anywhere but the main
loop, while diagnostics take seconds and must not freeze the window. This
module owns that split: work happens on daemon threads, results arrive as
events on a queue, and the UI drains that queue from its own loop.

Keeping this layer free of Tk also means it can be tested without a
display, which is most of what is worth testing here.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core import registry
from ..core.config import Config
from ..core.context import Context
from ..core.journal import Journal
from ..core.runner import Diagnosis, apply_plan, plan_fix, run_checks

# Event kinds pushed onto the queue.
PROGRESS = "progress"
DIAGNOSIS = "diagnosis"
PLANS = "plans"
RESULTS = "results"
FAILED = "failed"
BUSY = "busy"


@dataclass
class Event:
    kind: str
    payload: Any = None


@dataclass
class PlanBundle:
    """Previewed plans plus the summary the UI needs to describe them."""

    plans: list = field(default_factory=list)
    fix_ids: list[str] = field(default_factory=list)
    est_bytes: int = 0
    highest_risk: str = "safe"

    @property
    def actionable(self) -> bool:
        return bool(self.fix_ids)


class Controller:
    """Runs medic's engine off the UI thread and reports back as events."""

    def __init__(self, *, config: Config | None = None, offline: bool = False) -> None:
        self.config = config or Config.load()
        self.offline = offline
        self.journal = Journal()
        self.events: queue.Queue[Event] = queue.Queue()
        self.diagnosis: Diagnosis | None = None
        self._busy = threading.Lock()
        self._worker: threading.Thread | None = None

    # -- state ------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    def make_context(self, *, dry_run: bool = True) -> Context:
        ctx = Context.detect()
        ctx.config = self.config
        ctx.offline = self.offline
        ctx.timeout = self.config.command_timeout
        ctx.journal = self.journal
        ctx.dry_run = dry_run
        return ctx

    def describe_system(self) -> dict[str, Any]:
        ctx = self.make_context()
        return {
            "host": ctx.hostname,
            "platform": ctx.describe(),
            "user": ctx.user,
            "is_root": ctx.is_root,
        }

    def fixes(self) -> list[dict[str, Any]]:
        """Every registered fix, annotated with whether it can run here."""
        ctx = self.make_context()
        described = []
        for fix in registry.all_fixes():
            reason = fix.unavailable_reason(ctx)
            described.append(
                {
                    "id": fix.id,
                    "name": fix.name,
                    "description": fix.description,
                    "risk": fix.risk.label,
                    "requires_root": fix.requires_root,
                    "available": not reason,
                    "reason": reason,
                }
            )
        return described

    # -- work dispatch ----------------------------------------------------

    def _spawn(self, name: str, work: Callable[[], None]) -> bool:
        """Start a worker unless one is already running."""
        if not self._busy.acquire(blocking=False):
            self.events.put(Event(FAILED, "Something is already running."))
            return False

        def runner() -> None:
            self.events.put(Event(BUSY, True))
            try:
                work()
            except Exception as exc:
                self.events.put(Event(FAILED, f"{type(exc).__name__}: {exc}"))
            finally:
                self._busy.release()
                self.events.put(Event(BUSY, False))

        self._worker = threading.Thread(target=runner, name=f"medic-gui-{name}", daemon=True)
        self._worker.start()
        return True

    def start_diagnose(self, profile: str = "full", only: list[str] | None = None) -> bool:
        def work() -> None:
            ctx = self.make_context()
            checks = registry.all_checks()
            if only:
                wanted = set(registry.resolve(only, [check.id for check in checks]))
                checks = [check for check in checks if check.id in wanted]
            else:
                checks = [check for check in checks if profile in check.profiles]

            def on_start(check, index: int, total: int) -> None:
                self.events.put(
                    Event(PROGRESS, {"current": index + 1, "total": total, "label": check.name})
                )

            diagnosis = run_checks(ctx, checks, on_start=on_start)
            self.diagnosis = diagnosis
            self.events.put(Event(DIAGNOSIS, diagnosis))

        return self._spawn("diagnose", work)

    def start_preview(self, fix_ids: list[str]) -> bool:
        def work() -> None:
            ctx = self.make_context(dry_run=True)
            bundle = PlanBundle()
            ranks = {"safe": 0, "moderate": 1, "risky": 2}

            for index, fix_id in enumerate(fix_ids):
                fix = registry.get_fix(fix_id)
                if fix is None:
                    continue
                self.events.put(
                    Event(
                        PROGRESS,
                        {"current": index + 1, "total": len(fix_ids), "label": f"Planning {fix.name}"},
                    )
                )
                plan = plan_fix(ctx, fix)
                bundle.plans.append(plan)
                if not plan.blocked and plan.actions:
                    bundle.fix_ids.append(fix_id)
                    bundle.est_bytes += plan.est_bytes
                    if ranks[plan.risk.label] > ranks[bundle.highest_risk]:
                        bundle.highest_risk = plan.risk.label

            self.events.put(Event(PLANS, bundle))

        return self._spawn("preview", work)

    def start_apply(self, fix_ids: list[str]) -> bool:
        """Apply repairs. The caller is responsible for having shown the
        plan and obtained confirmation first - that is the whole contract
        this tool makes with the user."""

        def work() -> None:
            ctx = self.make_context(dry_run=False)
            results = []

            for index, fix_id in enumerate(fix_ids):
                fix = registry.get_fix(fix_id)
                if fix is None:
                    continue
                self.events.put(
                    Event(
                        PROGRESS,
                        {"current": index + 1, "total": len(fix_ids), "label": f"Applying {fix.name}"},
                    )
                )
                plan = plan_fix(ctx, fix)
                results.append(apply_plan(ctx, fix, plan, dry_run=False, confirm=None))

            self.events.put(Event(RESULTS, results))

        return self._spawn("apply", work)

    # -- consumption ------------------------------------------------------

    def drain(self, limit: int = 100) -> list[Event]:
        """Take everything queued so far. Safe to call from the UI loop."""
        collected: list[Event] = []
        for _ in range(limit):
            try:
                collected.append(self.events.get_nowait())
            except queue.Empty:
                break
        return collected

    def wait(self, timeout: float = 120.0) -> bool:
        """Block until the current worker finishes. For tests and scripts."""
        worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()
