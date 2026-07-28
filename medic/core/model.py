"""Core data types: findings, check reports, fix plans and results.

These are deliberately plain dataclasses with no behaviour beyond
serialisation, so that the JSON output format and the terminal renderer can
both be written against the same stable shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable


class Severity(IntEnum):
    """How much a finding should worry you.

    Ordered so that ``max()`` over a list of findings gives the headline
    severity for a report.
    """

    OK = 0
    INFO = 10
    UNKNOWN = 20
    WARN = 30
    CRITICAL = 40

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, text: str) -> Severity:
        try:
            return cls[text.strip().upper()]
        except KeyError as exc:
            raise ValueError(f"unknown severity: {text!r}") from exc


class Risk(IntEnum):
    """How much damage a fix could plausibly do if it goes wrong."""

    SAFE = 0
    MODERATE = 1
    RISKY = 2

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, text: str) -> Risk:
        try:
            return cls[text.strip().upper()]
        except KeyError as exc:
            raise ValueError(f"unknown risk level: {text!r}") from exc


@dataclass
class Finding:
    """One observation made by one check."""

    check_id: str
    title: str
    severity: Severity = Severity.INFO
    detail: str = ""
    #: Machine-readable supporting numbers, shown with --verbose.
    evidence: dict[str, Any] = field(default_factory=dict)
    #: Fix ids that claim to address this finding.
    fix_ids: list[str] = field(default_factory=list)
    #: What a human should do when no automated fix exists.
    advice: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "title": self.title,
            "severity": self.severity.label,
            "detail": self.detail,
            "evidence": self.evidence,
            "fix_ids": list(self.fix_ids),
            "advice": self.advice,
        }


@dataclass
class CheckReport:
    """The outcome of running a single check."""

    check_id: str
    name: str
    category: str
    findings: list[Finding] = field(default_factory=list)
    #: Set when the check declined to run (wrong OS, missing tool, no perms).
    skipped_reason: str = ""
    #: Set when the check raised. A crashing check is a bug, not a diagnosis.
    error: str = ""
    duration_ms: int = 0

    @property
    def skipped(self) -> bool:
        return bool(self.skipped_reason)

    @property
    def severity(self) -> Severity:
        if self.error:
            return Severity.UNKNOWN
        if self.skipped:
            return Severity.INFO
        if not self.findings:
            return Severity.OK
        return max(finding.severity for finding in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "category": self.category,
            "severity": self.severity.label,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass
class ActionOutcome:
    """What happened when a single action ran."""

    ok: bool = True
    message: str = ""
    bytes_freed: int = 0


@dataclass
class Action:
    """One concrete step inside a fix plan.

    An action is either a subprocess invocation (``argv``) or a Python
    callable (``func``). Both forms are previewable: in dry-run mode the
    executor prints ``description`` and, where known, ``est_bytes``, without
    touching anything.
    """

    description: str
    argv: list[str] | None = None
    func: Callable[[object], ActionOutcome] | None = None
    #: Best-effort estimate of disk space this reclaims.
    est_bytes: int = 0
    #: How to undo this, in plain words. Empty means "not reversible".
    undo_note: str = ""
    requires_root: bool = False

    def __post_init__(self) -> None:
        if (self.argv is None) == (self.func is None):
            raise ValueError("Action needs exactly one of argv or func")

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "argv": list(self.argv) if self.argv else None,
            "est_bytes": self.est_bytes,
            "undo_note": self.undo_note,
            "requires_root": self.requires_root,
        }


@dataclass
class FixPlan:
    """What a fix intends to do, before it does any of it."""

    fix_id: str
    name: str
    risk: Risk
    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Non-empty when the fix cannot run at all (wrong OS, missing tool,
    #: needs root and we do not have it).
    blocked_reason: str = ""

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reason)

    @property
    def empty(self) -> bool:
        return not self.actions

    @property
    def est_bytes(self) -> int:
        return sum(action.est_bytes for action in self.actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fix_id": self.fix_id,
            "name": self.name,
            "risk": self.risk.label,
            "blocked_reason": self.blocked_reason,
            "est_bytes": self.est_bytes,
            "notes": list(self.notes),
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass
class ActionResult:
    description: str
    ok: bool
    applied: bool
    message: str = ""
    bytes_freed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "ok": self.ok,
            "applied": self.applied,
            "message": self.message,
            "bytes_freed": self.bytes_freed,
        }


@dataclass
class FixResult:
    fix_id: str
    name: str
    applied: bool
    results: list[ActionResult] = field(default_factory=list)
    blocked_reason: str = ""
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def bytes_freed(self) -> int:
        return sum(result.bytes_freed for result in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fix_id": self.fix_id,
            "name": self.name,
            "applied": self.applied,
            "ok": self.ok,
            "bytes_freed": self.bytes_freed,
            "blocked_reason": self.blocked_reason,
            "skipped_reason": self.skipped_reason,
            "results": [result.to_dict() for result in self.results],
        }
