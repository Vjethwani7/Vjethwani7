"""Base classes every check and fix inherits from."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .context import Context
from .model import Action, Finding, FixPlan, Risk, Severity

ALL_PLATFORMS = ("linux", "darwin", "windows")


class Check:
    """A read-only inspection of some part of the system.

    Checks must never modify anything. They return zero or more
    :class:`Finding` objects; returning none means "looks healthy".
    """

    id: str = ""
    name: str = ""
    description: str = ""
    category: str = "system"
    platforms: tuple[str, ...] = ALL_PLATFORMS
    #: 'quick' checks must be fast and dependency-free; 'full' may be slower.
    profiles: tuple[str, ...] = ("quick", "full")

    def unavailable_reason(self, ctx: Context) -> str:
        """Return why this check cannot run here, or '' if it can.

        Subclasses override to add their own requirements (a missing tool,
        a container with no host visibility) and should call ``super()``.
        """
        if ctx.os_name not in self.platforms:
            return f"not supported on {ctx.os_name}"
        return ""

    def run(self, ctx: Context) -> Iterable[Finding]:
        raise NotImplementedError

    # -- finding constructors ---------------------------------------------

    def _finding(self, severity: Severity, title: str, **kwargs: Any) -> Finding:
        return Finding(check_id=self.id, title=title, severity=severity, **kwargs)

    def info(self, title: str, **kwargs: Any) -> Finding:
        return self._finding(Severity.INFO, title, **kwargs)

    def warn(self, title: str, **kwargs: Any) -> Finding:
        return self._finding(Severity.WARN, title, **kwargs)

    def critical(self, title: str, **kwargs: Any) -> Finding:
        return self._finding(Severity.CRITICAL, title, **kwargs)

    def unknown(self, title: str, **kwargs: Any) -> Finding:
        return self._finding(Severity.UNKNOWN, title, **kwargs)


class Fix:
    """A repair that can be previewed before it is applied.

    The contract is that :meth:`plan` is pure: it may read the system and
    run read-only commands, but it must not change anything. All mutation
    happens when the executor runs the returned actions.
    """

    id: str = ""
    name: str = ""
    description: str = ""
    category: str = "system"
    risk: Risk = Risk.SAFE
    requires_root: bool = False
    platforms: tuple[str, ...] = ALL_PLATFORMS
    #: Check ids whose findings this fix addresses, for cross-referencing.
    addresses: tuple[str, ...] = ()

    def unavailable_reason(self, ctx: Context) -> str:
        if ctx.os_name not in self.platforms:
            return f"not supported on {ctx.os_name}"
        if self.requires_root and not ctx.is_root:
            return "needs root; re-run this fix with sudo"
        return ""

    def plan(self, ctx: Context) -> FixPlan:
        raise NotImplementedError

    # -- plan constructors -------------------------------------------------

    def new_plan(self, **kwargs: Any) -> FixPlan:
        return FixPlan(fix_id=self.id, name=self.name, risk=self.risk, **kwargs)

    def blocked(self, reason: str) -> FixPlan:
        return self.new_plan(blocked_reason=reason)

    @staticmethod
    def action(description: str, **kwargs: Any) -> Action:
        return Action(description=description, **kwargs)
