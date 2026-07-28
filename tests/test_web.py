"""Tests for the dashboard server.

A real server is started on an ephemeral port and driven over HTTP, because
the interesting behaviour here (token checks, Host validation, static file
containment) lives in the request path, not in a function that can be
called directly.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from medic.web.jobs import DONE, ERROR, JobRegistry
from medic.web.server import DashboardServer


@pytest.fixture
def server():
    """A read-only dashboard on a random free port."""
    app = DashboardServer(host="127.0.0.1", port=0, offline=True)
    httpd = app.build()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield app
    app.shutdown()


@pytest.fixture
def writable_server(tmp_path, monkeypatch):
    """A dashboard permitted to apply repairs, rooted in a throwaway home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    app = DashboardServer(host="127.0.0.1", port=0, offline=True, allow_fixes=True)
    httpd = app.build()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield app
    app.shutdown()


def request(app, path, *, method="GET", body=None, token=None, headers=None):
    """Make an HTTP call, returning (status, parsed_body)."""
    url = f"http://127.0.0.1:{app.port}{path}"
    data = json.dumps(body).encode() if body is not None else None

    req = urllib.request.Request(url, data=data, method=method)
    supplied = app.token if token is None else token
    if supplied:
        req.add_header("X-Medic-Token", supplied)
    if data:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            return response.status, _maybe_json(raw)
    except urllib.error.HTTPError as exc:
        return exc.code, _maybe_json(exc.read())


def _maybe_json(raw: bytes):
    try:
        return json.loads(raw.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw


def wait_for_job(app, job_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload = request(app, f"/api/job/{job_id}")
        assert status == 200
        if payload["status"] != "running":
            return payload
        time.sleep(0.1)
    raise AssertionError("job did not finish in time")


class TestAuthentication:
    def test_api_requires_a_token(self, server):
        status, payload = request(server, "/api/meta", token="")
        assert status == 401
        assert "token" in payload["error"]

    def test_wrong_token_is_rejected(self, server):
        status, _ = request(server, "/api/meta", token="not-the-token")
        assert status == 401

    def test_correct_token_is_accepted(self, server):
        status, payload = request(server, "/api/meta")
        assert status == 200
        assert payload["host"]

    def test_token_also_works_as_a_query_parameter(self, server):
        """The browser lands on the page with the token in the URL."""
        status, _ = request(server, f"/api/meta?token={server.token}", token="")
        assert status == 200

    def test_tokens_are_unpredictable(self):
        tokens = {DashboardServer(port=0).token for _ in range(20)}
        assert len(tokens) == 20
        assert all(len(token) >= 20 for token in tokens)


class TestHostValidation:
    def test_foreign_host_header_is_refused(self, server):
        """This is the DNS-rebinding defence: a hostname resolving to
        127.0.0.1 must not be able to drive the dashboard."""
        status, payload = request(
            server, "/api/meta", headers={"Host": "attacker.example.com"}
        )
        assert status == 403
        assert "Host" in payload["error"]

    def test_localhost_is_accepted(self, server):
        status, _ = request(server, "/api/meta", headers={"Host": f"localhost:{server.port}"})
        assert status == 200

    def test_static_files_are_also_host_checked(self, server):
        status, _ = request(server, "/", headers={"Host": "attacker.example.com"})
        assert status == 403


class TestOriginValidation:
    def test_cross_origin_post_is_refused(self, server):
        status, payload = request(
            server,
            "/api/diagnose",
            method="POST",
            body={"profile": "quick"},
            headers={"Origin": "https://attacker.example.com"},
        )
        assert status == 403
        assert "cross-origin" in payload["error"]

    def test_same_origin_post_is_allowed(self, server):
        status, _ = request(
            server,
            "/api/diagnose",
            method="POST",
            body={"profile": "quick"},
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
        )
        assert status == 202

    def test_get_requests_do_not_need_an_origin(self, server):
        status, _ = request(server, "/api/meta")
        assert status == 200


class TestStaticFiles:
    def test_serves_the_dashboard(self, server):
        status, body = request(server, "/")
        assert status == 200
        assert b"<title>medic</title>" in body

    def test_serves_css_and_js(self, server):
        for path in ("/style.css", "/app.js"):
            status, body = request(server, path)
            assert status == 200, path
            assert body

    def test_directory_traversal_is_blocked(self, server):
        status, payload = request(server, "/../../../../etc/passwd")
        assert status == 403
        assert b"root:" not in json.dumps(payload).encode()

    def test_unknown_file_is_a_404(self, server):
        status, _ = request(server, "/nope.txt")
        assert status == 404

    def test_static_paths_do_not_need_a_token(self, server):
        """The page itself must load so it can then authenticate."""
        status, _ = request(server, "/", token="")
        assert status == 200

    def test_a_restrictive_csp_is_sent(self, server):
        url = f"http://127.0.0.1:{server.port}/"
        with urllib.request.urlopen(url, timeout=10) as response:
            csp = response.headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp
        # No outbound connections: a diagnostic report must not be able to
        # leave the machine even if the page were compromised.
        assert "connect-src 'self'" in csp


class TestMeta:
    def test_lists_checks_and_fixes(self, server):
        _status, payload = request(server, "/api/meta")
        check_ids = {check["id"] for check in payload["checks"]}
        fix_ids = {fix["id"] for fix in payload["fixes"]}

        assert "disk.space" in check_ids
        assert "clean.user-cache" in fix_ids

    def test_reports_read_only_mode(self, server):
        _status, payload = request(server, "/api/meta")
        assert payload["allow_fixes"] is False

    def test_every_fix_carries_a_risk_rating(self, server):
        _status, payload = request(server, "/api/meta")
        for fix in payload["fixes"]:
            assert fix["risk"] in ("safe", "moderate", "risky")


class TestDiagnose:
    def test_runs_and_returns_a_diagnosis(self, server):
        status, job = request(server, "/api/diagnose", method="POST", body={"profile": "quick"})
        assert status == 202

        finished = wait_for_job(server, job["id"])
        assert finished["status"] == DONE
        assert finished["result"]["schema"] == 1
        assert finished["result"]["counts"]["checks_run"] > 0

    def test_reports_progress_while_running(self, server):
        _status, job = request(server, "/api/diagnose", method="POST", body={"profile": "quick"})
        finished = wait_for_job(server, job["id"])
        assert finished["progress"]["total"] > 0

    def test_can_run_a_single_check(self, server):
        _status, job = request(
            server, "/api/diagnose", method="POST", body={"only": ["disk.space"]}
        )
        finished = wait_for_job(server, job["id"])
        assert [r["check_id"] for r in finished["result"]["reports"]] == ["disk.space"]

    def test_unknown_job_is_a_404(self, server):
        status, _ = request(server, "/api/job/does-not-exist")
        assert status == 404


class TestPreview:
    def test_preview_returns_plans_without_changing_anything(self, server, tmp_path):
        status, payload = request(
            server, "/api/fix/preview", method="POST", body={"fix_ids": ["clean.user-cache"]}
        )
        assert status == 200
        assert payload["plans"]
        assert payload["plans"][0]["fix_id"] == "clean.user-cache"

    def test_preview_works_even_in_read_only_mode(self, server):
        """Previews cannot change anything, so they are never gated."""
        status, _ = request(
            server, "/api/fix/preview", method="POST", body={"fix_ids": ["clean.tmp"]}
        )
        assert status == 200

    def test_unknown_fix_ids_are_ignored(self, server):
        status, payload = request(
            server, "/api/fix/preview", method="POST", body={"fix_ids": ["no.such.fix"]}
        )
        assert status == 200
        assert payload["plans"] == []


class TestApplyGating:
    def test_apply_is_refused_in_read_only_mode(self, server):
        status, payload = request(
            server, "/api/fix/apply", method="POST", body={"fix_ids": ["clean.user-cache"]}
        )
        assert status == 403
        assert "read-only" in payload["error"]

    def test_apply_works_when_permitted(self, writable_server, tmp_path):
        """The full browser path: a stale cache is removed, a fresh one is not."""
        import os

        cache = tmp_path / ".cache"
        (cache / "stale").mkdir(parents=True)
        (cache / "stale" / "blob.bin").write_bytes(b"x" * 4096)
        (cache / "fresh").mkdir(parents=True)

        long_ago = time.time() - (90 * 86400)
        os.utime(cache / "stale", (long_ago, long_ago))

        status, job = request(
            writable_server,
            "/api/fix/apply",
            method="POST",
            body={"fix_ids": ["clean.user-cache"]},
        )
        assert status == 202

        finished = wait_for_job(writable_server, job["id"])
        assert finished["status"] == DONE
        assert not (cache / "stale").exists()
        assert (cache / "fresh").exists(), "an active cache must not be removed"

    def test_applying_is_journalled(self, writable_server, tmp_path):
        import os

        cache = tmp_path / ".cache" / "stale"
        cache.mkdir(parents=True)
        (cache / "f").write_bytes(b"x" * 1024)
        long_ago = time.time() - (90 * 86400)
        os.utime(cache, (long_ago, long_ago))

        _status, job = request(
            writable_server,
            "/api/fix/apply",
            method="POST",
            body={"fix_ids": ["clean.user-cache"]},
        )
        wait_for_job(writable_server, job["id"])

        _status, history = request(writable_server, "/api/history")
        events = [entry.get("event") for entry in history["entries"]]
        assert "fix.begin" in events and "fix.end" in events


class TestMalformedRequests:
    def test_bad_json_is_a_400(self, server):
        url = f"http://127.0.0.1:{server.port}/api/diagnose"
        req = urllib.request.Request(url, data=b"{not json", method="POST")
        req.add_header("X-Medic-Token", server.token)
        req.add_header("Content-Type", "application/json")

        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("should have failed")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400

    def test_oversized_body_is_rejected(self, server):
        status, _ = request(
            server, "/api/fix/preview", method="POST", body={"fix_ids": ["x" * 100_000]}
        )
        assert status == 400

    def test_unknown_endpoint_is_a_404(self, server):
        status, _ = request(server, "/api/nonsense")
        assert status == 404

    def test_post_to_a_static_path_is_rejected(self, server):
        status, _ = request(server, "/style.css", method="POST", body={})
        assert status == 405


class TestJobRegistry:
    def test_successful_job_records_its_result(self):
        registry = JobRegistry()
        job = registry.start("test", lambda j: {"value": 42})

        for _ in range(100):
            if job.status != "running":
                break
            time.sleep(0.02)

        assert job.status == DONE
        assert job.result == {"value": 42}

    def test_failing_job_is_captured_not_raised(self):
        registry = JobRegistry()

        def boom(job):
            raise RuntimeError("nope")

        job = registry.start("test", boom)
        for _ in range(100):
            if job.status != "running":
                break
            time.sleep(0.02)

        assert job.status == ERROR
        assert "nope" in job.error

    def test_finished_jobs_are_eventually_reaped(self):
        registry = JobRegistry(retain_seconds=0.0)
        first = registry.start("test", lambda j: None)
        for _ in range(100):
            if first.status != "running":
                break
            time.sleep(0.02)

        registry.start("test", lambda j: None)  # triggers a reap
        assert registry.get(first.id) is None

    def test_unknown_job_id_returns_none(self):
        assert JobRegistry().get("nope") is None


class TestServerConfig:
    def test_url_contains_the_token(self):
        app = DashboardServer(port=0)
        assert app.token in app.url

    def test_defaults_to_loopback_and_read_only(self):
        app = DashboardServer()
        assert app.host == "127.0.0.1"
        assert app.allow_fixes is False
