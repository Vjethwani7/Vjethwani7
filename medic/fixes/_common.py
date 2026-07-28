"""Helpers shared by the cleanup fixes."""

from __future__ import annotations

from ..core.context import Context
from ..core.model import Action, ActionOutcome
from ..core.safety import measure, safe_delete
from ..core.util import human_bytes


def delete_action(
    description: str,
    paths: list[str],
    allowed_roots: list[str],
    *,
    undo_note: str = "",
) -> Action:
    """Build an action that deletes ``paths``, guarded by :mod:`core.safety`.

    Sizes are measured now so the dry-run preview can state honestly how
    much space the action would reclaim. The paths list is captured by
    value, so a plan built earlier cannot be pointed at different targets
    later.
    """
    targets = list(paths)
    estimate = sum(measure(path) for path in targets)
    roots = list(allowed_roots)

    def run(ctx: Context) -> ActionOutcome:
        report = safe_delete(targets, roots, dry_run=False)
        message = f"removed {len(report.deleted)} item(s), {human_bytes(report.bytes_freed)}"
        if report.skipped:
            message += f"; skipped {len(report.skipped)}"
        if report.errors:
            message += f"; {len(report.errors)} error(s): {report.errors[0]}"
        return ActionOutcome(
            ok=not report.errors,
            message=message,
            bytes_freed=report.bytes_freed,
        )

    return Action(
        description=description,
        func=run,
        est_bytes=estimate,
        undo_note=undo_note or "not reversible - these files are deleted, not moved to trash",
    )
