"""Background job tracking for long-running dashboard requests.

A full diagnostic run takes several seconds, which is too long to block an
HTTP request without the page looking hung. Requests start a job and poll
it, so the browser can show real progress ("checking 12 of 28") instead of
an indeterminate spinner.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

RUNNING = "running"
DONE = "done"
ERROR = "error"


@dataclass
class Job:
    id: str
    kind: str
    status: str = RUNNING
    current: int = 0
    total: int = 0
    label: str = ""
    result: Any = None
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": {"current": self.current, "total": self.total, "label": self.label},
            "result": self.result,
            "error": self.error,
            "elapsed_ms": int(((self.finished_at or time.time()) - self.started_at) * 1000),
        }


class JobRegistry:
    """Thread-safe store of running and completed jobs.

    Completed jobs are kept briefly so a poll that arrives just after
    completion still finds the result, then reaped to bound memory.
    """

    def __init__(self, *, retain_seconds: float = 600.0, max_jobs: int = 50) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.retain_seconds = retain_seconds
        self.max_jobs = max_jobs

    def start(self, kind: str, work: Callable[[Job], Any]) -> Job:
        """Run ``work`` on a background thread, tracked as a job."""
        job = Job(id=uuid.uuid4().hex, kind=kind)

        with self._lock:
            self._reap_locked()
            self._jobs[job.id] = job

        def runner() -> None:
            try:
                job.result = work(job)
                job.status = DONE
            except Exception as exc:
                job.status = ERROR
                job.error = f"{type(exc).__name__}: {exc}"
                job.result = {"traceback": traceback.format_exc()}
            finally:
                job.finished_at = time.time()

        thread = threading.Thread(target=runner, name=f"medic-{kind}", daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _reap_locked(self) -> None:
        cutoff = time.time() - self.retain_seconds
        stale = [
            job_id
            for job_id, job in self._jobs.items()
            if job.finished_at and job.finished_at < cutoff
        ]
        for job_id in stale:
            del self._jobs[job_id]

        if len(self._jobs) > self.max_jobs:
            finished = sorted(
                (job for job in self._jobs.values() if job.finished_at),
                key=lambda job: job.finished_at,
            )
            for job in finished[: len(self._jobs) - self.max_jobs]:
                self._jobs.pop(job.id, None)
