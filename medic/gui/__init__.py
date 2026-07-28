"""Desktop application for medic.

Importing this package does not require Tk; :func:`launch` imports it
lazily so that ``medic gui`` can print a helpful message on systems where
tkinter is not installed rather than dying with an ImportError.
"""

from __future__ import annotations


def launch(*, offline: bool = False) -> int:
    """Open the desktop window. Returns a process exit code."""
    from .app import launch as _launch

    return _launch(offline=offline)


def tkinter_available() -> bool:
    """Whether a Tk build is importable in this interpreter."""
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    return True


__all__ = ["launch", "tkinter_available"]
