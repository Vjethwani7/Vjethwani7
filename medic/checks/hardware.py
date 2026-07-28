"""Drive health, temperature, and battery condition.

These are the checks that catch problems before they become data loss, so
they are worth the extra dependencies where those exist.
"""

from __future__ import annotations

import glob
import os
import re
from collections.abc import Iterable

from ..core.base import Check
from ..core.context import Context
from ..core.model import Finding
from ..core.registry import register_check


@register_check
class DriveHealth(Check):
    id = "hw.smart"
    name = "Drive health (SMART)"
    description = "Reads the drive's own assessment of whether it is failing."
    category = "hardware"
    platforms = ("linux", "darwin")
    profiles = ("full",)

    def unavailable_reason(self, ctx: Context) -> str:
        reason = super().unavailable_reason(ctx)
        if reason:
            return reason
        if not ctx.has("smartctl"):
            return "needs smartmontools (install it for drive failure warnings)"
        if not ctx.is_root:
            return "needs root to read SMART data"
        if ctx.in_container:
            return "no host drive access from inside a container"
        return ""

    def run(self, ctx: Context) -> Iterable[Finding]:
        devices = self._devices(ctx)
        if not devices:
            yield self.unknown("No drives found to query")
            return

        for device in devices:
            result = ctx.run(["smartctl", "-H", "-A", device], timeout=max(ctx.timeout, 30.0))
            if not result.stdout:
                continue

            healthy = re.search(
                r"(SMART overall-health self-assessment test result|SMART Health Status):\s*(\S+)",
                result.stdout,
            )
            verdict = healthy.group(2) if healthy else ""

            if verdict and verdict.upper() not in ("PASSED", "OK"):
                yield self.critical(
                    f"{device} reports SMART status {verdict}",
                    detail=(
                        "The drive's own diagnostics say it is failing. This is the "
                        "clearest warning you will get before data loss."
                    ),
                    evidence={"device": device, "status": verdict},
                    advice=(
                        "Back up everything on this drive now, then replace it. Do not "
                        "wait for a convenient moment."
                    ),
                )
                continue

            yield from self._attributes(device, result.stdout)

    def _attributes(self, device: str, text: str) -> Iterable[Finding]:
        """Attributes that predict failure even while SMART says PASSED."""
        watch = {
            "Reallocated_Sector_Ct": "sectors have been remapped after write failures",
            "Current_Pending_Sector": "sectors are unreadable and awaiting remap",
            "Offline_Uncorrectable": "sectors could not be read at all",
            "Reported_Uncorrect": "uncorrectable errors have been reported",
        }
        for name, meaning in watch.items():
            match = re.search(rf"^\s*\d+\s+{name}\s+.*?\s(\d+)\s*$", text, re.M)
            if not match:
                continue
            value = int(match.group(1))
            if value > 0:
                yield self.warn(
                    f"{device}: {value} {meaning}",
                    detail=f"SMART attribute {name} = {value}",
                    evidence={"device": device, "attribute": name, "value": value},
                    advice=(
                        "A non-zero and growing count here means the drive is degrading. "
                        "Make sure your backups are current."
                    ),
                )

    def _devices(self, ctx: Context) -> list[str]:
        result = ctx.run(["smartctl", "--scan"])
        devices: list[str] = []
        for line in result.lines():
            if line.startswith("/dev/"):
                devices.append(line.split()[0])
        return devices


@register_check
class Temperature(Check):
    id = "hw.temp"
    name = "Temperature"
    description = "Thermal readings, which explain sudden slowdowns and shutdowns."
    category = "hardware"
    platforms = ("linux",)

    def run(self, ctx: Context) -> Iterable[Finding]:
        readings = self._readings()
        if not readings:
            return

        hottest_name, hottest = max(readings.items(), key=lambda item: item[1])
        config = ctx.config
        detail = "\n".join(f"{name}: {value:.0f} °C" for name, value in sorted(readings.items()))

        if hottest >= config.temp_critical_celsius:
            yield self.critical(
                f"Running very hot ({hottest:.0f} °C at {hottest_name})",
                detail=detail,
                evidence=dict(readings),
                advice=(
                    "At this temperature the CPU throttles and the machine may shut down. "
                    "Check that vents are clear and fans are spinning."
                ),
            )
        elif hottest >= config.temp_warn_celsius:
            yield self.warn(
                f"Temperature is high ({hottest:.0f} °C at {hottest_name})",
                detail=detail,
                evidence=dict(readings),
                advice="Sustained heat causes throttling, which feels like the machine slowing down.",
            )

    def _readings(self) -> dict[str, float]:
        readings: dict[str, float] = {}
        for path in glob.glob("/sys/class/hwmon/hwmon*/temp*_input"):
            try:
                with open(path, encoding="utf-8") as handle:
                    millicelsius = int(handle.read().strip())
            except (OSError, ValueError):
                continue
            celsius = millicelsius / 1000.0
            # Sensors that read absurdly sometimes exist; ignore them.
            if not 0 < celsius < 150:
                continue
            label = self._label(path)
            readings[label] = max(readings.get(label, 0.0), celsius)
        return readings

    def _label(self, path: str) -> str:
        directory = os.path.dirname(path)
        base = os.path.basename(path).replace("_input", "")
        name = ""
        for candidate in (os.path.join(directory, "name"), os.path.join(directory, f"{base}_label")):
            try:
                with open(candidate, encoding="utf-8") as handle:
                    part = handle.read().strip()
                name = f"{name} {part}".strip() if name else part
            except OSError:
                continue
        return name or base


@register_check
class BatteryHealth(Check):
    id = "hw.battery"
    name = "Battery condition"
    description = "Charge capacity relative to when the battery was new."
    category = "hardware"
    platforms = ("linux", "darwin")
    profiles = ("full",)

    def run(self, ctx: Context) -> Iterable[Finding]:
        if ctx.is_linux:
            yield from self._linux(ctx)
        elif ctx.is_macos:
            yield from self._macos(ctx)

    def _linux(self, ctx: Context) -> Iterable[Finding]:
        for base in sorted(glob.glob("/sys/class/power_supply/BAT*")):
            full = self._read_int(os.path.join(base, "energy_full")) or self._read_int(
                os.path.join(base, "charge_full")
            )
            design = self._read_int(os.path.join(base, "energy_full_design")) or self._read_int(
                os.path.join(base, "charge_full_design")
            )
            if not full or not design:
                continue

            health = full / design * 100.0
            name = os.path.basename(base)

            if health < ctx.config.battery_health_warn_percent:
                yield self.warn(
                    f"{name} holds {health:.0f}% of its original charge",
                    detail=(
                        "Battery wear is normal, but below about 70% you will notice "
                        "much shorter runtime and possible unexpected shutdowns."
                    ),
                    evidence={"battery": name, "health_percent": round(health, 1)},
                    advice="Consider a replacement if you rely on running unplugged.",
                )

    def _macos(self, ctx: Context) -> Iterable[Finding]:
        result = ctx.run(["system_profiler", "SPPowerDataType"], timeout=max(ctx.timeout, 30.0))
        if not result.ok:
            return
        condition = re.search(r"Condition:\s*(.+)", result.stdout)
        if condition and condition.group(1).strip() not in ("Normal", ""):
            yield self.warn(
                f"Battery condition: {condition.group(1).strip()}",
                detail="macOS reports this battery as needing attention.",
                evidence={"condition": condition.group(1).strip()},
                advice="Check System Settings > Battery for Apple's recommendation.",
            )

    @staticmethod
    def _read_int(path: str) -> int | None:
        try:
            with open(path, encoding="utf-8") as handle:
                return int(handle.read().strip())
        except (OSError, ValueError):
            return None
