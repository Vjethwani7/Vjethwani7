"""Rendering: terminal output, Markdown reports, and JSON."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterable
from typing import Any, TextIO

from .base import Fix
from .model import CheckReport, Finding, FixPlan, FixResult, Severity
from .runner import Diagnosis
from .util import human_bytes, truncate

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

SEVERITY_COLOR = {
    Severity.OK: "\033[32m",
    Severity.INFO: "\033[36m",
    Severity.UNKNOWN: "\033[35m",
    Severity.WARN: "\033[33m",
    Severity.CRITICAL: "\033[31m",
}

SEVERITY_GLYPH = {
    Severity.OK: "ok  ",
    Severity.INFO: "info",
    Severity.UNKNOWN: "??  ",
    Severity.WARN: "warn",
    Severity.CRITICAL: "CRIT",
}


class Printer:
    """Terminal writer that degrades to plain text when colour is unwanted."""

    def __init__(self, stream: TextIO | None = None, *, color: bool | None = None) -> None:
        self.stream = stream or sys.stdout
        self.color = self._should_color() if color is None else color

    def _should_color(self) -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("FORCE_COLOR"):
            return True
        return bool(getattr(self.stream, "isatty", lambda: False)())

    def paint(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.color and code else text

    def write(self, text: str = "") -> None:
        self.stream.write(text + "\n")

    def header(self, text: str) -> None:
        self.write()
        self.write(self.paint(text, BOLD))
        self.write(self.paint("─" * min(len(text), 72), DIM))

    def dim(self, text: str) -> None:
        self.write(self.paint(text, DIM))

    def severity_tag(self, severity: Severity) -> str:
        return self.paint(SEVERITY_GLYPH[severity], SEVERITY_COLOR[severity])


def render_diagnosis(
    diagnosis: Diagnosis,
    printer: Printer,
    *,
    verbose: bool = False,
    show_ok: bool = False,
    minimum: Severity = Severity.INFO,
) -> None:
    """Print a diagnostic run to the terminal."""
    printer.header(f"medic  {diagnosis.host}  {diagnosis.platform}")

    shown_any = False
    for report in _ordered(diagnosis.reports):
        if report.error:
            printer.write(
                f"  {printer.severity_tag(Severity.UNKNOWN)}  {report.name}: "
                f"check failed - {report.error}"
            )
            shown_any = True
            continue

        if report.skipped:
            if verbose:
                printer.dim(f"  skip  {report.name}: {report.skipped_reason}")
            continue

        visible = [finding for finding in report.findings if finding.severity >= minimum]
        if not visible:
            if show_ok:
                printer.write(f"  {printer.severity_tag(Severity.OK)}  {report.name}")
            continue

        shown_any = True
        for finding in visible:
            _render_finding(finding, printer, verbose=verbose)

    if not shown_any:
        printer.write()
        printer.write(
            "  " + printer.paint("No problems found.", SEVERITY_COLOR[Severity.OK])
        )

    _render_summary(diagnosis, printer)


def _render_finding(finding: Finding, printer: Printer, *, verbose: bool) -> None:
    printer.write(f"  {printer.severity_tag(finding.severity)}  {finding.title}")

    if finding.detail:
        for line in finding.detail.splitlines():
            printer.write(f"          {line}")

    if finding.advice:
        printer.write(f"          {printer.paint('→ ' + finding.advice, DIM)}")

    if finding.fix_ids:
        commands = ", ".join(f"medic fix {fix_id}" for fix_id in finding.fix_ids)
        printer.write(f"          {printer.paint('→ ' + commands, DIM)}")

    if verbose and finding.evidence:
        for key, value in finding.evidence.items():
            printer.write(f"          {printer.paint(f'{key}: {value}', DIM)}")


def _render_summary(diagnosis: Diagnosis, printer: Printer) -> None:
    counts = diagnosis.counts()
    printer.write()

    parts = []
    for level in (Severity.CRITICAL, Severity.WARN, Severity.INFO, Severity.UNKNOWN):
        if counts.get(level.label):
            parts.append(
                printer.paint(f"{counts[level.label]} {level.label}", SEVERITY_COLOR[level])
            )
    summary = ", ".join(parts) if parts else printer.paint("all clear", SEVERITY_COLOR[Severity.OK])

    printer.write(
        f"  {summary}  "
        + printer.paint(
            f"({counts['checks_run']} checks in {diagnosis.duration_ms} ms)", DIM
        )
    )

    suggested = diagnosis.suggested_fix_ids()
    if suggested:
        printer.write()
        printer.write(f"  {printer.paint('Suggested repairs', BOLD)}")
        printer.write(f"    medic fix {' '.join(suggested)}      {printer.paint('# preview', DIM)}")
        printer.write(
            f"    medic fix {' '.join(suggested)} --apply"
            f"  {printer.paint('# actually do it', DIM)}"
        )


def _ordered(reports: Iterable[CheckReport]) -> list[CheckReport]:
    """Worst news first; stable within a severity."""
    return sorted(reports, key=lambda report: (-report.severity, report.category, report.check_id))


# -- fix rendering ---------------------------------------------------------


def render_plan(fix: Fix, plan: FixPlan, printer: Printer, *, dry_run: bool) -> None:
    label = "would do" if dry_run else "doing"
    risk_color = {"safe": "\033[32m", "moderate": "\033[33m", "risky": "\033[31m"}
    printer.write()
    printer.write(
        f"  {printer.paint(fix.name, BOLD)}  "
        f"{printer.paint('[' + plan.risk.label + ']', risk_color.get(plan.risk.label, ''))}  "
        f"{printer.paint(fix.id, DIM)}"
    )

    if plan.blocked:
        printer.write(f"    {printer.paint('blocked: ' + plan.blocked_reason, DIM)}")
        return

    if plan.empty:
        printer.write(f"    {printer.paint('nothing to do', DIM)}")
        return

    for note in plan.notes:
        printer.write(f"    {printer.paint('note: ' + note, DIM)}")

    printer.write(f"    {printer.paint(label + ':', DIM)}")
    for action in plan.actions:
        suffix = ""
        if action.est_bytes:
            suffix = printer.paint(f"  (~{human_bytes(action.est_bytes)})", DIM)
        printer.write(f"      - {action.description}{suffix}")
        if action.argv:
            printer.write(f"        {printer.paint('$ ' + ' '.join(action.argv), DIM)}")
        if action.undo_note:
            printer.write(f"        {printer.paint('undo: ' + action.undo_note, DIM)}")


def render_fix_result(result: FixResult, printer: Printer) -> None:
    if result.blocked_reason or result.skipped_reason:
        return
    for item in result.results:
        if item.ok:
            mark = printer.paint("  ok", SEVERITY_COLOR[Severity.OK])
        else:
            mark = printer.paint("fail", SEVERITY_COLOR[Severity.CRITICAL])
        message = f"  {item.message}" if item.message and not item.ok else ""
        printer.write(f"    {mark}  {truncate(item.description, 90)}{message}")


def render_fix_summary(results: list[FixResult], printer: Printer, *, dry_run: bool) -> None:
    freed = sum(result.bytes_freed for result in results)
    applied = [result for result in results if result.applied]
    failed = [result for result in results if result.applied and not result.ok]

    printer.write()
    if dry_run:
        printer.write(
            f"  {printer.paint('Preview only - nothing was changed.', BOLD)}"
        )
        if freed:
            printer.write(f"  Applying these would free about {human_bytes(freed)}.")
        printer.write(f"  {printer.paint('Re-run with --apply to make these changes.', DIM)}")
        return

    printer.write(f"  {printer.paint('Applied', BOLD)} {len(applied)} fix(es).")
    if freed:
        printer.write(f"  Reclaimed about {human_bytes(freed)}.")
    if failed:
        names = ", ".join(result.fix_id for result in failed)
        printer.write(
            f"  {printer.paint('Some steps failed: ' + names, SEVERITY_COLOR[Severity.WARN])}"
        )


# -- other formats ---------------------------------------------------------


def to_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def to_markdown(diagnosis: Diagnosis) -> str:
    """A shareable report - useful when asking someone else for help."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(diagnosis.started_at))
    counts = diagnosis.counts()

    lines = [
        "# System diagnostic report",
        "",
        f"- **Host:** {diagnosis.host}",
        f"- **Platform:** {diagnosis.platform}",
        f"- **Generated:** {stamp}",
        f"- **Overall:** {diagnosis.severity.label}",
        f"- **Checks run:** {counts['checks_run']} "
        f"({counts['checks_skipped']} skipped, {counts['checks_failed']} failed)",
        "",
    ]

    findings = [
        finding for finding in diagnosis.findings if finding.severity >= Severity.INFO
    ]
    if not findings:
        lines += ["No problems found.", ""]
        return "\n".join(lines)

    lines += ["## Findings", "", "| Severity | Check | Finding |", "| --- | --- | --- |"]
    for finding in sorted(findings, key=lambda item: -item.severity):
        title = finding.title.replace("|", "\\|")
        lines.append(f"| {finding.severity.label} | `{finding.check_id}` | {title} |")
    lines.append("")

    lines += ["## Details", ""]
    for finding in sorted(findings, key=lambda item: -item.severity):
        lines.append(f"### {finding.title}")
        lines.append("")
        lines.append(f"- **Severity:** {finding.severity.label}")
        lines.append(f"- **Check:** `{finding.check_id}`")
        if finding.detail:
            lines += ["", "```", finding.detail, "```"]
        if finding.advice:
            lines += ["", f"**What to do:** {finding.advice}"]
        if finding.fix_ids:
            commands = ", ".join(f"`medic fix {fix_id}`" for fix_id in finding.fix_ids)
            lines += ["", f"**Automated repair:** {commands}"]
        lines.append("")

    return "\n".join(lines)
