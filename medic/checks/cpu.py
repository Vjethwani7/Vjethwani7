"""CPU load, runaway processes, and stuck process states."""

from __future__ import annotations

from collections.abc import Iterable

from ..core.base import Check
from ..core.context import Context
from ..core.model import Finding
from ..core.probe import processes, system_info, without_self
from ..core.registry import register_check
from ..core.util import human_bytes, truncate


@register_check
class LoadAverage(Check):
    id = "cpu.load"
    name = "CPU load"
    description = "Compares load average against the number of cores."
    category = "cpu"
    platforms = ("linux", "darwin")

    def run(self, ctx: Context) -> Iterable[Finding]:
        info = system_info(ctx)
        if info.load_average is None:
            yield self.unknown("Load average unavailable on this system")
            return

        one, five, fifteen = info.load_average
        cores = info.cpu_count or 1
        # The 5-minute figure is the honest one: the 1-minute average spikes
        # for reasons nobody needs to be told about.
        ratio = five / cores

        detail = (
            f"load {one:.2f} / {five:.2f} / {fifteen:.2f} (1/5/15 min) across {cores} core(s)"
            f"  =  {ratio:.2f}x per core"
        )
        evidence = {
            "load1": one,
            "load5": five,
            "load15": fifteen,
            "cores": cores,
            "ratio": round(ratio, 2),
        }

        if ratio >= ctx.config.load_critical_ratio:
            yield self.critical(
                f"CPU is heavily oversubscribed ({ratio:.1f}x cores)",
                detail=detail + self._top_consumers(ctx),
                evidence=evidence,
                advice="Something is consuming the machine. Check the processes listed above.",
            )
        elif ratio >= ctx.config.load_warn_ratio:
            yield self.warn(
                f"CPU load is elevated ({ratio:.1f}x cores)",
                detail=detail + self._top_consumers(ctx),
                evidence=evidence,
            )

    def _top_consumers(self, ctx: Context) -> str:
        running = processes(ctx)
        if not running:
            return ""
        running.sort(key=lambda process: -process.cpu_percent)
        top = [p for p in running[: ctx.config.top_process_count] if p.cpu_percent > 1.0]
        if not top:
            return ""
        lines = [
            f"{process.cpu_percent:>6.1f}%  pid {process.pid:<7} {truncate(process.command, 52)}"
            for process in top
        ]
        return "\n\nBusiest processes:\n" + "\n".join(lines)


@register_check
class RunawayProcesses(Check):
    id = "cpu.hogs"
    name = "Runaway processes"
    description = "Single processes eating a disproportionate share of CPU or memory."
    category = "cpu"

    def run(self, ctx: Context) -> Iterable[Finding]:
        # Ask for everything first so that "ps produced nothing" stays
        # distinguishable from "the only process was medic itself".
        listed = processes(ctx, exclude_self=False)
        if not listed:
            yield self.unknown("Could not list processes")
            return
        running = without_self(listed)

        config = ctx.config
        for process in sorted(running, key=lambda item: -item.cpu_percent):
            if process.cpu_percent < config.cpu_hog_percent:
                break
            yield self.warn(
                f"{truncate(process.command.split()[0] if process.command else '?', 40)} "
                f"is using {process.cpu_percent:.0f}% CPU",
                detail=f"pid {process.pid}  user {process.user}  {truncate(process.command, 90)}",
                evidence={
                    "pid": process.pid,
                    "cpu_percent": process.cpu_percent,
                    "command": process.command,
                },
                advice=(
                    f"If this is unexpected and unresponsive, stop it with "
                    f"`kill {process.pid}` (or `kill -9 {process.pid}` if it ignores that). "
                    "medic will not kill processes for you - that decision is yours."
                ),
            )

        for process in sorted(running, key=lambda item: -item.mem_percent):
            if process.mem_percent < config.proc_mem_percent:
                break
            yield self.info(
                f"{truncate(process.command.split()[0] if process.command else '?', 40)} "
                f"is holding {process.mem_percent:.0f}% of RAM",
                detail=(
                    f"pid {process.pid}  {human_bytes(process.rss_bytes)} resident  "
                    f"{truncate(process.command, 80)}"
                ),
                evidence={
                    "pid": process.pid,
                    "mem_percent": process.mem_percent,
                    "rss_bytes": process.rss_bytes,
                },
            )


@register_check
class StuckProcesses(Check):
    id = "cpu.stuck"
    name = "Zombie and stuck processes"
    description = "Zombie processes and tasks wedged in uninterruptible I/O wait."
    category = "cpu"
    platforms = ("linux", "darwin")

    def run(self, ctx: Context) -> Iterable[Finding]:
        running = processes(ctx)
        if not running:
            return

        zombies = [p for p in running if p.state.startswith("Z")]
        blocked = [p for p in running if p.state.startswith("D")]

        if len(zombies) > 20:
            parents = sorted({p.ppid for p in zombies})
            yield self.warn(
                f"{len(zombies)} zombie processes",
                detail=(
                    "Zombies are finished processes whose parent never collected the exit "
                    f"status. Parent pids: {', '.join(str(pid) for pid in parents[:10])}"
                ),
                evidence={"count": len(zombies), "parent_pids": parents[:20]},
                advice=(
                    "Zombies use no CPU or memory, but a growing pile means a buggy parent "
                    "process. Restarting the parent clears them."
                ),
            )
        elif zombies:
            yield self.info(
                f"{len(zombies)} zombie process(es)",
                detail="Harmless in this quantity; they consume no resources.",
                evidence={"count": len(zombies)},
            )

        if len(blocked) >= 3:
            names = ", ".join(truncate(p.command, 30) for p in blocked[:5])
            yield self.warn(
                f"{len(blocked)} processes stuck in uninterruptible I/O wait",
                detail=f"Processes: {names}",
                evidence={"count": len(blocked), "pids": [p.pid for p in blocked[:20]]},
                advice=(
                    "This usually means a disk or network filesystem is not responding. "
                    "These processes cannot be killed until the I/O completes. Check disk "
                    "health and any NFS/SMB mounts."
                ),
            )
