"""User-tunable thresholds.

Defaults are chosen to be quiet on a healthy machine: a check that fires on
every laptop is noise, and noise is what makes people stop reading
diagnostics. Override in ``~/.config/medic/config.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from typing import Any


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "medic", "config.json")


@dataclass
class Config:
    """Thresholds and limits. All sizes in bytes, all ages in days."""

    # Disk
    disk_warn_percent: float = 85.0
    disk_critical_percent: float = 93.0
    disk_min_free_bytes: int = 2 * 1024**3
    inode_warn_percent: float = 85.0
    inode_critical_percent: float = 95.0

    # Memory
    mem_warn_percent: float = 88.0
    mem_critical_percent: float = 96.0
    swap_warn_percent: float = 60.0

    # CPU
    load_warn_ratio: float = 1.5  # load average per core
    load_critical_ratio: float = 3.0
    cpu_hog_percent: float = 60.0
    proc_mem_percent: float = 20.0
    top_process_count: int = 5

    # Logs and caches
    log_dir_warn_bytes: int = 2 * 1024**3
    journal_warn_bytes: int = 1024**3
    journal_vacuum_target: str = "500M"
    cache_stale_days: float = 30.0
    tmp_stale_days: float = 7.0
    trash_stale_days: float = 30.0

    # Uptime and updates
    uptime_warn_days: float = 45.0
    updates_warn_count: int = 25
    security_updates_warn_count: int = 1

    # Thermals and battery
    temp_warn_celsius: float = 85.0
    temp_critical_celsius: float = 95.0
    battery_health_warn_percent: float = 70.0

    # General
    command_timeout: float = 15.0
    slow_check_ms: int = 3000

    @classmethod
    def load(cls, path: str | None = None) -> Config:
        """Load config from disk, falling back to defaults.

        Unknown keys are ignored rather than fatal, so a config written by a
        newer medic still works with an older one.
        """
        path = path or config_path()
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {field.name: field.type for field in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            try:
                if known[key] is float or known[key] == "float":
                    kwargs[key] = float(value)
                elif known[key] is int or known[key] == "int":
                    kwargs[key] = int(value)
                else:
                    kwargs[key] = value
            except (TypeError, ValueError):
                continue
        return cls(**kwargs)

    def save(self, path: str | None = None) -> str:
        path = path or config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(asdict(self), handle, indent=2, sort_keys=True)
            handle.write("\n")
        return path

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
