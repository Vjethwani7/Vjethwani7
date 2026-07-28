"""Small formatting and parsing helpers shared across checks and fixes."""

from __future__ import annotations

import os
import re
import time

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_bytes(num: float | int | None) -> str:
    """Format a byte count for humans. 1536 -> '1.5 KB'."""
    if num is None:
        return "?"
    negative = num < 0
    num = abs(float(num))
    for unit in _UNITS:
        if num < 1024 or unit == _UNITS[-1]:
            text = f"{int(num)} {unit}" if unit == "B" else f"{num:.1f} {unit}"
            return "-" + text if negative else text
        num /= 1024
    return "?"  # unreachable


def human_duration(seconds: float | int | None) -> str:
    """Format a duration. 93784 -> '1d 2h 3m'."""
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    parts: list[str] = []
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def percent(part: float, whole: float) -> float:
    """Percentage, guarding against division by zero."""
    if not whole:
        return 0.0
    return (part / whole) * 100.0


def truncate(text: str, limit: int = 200) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def parse_size(text: str) -> int | None:
    """Parse '1.5G', '512M', '900k' into bytes. Returns None if unparseable."""
    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([kKmMgGtTpP]?)[iI]?[bB]?\s*", text or "")
    if not match:
        return None
    value = float(match.group(1))
    scale = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4, "p": 1024**5}
    return int(value * scale[match.group(2).lower()])


def dir_size(path: str, *, follow_symlinks: bool = False, max_entries: int = 400_000) -> int:
    """Total size of a directory tree, in bytes.

    Symlinks are not followed by default, so a link into / does not make a
    cache directory look like the whole filesystem. Bails out after
    ``max_entries`` files so a pathological tree cannot hang a check.
    """
    total = 0
    seen = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > max_entries:
                        return total
                    try:
                        if entry.is_symlink() and not follow_symlinks:
                            total += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=follow_symlinks):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def age_days(path: str) -> float | None:
    """Days since a path was last modified, or None if it cannot be stat'ed."""
    try:
        mtime = os.lstat(path).st_mtime
    except OSError:
        return None
    return max(0.0, (time.time() - mtime) / 86400.0)


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""
