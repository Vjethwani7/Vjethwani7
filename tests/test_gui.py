"""Tests for the desktop app.

The controller and theme layers carry the logic and are deliberately free
of Tk, so they are tested directly. The window itself is only smoke-tested,
and only where a Tk build and a display both exist - which is why that
logic lives outside the widgets in the first place.
"""

from __future__ import annotations

import os
import time

import pytest

from medic.gui import controller as ctrl
from medic.gui import tkinter_available
from medic.gui.theme import DARK, LIGHT, detect_palette, mono_font, ui_font


def drain_until(controller, kind, timeout=90.0):
    """Collect events until one of ``kind`` arrives; return (event, all).

    The whole batch is kept even after the match is found - drain() has
    already taken those events off the queue, so returning early would
    silently lose any that followed the one being waited for.
    """
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        batch = controller.drain()
        seen.extend(batch)
        match = next((event for event in batch if event.kind == kind), None)
        if match is not None:
            return match, seen
        time.sleep(0.05)
    raise AssertionError(f"no {kind} event within {timeout}s (saw {[e.kind for e in seen]})")


@pytest.fixture
def controller():
    return ctrl.Controller(offline=True)


class TestControllerBasics:
    def test_describes_the_system(self, controller):
        described = controller.describe_system()
        assert described["host"]
        assert "platform" in described
        assert isinstance(described["is_root"], bool)

    def test_lists_fixes_with_availability(self, controller):
        fixes = controller.fixes()
        assert fixes

        by_id = {fix["id"]: fix for fix in fixes}
        assert "clean.user-cache" in by_id

        for fix in fixes:
            assert fix["risk"] in ("safe", "moderate", "risky")
            assert isinstance(fix["available"], bool)
            # An unavailable fix must explain itself.
            if not fix["available"]:
                assert fix["reason"]

    def test_starts_idle(self, controller):
        assert not controller.busy
        assert controller.diagnosis is None


class TestDiagnose:
    def test_produces_a_diagnosis(self, controller):
        assert controller.start_diagnose(profile="quick")
        event, _seen = drain_until(controller, ctrl.DIAGNOSIS)

        diagnosis = event.payload
        assert diagnosis.reports
        assert diagnosis.counts()["checks_run"] > 0
        assert controller.diagnosis is diagnosis

    def test_reports_progress(self, controller):
        controller.start_diagnose(profile="quick")
        _event, seen = drain_until(controller, ctrl.DIAGNOSIS)

        progress = [e for e in seen if e.kind == ctrl.PROGRESS]
        assert progress
        assert progress[-1].payload["total"] > 0
        assert progress[-1].payload["label"]

    def test_emits_busy_transitions(self, controller):
        """The window re-enables its Run button on the trailing busy=False."""
        controller.start_diagnose(profile="quick")
        _event, seen = drain_until(controller, ctrl.DIAGNOSIS)
        controller.wait()

        states = [event.payload for event in seen if event.kind == ctrl.BUSY]
        deadline = time.time() + 5
        while time.time() < deadline and False not in states:
            states += [e.payload for e in controller.drain() if e.kind == ctrl.BUSY]
            time.sleep(0.05)

        assert True in states, "the UI must be told when work starts"
        assert False in states, "the UI must be told when work finishes"

    def test_can_run_a_single_check(self, controller):
        controller.start_diagnose(only=["disk.space"])
        event, _ = drain_until(controller, ctrl.DIAGNOSIS)
        assert [r.check_id for r in event.payload.reports] == ["disk.space"]

    def test_refuses_to_run_two_things_at_once(self, controller):
        assert controller.start_diagnose(profile="quick")
        # The second call must be rejected rather than racing the first.
        assert controller.start_diagnose(profile="quick") is False

        failures = []
        deadline = time.time() + 5
        while time.time() < deadline and not failures:
            failures += [e for e in controller.drain() if e.kind == ctrl.FAILED]
            time.sleep(0.05)
        assert failures
        controller.wait()

    def test_worker_failure_becomes_an_event(self, controller, monkeypatch):
        def explode(*_args, **_kwargs):
            raise RuntimeError("engine on fire")

        monkeypatch.setattr("medic.gui.controller.run_checks", explode)
        controller.start_diagnose(profile="quick")

        event, _ = drain_until(controller, ctrl.FAILED, timeout=20)
        assert "engine on fire" in str(event.payload)

    def test_lock_is_released_after_a_failure(self, controller, monkeypatch):
        monkeypatch.setattr(
            "medic.gui.controller.run_checks",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        controller.start_diagnose(profile="quick")
        drain_until(controller, ctrl.FAILED, timeout=20)
        controller.wait()

        assert not controller.busy, "a crash must not wedge the app permanently"


class TestPreviewAndApply:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        cache = tmp_path / ".cache"
        (cache / "stale").mkdir(parents=True)
        (cache / "stale" / "blob.bin").write_bytes(b"x" * 8192)
        (cache / "fresh").mkdir(parents=True)

        long_ago = time.time() - (90 * 86400)
        os.utime(cache / "stale", (long_ago, long_ago))
        return tmp_path

    def test_preview_bundles_the_plans(self, controller, home):
        controller.start_preview(["clean.user-cache"])
        event, _ = drain_until(controller, ctrl.PLANS)

        bundle = event.payload
        assert bundle.actionable
        assert bundle.fix_ids == ["clean.user-cache"]
        assert bundle.est_bytes >= 8192
        assert bundle.highest_risk == "safe"

    def test_preview_changes_nothing(self, controller, home):
        controller.start_preview(["clean.user-cache"])
        drain_until(controller, ctrl.PLANS)
        assert (home / ".cache" / "stale").exists()

    def test_bundle_reports_the_highest_risk(self, controller, home):
        controller.start_preview(["clean.user-cache", "clean.tmp"])
        event, _ = drain_until(controller, ctrl.PLANS)

        if "clean.tmp" in event.payload.fix_ids:
            assert event.payload.highest_risk == "moderate"

    def test_nothing_to_do_is_not_actionable(self, controller, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".cache").mkdir()

        controller.start_preview(["clean.user-cache"])
        event, _ = drain_until(controller, ctrl.PLANS)
        assert not event.payload.actionable

    def test_apply_removes_only_the_stale_cache(self, controller, home):
        controller.start_apply(["clean.user-cache"])
        event, _ = drain_until(controller, ctrl.RESULTS)

        assert not (home / ".cache" / "stale").exists()
        assert (home / ".cache" / "fresh").exists()
        assert sum(result.bytes_freed for result in event.payload) >= 8192

    def test_unknown_fix_ids_are_skipped(self, controller, home):
        controller.start_preview(["no.such.fix"])
        event, _ = drain_until(controller, ctrl.PLANS)
        assert event.payload.plans == []


class TestTheme:
    def test_palettes_cover_every_severity(self):
        for palette in (LIGHT, DARK):
            for level in ("critical", "warn", "info", "ok", "unknown"):
                assert palette.severity(level).startswith("#")
                assert palette.severity_bg(level).startswith("#")

    def test_palettes_cover_every_risk_level(self):
        for palette in (LIGHT, DARK):
            for level in ("safe", "moderate", "risky"):
                assert palette.risk(level).startswith("#")
                assert palette.risk_bg(level).startswith("#")

    def test_unknown_keys_fall_back_rather_than_crash(self):
        assert LIGHT.severity("invented").startswith("#")
        assert LIGHT.risk("invented").startswith("#")

    def test_light_and_dark_actually_differ(self):
        assert LIGHT.bg != DARK.bg
        assert LIGHT.text != DARK.text

    def test_env_var_overrides_detection(self, monkeypatch):
        monkeypatch.setenv("MEDIC_THEME", "dark")
        assert detect_palette() is DARK
        monkeypatch.setenv("MEDIC_THEME", "light")
        assert detect_palette() is LIGHT

    def test_detection_falls_back_to_a_real_palette(self, monkeypatch):
        monkeypatch.delenv("MEDIC_THEME", raising=False)
        assert detect_palette() in (LIGHT, DARK)

    def test_fonts_are_named_with_a_size(self):
        for font in (mono_font(), ui_font()):
            family, size = font
            assert isinstance(family, str) and family
            assert isinstance(size, int) and size > 0


class TestAvailability:
    def test_reports_whether_tk_can_be_imported(self):
        assert isinstance(tkinter_available(), bool)

    def test_importing_the_package_does_not_need_tk(self):
        """`medic gui` must be able to print a helpful error message."""
        import importlib

        module = importlib.import_module("medic.gui")
        assert hasattr(module, "launch")


@pytest.mark.skipif(not tkinter_available(), reason="tkinter is not installed")
@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="no display available")
class TestWindow:
    """Smoke tests that only run where a real display exists."""

    def test_window_builds_and_renders(self):
        import tkinter as tk

        from medic.gui.app import MedicApp

        root = tk.Tk()
        try:
            app = MedicApp(root, offline=True, palette=LIGHT)
            root.update_idletasks()

            assert app.run_button.winfo_exists()
            assert app.notebook.index("end") == 2  # Findings + Repairs
            assert app.fixes_view.body.winfo_children()
        finally:
            root.destroy()

    def test_theme_switch_does_not_raise(self):
        import tkinter as tk

        from medic.gui.app import MedicApp

        root = tk.Tk()
        try:
            app = MedicApp(root, offline=True, palette=LIGHT)
            app.theme_var.set("dark")
            app.palette = DARK
            app._render_fixes()
            root.update_idletasks()
        finally:
            root.destroy()
