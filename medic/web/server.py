"""A local HTTP server exposing medic's diagnostics to a browser.

Threat model
------------
This server can, if permitted, change the machine it runs on. That makes
it a more interesting target than a normal web app, so:

* it binds to the loopback interface, and refuses any other bind address
  unless the caller explicitly insists;
* every API call must carry a session token minted at startup;
* the ``Host`` header is validated, which is what stops a malicious page
  in your browser from reaching the server via DNS rebinding;
* state-changing requests must carry a same-origin ``Origin`` header;
* applying repairs is disabled unless the server was started with
  ``allow_fixes``. Previewing is always available, because previews cannot
  change anything.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..core import registry
from ..core.config import Config
from ..core.context import Context
from ..core.journal import Journal
from ..core.runner import apply_plan, plan_fix, run_checks
from ..core.util import human_bytes
from .jobs import JobRegistry

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: Host header values that are unambiguously this machine.
LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})

MAX_BODY_BYTES = 64 * 1024


class DashboardServer:
    """Holds the state shared by every request handler."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        allow_fixes: bool = False,
        config: Config | None = None,
        offline: bool = False,
        token: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.allow_fixes = allow_fixes
        self.offline = offline
        self.config = config or Config.load()
        self.token = token or secrets.token_urlsafe(24)
        self.jobs = JobRegistry()
        self.journal = Journal()
        self._httpd: ThreadingHTTPServer | None = None
        self._lock = threading.Lock()

    # -- context ----------------------------------------------------------

    def make_context(self, *, dry_run: bool = True) -> Context:
        ctx = Context.detect()
        ctx.config = self.config
        ctx.offline = self.offline
        ctx.timeout = self.config.command_timeout
        ctx.journal = self.journal
        ctx.dry_run = dry_run
        return ctx

    # -- lifecycle --------------------------------------------------------

    def build(self) -> ThreadingHTTPServer:
        server = self  # captured by the handler below

        class Handler(DashboardHandler):
            app = server

        httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        httpd.daemon_threads = True
        # Port 0 means "pick one for me"; record what we actually got.
        self.port = httpd.server_address[1]
        self._httpd = httpd
        return httpd

    @property
    def url(self) -> str:
        host = "localhost" if self.host in ("127.0.0.1", "0.0.0.0", "::1") else self.host
        return f"http://{host}:{self.port}/?token={self.token}"

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


class DashboardHandler(BaseHTTPRequestHandler):
    """Request handler. ``app`` is injected by :meth:`DashboardServer.build`."""

    app: DashboardServer
    server_version = "medic"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        """Silence per-request logging; the dashboard is not a web host."""

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The dashboard is entirely self-contained; forbid outside resources
        # so a compromised dependency cannot exfiltrate a diagnostic report.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("body is not valid JSON") from exc
        return parsed if isinstance(parsed, dict) else {}

    # -- security ---------------------------------------------------------

    def _host_is_local(self) -> bool:
        """Reject requests whose Host header is not this server.

        Without this, a page on any website could point a hostname at
        127.0.0.1 and drive the dashboard from the victim's browser.
        """
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return False
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name in LOCAL_HOSTNAMES or name == self.app.host

    def _origin_ok(self) -> bool:
        """Same-origin check for state-changing requests."""
        origin = self.headers.get("Origin")
        if not origin:
            # Browsers always send Origin on cross-origin requests, so a
            # missing header means a same-origin fetch or a local tool.
            return True
        parsed = urlparse(origin)
        return parsed.hostname in LOCAL_HOSTNAMES or parsed.hostname == self.app.host

    def _token_ok(self, query: dict[str, list[str]]) -> bool:
        supplied = self.headers.get("X-Medic-Token") or (query.get("token") or [""])[0]
        # Constant-time comparison: the token is the only thing standing
        # between a local process and this API.
        return bool(supplied) and secrets.compare_digest(supplied, self.app.token)

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        if not self._host_is_local():
            self._error(HTTPStatus.FORBIDDEN, "invalid Host header")
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if not path.startswith("/api/"):
            if method != "GET":
                self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
                return
            self._serve_static(path)
            return

        if method == "POST" and not self._origin_ok():
            self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
            return

        if not self._token_ok(query):
            self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid session token")
            return

        try:
            self._route_api(method, path)
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # never leak a traceback to the browser
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def _route_api(self, method: str, path: str) -> None:
        app = self.app

        if path == "/api/meta" and method == "GET":
            self._json(HTTPStatus.OK, self._meta())
            return

        if path == "/api/diagnose" and method == "POST":
            body = self._read_body()
            job = app.jobs.start("diagnose", self._diagnose_work(body))
            self._json(HTTPStatus.ACCEPTED, job.to_dict())
            return

        if path.startswith("/api/job/") and method == "GET":
            job = app.jobs.get(path.rsplit("/", 1)[-1])
            if job is None:
                self._error(HTTPStatus.NOT_FOUND, "unknown job")
                return
            self._json(HTTPStatus.OK, job.to_dict())
            return

        if path == "/api/fix/preview" and method == "POST":
            body = self._read_body()
            self._json(HTTPStatus.OK, self._preview(body.get("fix_ids") or []))
            return

        if path == "/api/fix/apply" and method == "POST":
            if not app.allow_fixes:
                self._error(
                    HTTPStatus.FORBIDDEN,
                    "this dashboard is read-only; restart with `medic serve --allow-fixes` "
                    "to apply repairs",
                )
                return
            body = self._read_body()
            job = app.jobs.start("apply", self._apply_work(body.get("fix_ids") or []))
            self._json(HTTPStatus.ACCEPTED, job.to_dict())
            return

        if path == "/api/history" and method == "GET":
            self._json(HTTPStatus.OK, {"entries": app.journal.read(limit=50)})
            return

        self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    # -- endpoint implementations -----------------------------------------

    def _meta(self) -> dict:
        app = self.app
        ctx = app.make_context()
        return {
            "version": __import__("medic").__version__,
            "host": ctx.hostname,
            "platform": ctx.describe(),
            "user": ctx.user,
            "is_root": ctx.is_root,
            "allow_fixes": app.allow_fixes,
            "offline": app.offline,
            "checks": [
                {
                    "id": check.id,
                    "name": check.name,
                    "description": check.description,
                    "category": check.category,
                    "supported": not check.unavailable_reason(ctx),
                }
                for check in registry.all_checks()
            ],
            "fixes": [
                {
                    "id": fix.id,
                    "name": fix.name,
                    "description": fix.description,
                    "category": fix.category,
                    "risk": fix.risk.label,
                    "requires_root": fix.requires_root,
                    "addresses": list(fix.addresses),
                    "available": not fix.unavailable_reason(ctx),
                    "unavailable_reason": fix.unavailable_reason(ctx),
                }
                for fix in registry.all_fixes()
            ],
        }

    def _diagnose_work(self, body: dict):
        profile = body.get("profile") if body.get("profile") in ("quick", "full") else "full"
        only = [item for item in (body.get("only") or []) if isinstance(item, str)]
        app = self.app

        def work(job):
            ctx = app.make_context()
            checks = registry.all_checks()
            if only:
                wanted = set(registry.resolve(only, [check.id for check in checks]))
                checks = [check for check in checks if check.id in wanted]
            else:
                checks = [check for check in checks if profile in check.profiles]

            def on_start(check, index: int, total: int) -> None:
                job.current, job.total, job.label = index + 1, total, check.name

            diagnosis = run_checks(ctx, checks, on_start=on_start)
            return diagnosis.to_dict()

        return work

    def _preview(self, fix_ids: list[str]) -> dict:
        ctx = self.app.make_context(dry_run=True)
        known = [fix.id for fix in registry.all_fixes()]
        wanted = registry.resolve([f for f in fix_ids if isinstance(f, str)], known)

        plans = []
        for fix_id in wanted:
            fix = registry.get_fix(fix_id)
            if fix is None:
                continue
            plan = plan_fix(ctx, fix)
            payload = plan.to_dict()
            payload["est_bytes_human"] = human_bytes(plan.est_bytes) if plan.est_bytes else ""
            payload["description"] = fix.description
            plans.append(payload)

        return {"plans": plans}

    def _apply_work(self, fix_ids: list[str]):
        app = self.app
        known = [fix.id for fix in registry.all_fixes()]
        wanted = registry.resolve([f for f in fix_ids if isinstance(f, str)], known)

        def work(job):
            ctx = app.make_context(dry_run=False)
            results = []
            job.total = len(wanted)

            for index, fix_id in enumerate(wanted):
                fix = registry.get_fix(fix_id)
                if fix is None:
                    continue
                job.current, job.label = index + 1, fix.name
                plan = plan_fix(ctx, fix)
                # The browser has already shown this plan and the user
                # clicked apply, so confirm=None is the consent we have.
                result = apply_plan(ctx, fix, plan, dry_run=False, confirm=None)
                payload = result.to_dict()
                payload["bytes_freed_human"] = (
                    human_bytes(result.bytes_freed) if result.bytes_freed else ""
                )
                results.append(payload)

            return {"results": results}

        return work

    # -- static files -----------------------------------------------------

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = os.path.normpath(os.path.join(STATIC_DIR, relative))

        # Refuse anything that escapes the static directory.
        if os.path.commonpath([os.path.realpath(target), os.path.realpath(STATIC_DIR)]) != (
            os.path.realpath(STATIC_DIR)
        ):
            self._error(HTTPStatus.FORBIDDEN, "forbidden")
            return

        if not os.path.isfile(target):
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return

        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "could not read file")
            return

        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, body, content_type)


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_fixes: bool = False,
    offline: bool = False,
    open_browser: bool = False,
    config: Config | None = None,
    on_ready=None,
) -> None:
    """Run the dashboard until interrupted."""
    app = DashboardServer(
        host=host, port=port, allow_fixes=allow_fixes, offline=offline, config=config
    )
    httpd = app.build()

    if on_ready:
        on_ready(app)

    if open_browser:
        import webbrowser

        threading.Timer(0.4, lambda: webbrowser.open(app.url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()


__all__ = ["DashboardServer", "DashboardHandler", "serve"]
