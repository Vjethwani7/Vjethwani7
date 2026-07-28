"""An append-only record of everything medic changed.

If a tool is allowed to modify your system, you should be able to answer
"what did it do, and when" without trusting your memory. Every applied
action lands here as one JSON object per line.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from typing import Any


def journal_path() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "medic", "journal.jsonl")


class Journal:
    """Append-only JSONL audit log."""

    def __init__(self, path: str | None = None, *, enabled: bool = True) -> None:
        self.path = path or journal_path()
        self.enabled = enabled

    def record(self, event: str, **payload: Any) -> None:
        """Append one event. Journal failures never break a run."""
        if not self.enabled:
            return
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "epoch": int(time.time()),
            "event": event,
            "pid": os.getpid(),
            **payload,
        }
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass

    def read(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Most recent entries last. Malformed lines are skipped."""
        entries = list(self._iter())
        if limit is not None and limit > 0:
            entries = entries[-limit:]
        return entries

    def _iter(self) -> Iterator[dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        yield parsed
        except OSError:
            return
