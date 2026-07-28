"""Local web dashboard for medic.

The server binds to the loopback interface, mints a single-use session
token, and refuses to apply repairs unless explicitly permitted. See
:mod:`medic.web.server`.
"""

from .server import DashboardServer, serve

__all__ = ["DashboardServer", "serve"]
