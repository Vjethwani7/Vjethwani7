"""Discovery and lookup for checks and fixes.

Modules register their classes with the ``@register_check`` /
``@register_fix`` decorators; :func:`load_all` imports every module in the
``checks`` and ``fixes`` packages so that decorators actually fire.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable
from typing import TypeVar

from .base import Check, Fix

_checks: dict[str, Check] = {}
_fixes: dict[str, Fix] = {}
_loaded = False

C = TypeVar("C", bound=Check)
F = TypeVar("F", bound=Fix)


def register_check(cls: type[C]) -> type[C]:
    instance = cls()
    if not instance.id:
        raise ValueError(f"{cls.__name__} is missing an id")
    if instance.id in _checks:
        raise ValueError(f"duplicate check id: {instance.id}")
    _checks[instance.id] = instance
    return cls


def register_fix(cls: type[F]) -> type[F]:
    instance = cls()
    if not instance.id:
        raise ValueError(f"{cls.__name__} is missing an id")
    if instance.id in _fixes:
        raise ValueError(f"duplicate fix id: {instance.id}")
    _fixes[instance.id] = instance
    return cls


def load_all() -> None:
    """Import every check and fix module exactly once."""
    global _loaded
    if _loaded:
        return
    for package_name in ("medic.checks", "medic.fixes"):
        package = importlib.import_module(package_name)
        for module in pkgutil.iter_modules(package.__path__):
            if module.name.startswith("_"):
                continue
            importlib.import_module(f"{package_name}.{module.name}")
    _loaded = True


def all_checks() -> list[Check]:
    load_all()
    return sorted(_checks.values(), key=lambda check: (check.category, check.id))


def all_fixes() -> list[Fix]:
    load_all()
    return sorted(_fixes.values(), key=lambda fix: (fix.risk, fix.category, fix.id))


def get_check(check_id: str) -> Check | None:
    load_all()
    return _checks.get(check_id)


def get_fix(fix_id: str) -> Fix | None:
    load_all()
    return _fixes.get(fix_id)


def resolve(patterns: Iterable[str], items: Iterable[str]) -> list[str]:
    """Expand id patterns against known ids.

    Supports exact ids ('disk.space'), category prefixes ('disk'), and
    globs ('disk.*'). Unmatched patterns are returned by the caller's own
    validation, not silently dropped.
    """
    import fnmatch

    known = list(items)
    matched: list[str] = []
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern:
            continue
        if pattern in known:
            hits = [pattern]
        else:
            hits = [
                item
                for item in known
                if fnmatch.fnmatch(item, pattern) or item.startswith(pattern + ".")
            ]
        for hit in hits:
            if hit not in matched:
                matched.append(hit)
    return matched


def unmatched(patterns: Iterable[str], items: Iterable[str]) -> list[str]:
    """Patterns that matched nothing, so the CLI can complain about typos."""
    known = list(items)
    bad: list[str] = []
    for pattern in patterns:
        if not resolve([pattern], known):
            bad.append(pattern)
    return bad
