"""Log volume and recent system errors."""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Iterable

from ..core.base import Check
from ..core.context import Context
from ..core.model import Finding
from ..core.probe import journal_bytes
from ..core.registry import register_check
from ..core.util import dir_size, human_bytes, truncate


@register_check
class LogVolume(Check):
    id = "logs.size"
    name = "Log storage"
    description = "Logs that have grown large enough to matter."
    category = "logs"
    platforms = ("linux", "darwin")

    def run(self, ctx: Context) -> Iterable[Finding]:
        size = journal_bytes(ctx)
        if size and size > ctx.config.journal_warn_bytes:
            yield self.warn(
                f"systemd journal is using {human_bytes(size)}",
                detail="Old entries can be discarded without affecting anything running.",
                evidence={"bytes": size},
                fix_ids=["clean.journal"],
            )

        log_dir = "/var/log"
        if os.path.isdir(log_dir):
            size = dir_size(log_dir)
            if size > ctx.config.log_dir_warn_bytes:
                yield self.warn(
                    f"/var/log is using {human_bytes(size)}",
                    detail=self._largest(log_dir),
                    evidence={"bytes": size},
                    fix_ids=["clean.journal"],
                    advice=(
                        "Check whether one service is logging excessively - that is usually "
                        "a symptom worth investigating, not just disk to reclaim."
                    ),
                )

    def _largest(self, root: str, count: int = 5) -> str:
        entries: list[tuple[str, int]] = []
        try:
            for entry in os.scandir(root):
                try:
                    size = (
                        dir_size(entry.path)
                        if entry.is_dir(follow_symlinks=False)
                        else entry.stat(follow_symlinks=False).st_size
                    )
                except OSError:
                    continue
                entries.append((entry.path, size))
        except OSError:
            return ""
        entries.sort(key=lambda item: -item[1])
        return "\n".join(f"{human_bytes(size):>10}  {path}" for path, size in entries[:count])


@register_check
class RecentErrors(Check):
    id = "logs.errors"
    name = "Recent system errors"
    description = "Repeated error-level messages from the last day."
    category = "logs"
    platforms = ("linux",)

    #: Messages that are noisy on healthy systems and mean nothing to a user.
    IGNORE = (
        "Failed to open VDPAU backend",
        "ACPI BIOS Error",
        "thermal_sys",
        "i8042",
        "Bluetooth: hci0: Opcode",
        "systemd-journald.service: Deactivated successfully",
    )

    def unavailable_reason(self, ctx: Context) -> str:
        reason = super().unavailable_reason(ctx)
        if reason:
            return reason
        if not ctx.has("journalctl"):
            return "needs journalctl"
        return ""

    def run(self, ctx: Context) -> Iterable[Finding]:
        result = ctx.run(
            ["journalctl", "--no-pager", "-p", "err", "--since", "-24 hours", "-o", "short"],
            timeout=max(ctx.timeout, 25.0),
        )
        if not result.ok:
            yield self.unknown(
                "Could not read the system journal",
                detail=result.failure_reason,
                advice="Reading other users' logs needs root; try `sudo medic diagnose`.",
            )
            return

        messages = Counter()
        for line in result.lines():
            # Drop the timestamp/host/unit prefix; keep the message text.
            message = re.sub(r"^\w{3}\s+\d+\s[\d:]+\s+\S+\s+", "", line)
            message = re.sub(r"\[\d+\]", "[]", message)
            if any(pattern in message for pattern in self.IGNORE):
                continue
            if message.strip():
                messages[self._normalise(message)] += 1

        if not messages:
            return

        repeated = [(text, count) for text, count in messages.most_common(5) if count >= 3]
        if not repeated:
            total = sum(messages.values())
            if total >= 20:
                yield self.info(
                    f"{total} error-level log entries in the last 24 hours",
                    detail="\n".join(truncate(text, 100) for text, _ in messages.most_common(3)),
                    evidence={"total": total},
                )
            return

        detail = "\n".join(f"{count:>5}x  {truncate(text, 95)}" for text, count in repeated)
        yield self.warn(
            "Repeated system errors in the last 24 hours",
            detail=detail,
            evidence={"top_errors": dict(repeated)},
            advice=(
                "A message repeating dozens of times is usually the root cause of whatever "
                "else is misbehaving. Search the exact text before changing anything."
            ),
        )

    @staticmethod
    def _normalise(message: str) -> str:
        """Collapse variable parts so repeats of one error group together."""
        message = re.sub(r"\b\d+\b", "N", message)
        message = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", message)
        message = re.sub(r"/dev/\w+", "/dev/DEV", message)
        return message.strip()
