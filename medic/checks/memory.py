"""Memory pressure, swap thrashing, and out-of-memory kills."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..core.base import Check
from ..core.context import Context
from ..core.model import Finding
from ..core.probe import memory, processes
from ..core.registry import register_check
from ..core.util import human_bytes, truncate


@register_check
class MemoryPressure(Check):
    id = "mem.pressure"
    name = "Memory usage"
    description = "Reports RAM exhaustion and the processes responsible."
    category = "memory"

    def run(self, ctx: Context) -> Iterable[Finding]:
        stats = memory(ctx)
        if stats is None or not stats.total:
            yield self.unknown("Could not read memory statistics")
            return

        used_percent = stats.percent_used
        detail = (
            f"{human_bytes(stats.used)} used of {human_bytes(stats.total)} "
            f"({used_percent:.0f}%), {human_bytes(stats.available)} available"
        )
        evidence = {
            "total_bytes": stats.total,
            "available_bytes": stats.available,
            "percent_used": round(used_percent, 1),
        }

        if used_percent >= ctx.config.mem_critical_percent:
            yield self.critical(
                f"Memory almost exhausted ({used_percent:.0f}% used)",
                detail=detail + self._top_consumers(ctx),
                evidence=evidence,
                advice=(
                    "Close the largest consumers below. If this keeps happening, the "
                    "kernel will start killing processes."
                ),
            )
        elif used_percent >= ctx.config.mem_warn_percent:
            yield self.warn(
                f"Memory usage is high ({used_percent:.0f}% used)",
                detail=detail + self._top_consumers(ctx),
                evidence=evidence,
            )

    def _top_consumers(self, ctx: Context) -> str:
        running = processes(ctx)
        if not running:
            return ""
        running.sort(key=lambda process: -process.rss_bytes)
        top = [process for process in running[: ctx.config.top_process_count] if process.rss_bytes]
        if not top:
            return ""
        lines = [
            f"{human_bytes(process.rss_bytes):>10}  pid {process.pid:<7} "
            f"{truncate(process.command, 52)}"
            for process in top
        ]
        return "\n\nLargest processes:\n" + "\n".join(lines)


@register_check
class SwapPressure(Check):
    id = "mem.swap"
    name = "Swap usage"
    description = "Heavy swap use is why a machine feels slow rather than busy."
    category = "memory"

    def run(self, ctx: Context) -> Iterable[Finding]:
        stats = memory(ctx)
        if stats is None:
            yield self.unknown("Could not read memory statistics")
            return

        if not stats.swap_total:
            # No swap configured is a legitimate choice, not a problem.
            return

        used_percent = stats.swap_percent_used
        if used_percent < ctx.config.swap_warn_percent:
            return

        yield self.warn(
            f"Swap is {used_percent:.0f}% used",
            detail=(
                f"{human_bytes(stats.swap_used)} of {human_bytes(stats.swap_total)} swap in use. "
                "Heavy swapping makes everything feel slow because the disk is far slower "
                "than RAM."
            ),
            evidence={
                "swap_total_bytes": stats.swap_total,
                "swap_used_bytes": stats.swap_used,
                "percent_used": round(used_percent, 1),
            },
            advice=(
                "Close memory-hungry applications. A reboot clears swap, but if it fills "
                "again you need more RAM or fewer running programs."
            ),
        )


@register_check
class OutOfMemoryKills(Check):
    id = "mem.oom"
    name = "Out-of-memory kills"
    description = "Finds processes the kernel killed because memory ran out."
    category = "memory"
    platforms = ("linux",)
    profiles = ("full",)

    def unavailable_reason(self, ctx: Context) -> str:
        reason = super().unavailable_reason(ctx)
        if reason:
            return reason
        if not ctx.has("journalctl") and not ctx.has("dmesg"):
            return "needs journalctl or dmesg"
        return ""

    def run(self, ctx: Context) -> Iterable[Finding]:
        text = ""
        if ctx.has("journalctl"):
            result = ctx.run(
                ["journalctl", "--no-pager", "--since", "-7 days", "-k", "-p", "warning"],
                timeout=max(ctx.timeout, 25.0),
            )
            if result.ok:
                text = result.stdout
        if not text and ctx.has("dmesg"):
            result = ctx.run(["dmesg"])
            if result.ok:
                text = result.stdout

        if not text:
            yield self.unknown(
                "Could not read the kernel log",
                advice="Kernel logs usually need root; try `sudo medic diagnose`.",
            )
            return

        victims: list[str] = []
        for line in text.splitlines():
            match = re.search(r"Killed process (\d+) \(([^)]+)\)", line)
            if match:
                victims.append(f"{match.group(2)} (pid {match.group(1)})")
            elif "Out of memory: Kill" in line or "oom-kill:" in line:
                match = re.search(r"comm=([^\s,]+)", line)
                if match:
                    victims.append(match.group(1))

        if not victims:
            return

        unique = sorted(set(victims))
        yield self.critical(
            f"The kernel killed {len(victims)} process(es) to reclaim memory",
            detail="Recently killed: " + ", ".join(unique[:10]),
            evidence={"victims": unique, "events": len(victims)},
            advice=(
                "The machine ran out of RAM. Reduce what runs at once, or add memory or "
                "swap. Data loss in the killed applications is possible."
            ),
        )
