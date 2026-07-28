"""Tests for the repair implementations, including a real apply cycle."""

from __future__ import annotations

import os
import time

import pytest

from medic.core.model import Risk
from medic.core.runner import apply_plan, plan_fix
from medic.fixes.cleanup import CleanUserCache, EmptyTrash, VacuumJournal
from medic.fixes.network import FlushDns
from medic.fixes.services import RestartFailedUnits
from medic.fixes.system import EnableTimeSync

AGE = 60 * 86400


def make_stale(path: str) -> None:
    long_ago = time.time() - AGE
    os.utime(path, (long_ago, long_ago))


class TestCleanUserCache:
    """The whole point of medic: preview honestly, then do exactly that."""

    @pytest.fixture
    def home(self, tmp_path, ctx):
        cache = tmp_path / ".cache"
        (cache / "stale-app").mkdir(parents=True)
        (cache / "stale-app" / "blob.bin").write_bytes(b"x" * 4096)
        (cache / "active-app").mkdir(parents=True)
        (cache / "active-app" / "blob.bin").write_bytes(b"y" * 4096)
        make_stale(str(cache / "stale-app"))

        ctx.home = str(tmp_path)
        return tmp_path

    def test_plan_targets_only_the_stale_directory(self, ctx, home):
        plan = plan_fix(ctx, CleanUserCache())

        assert len(plan.actions) == 1
        assert plan.est_bytes >= 4096
        assert "1 stale cache" in plan.actions[0].description

    def test_preview_changes_nothing(self, ctx, home):
        fix = CleanUserCache()
        apply_plan(ctx, fix, plan_fix(ctx, fix), dry_run=True)

        assert (home / ".cache" / "stale-app").exists()
        assert (home / ".cache" / "active-app").exists()

    def test_apply_removes_stale_and_keeps_active(self, ctx, home):
        fix = CleanUserCache()
        result = apply_plan(ctx, fix, plan_fix(ctx, fix), dry_run=False)

        assert not (home / ".cache" / "stale-app").exists()
        assert (home / ".cache" / "active-app").exists(), "active cache must survive"
        assert result.ok and result.bytes_freed >= 4096

    def test_previewing_first_does_not_change_what_apply_does(self, ctx, home):
        """Regression: measuring a directory used to refresh its access time,
        so a preview made its own finding vanish from the next run."""
        fix = CleanUserCache()
        apply_plan(ctx, fix, plan_fix(ctx, fix), dry_run=True)

        second = plan_fix(ctx, fix)
        assert len(second.actions) == 1, "the finding must survive being previewed"

    def test_empty_cache_produces_no_actions(self, ctx, tmp_path):
        (tmp_path / ".cache").mkdir()
        ctx.home = str(tmp_path)

        assert plan_fix(ctx, CleanUserCache()).empty

    def test_missing_cache_directory_is_not_an_error(self, ctx, tmp_path):
        ctx.home = str(tmp_path)
        plan = plan_fix(ctx, CleanUserCache())
        assert plan.empty and not plan.blocked

    def test_protected_caches_are_never_removed(self, ctx, tmp_path):
        cache = tmp_path / ".cache"
        (cache / "fontconfig").mkdir(parents=True)
        make_stale(str(cache / "fontconfig"))
        ctx.home = str(tmp_path)

        assert plan_fix(ctx, CleanUserCache()).empty

    def test_cannot_delete_outside_the_cache_root(self, ctx, tmp_path):
        """A symlink out of the cache must not lead the fix astray."""
        outside = tmp_path / "documents"
        outside.mkdir()
        (outside / "important.txt").write_text("do not delete")

        cache = tmp_path / ".cache"
        cache.mkdir()
        link = cache / "sneaky"
        link.symlink_to(outside)
        make_stale(str(link))

        ctx.home = str(tmp_path)
        fix = CleanUserCache()
        apply_plan(ctx, fix, plan_fix(ctx, fix), dry_run=False)

        assert (outside / "important.txt").exists(), "guardrails must block the escape"


class TestEmptyTrash:
    def test_only_old_trash_is_purged(self, ctx, tmp_path, monkeypatch):
        trash = tmp_path / ".local" / "share" / "Trash" / "files"
        trash.mkdir(parents=True)
        (trash / "old").mkdir()
        (trash / "recent").mkdir()
        make_stale(str(trash / "old"))

        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        ctx.home = str(tmp_path)

        plan = plan_fix(ctx, EmptyTrash())
        apply_plan(ctx, EmptyTrash(), plan, dry_run=False)

        assert not (trash / "old").exists()
        assert (trash / "recent").exists()

    def test_it_is_rated_riskier_than_cache_cleanup(self):
        """Emptying the trash is irreversible in a way caches are not."""
        assert EmptyTrash().risk > CleanUserCache().risk


class TestVacuumJournal:
    def test_blocked_when_journalctl_is_missing(self, ctx):
        assert plan_fix(ctx, VacuumJournal()).blocked

    def test_no_action_when_the_journal_is_already_small(self, ctx):
        ctx.is_root = True
        ctx.stub(["journalctl", "--disk-usage"], "journals take up 8.0M in the file system.")

        plan = plan_fix(ctx, VacuumJournal())
        assert plan.empty and not plan.blocked

    def test_vacuums_when_the_journal_is_large(self, ctx):
        ctx.is_root = True
        ctx.stub(["journalctl", "--disk-usage"], "journals take up 4.0G in the file system.")

        plan = plan_fix(ctx, VacuumJournal())

        assert len(plan.actions) == 1
        assert plan.actions[0].argv == ["journalctl", "--vacuum-size=500M"]
        assert plan.est_bytes > 3 * 1024**3

    def test_blocked_when_the_size_cannot_be_read(self, ctx):
        ctx.is_root = True
        ctx.stub(["journalctl", "--disk-usage"], "", returncode=1)

        assert "could not determine" in plan_fix(ctx, VacuumJournal()).blocked_reason

    def test_needs_root(self, ctx):
        ctx.is_root = False
        assert "root" in plan_fix(ctx, VacuumJournal()).blocked_reason


class TestEnableTimeSync:
    def test_no_action_when_sync_is_already_on(self, ctx):
        ctx.is_root = True
        ctx.stub(["timedatectl", "show", "--property=NTP"], "NTP=yes\n")

        assert plan_fix(ctx, EnableTimeSync()).empty

    def test_enables_sync_when_it_is_off(self, ctx):
        ctx.is_root = True
        ctx.stub(["timedatectl", "show", "--property=NTP"], "NTP=no\n")

        plan = plan_fix(ctx, EnableTimeSync())
        assert plan.actions[0].argv == ["timedatectl", "set-ntp", "true"]

    def test_blocked_when_timedatectl_does_not_work(self, ctx):
        """Never issue a command when the current state cannot be read."""
        ctx.is_root = True
        ctx.provide("timedatectl")
        ctx.stub(["timedatectl", "show", "--property=NTP"], "", returncode=1,
                 stderr="Failed to connect to bus")

        assert "not working" in plan_fix(ctx, EnableTimeSync()).blocked_reason

    def test_skipped_in_a_container(self, ctx):
        ctx.is_root = True
        ctx.container = "docker"
        assert "container" in plan_fix(ctx, EnableTimeSync()).blocked_reason


class TestRestartFailedUnits:
    def _failed(self, ctx, units: str, user_units: str = "") -> None:
        ctx.provide("systemctl")
        ctx.stub(
            ["systemctl", "--failed", "--no-legend", "--plain", "list-units", "--type=service"],
            units,
        )
        ctx.stub(
            ["systemctl", "--user", "--failed", "--no-legend", "--plain", "list-units",
             "--type=service"],
            user_units,
        )

    def test_plans_a_restart_for_each_failed_unit(self, ctx):
        ctx.is_root = True
        self._failed(ctx, "nginx.service loaded failed failed Web server\n")

        plan = plan_fix(ctx, RestartFailedUnits())

        assert len(plan.actions) == 1
        assert plan.actions[0].argv == ["systemctl", "restart", "nginx.service"]

    def test_refuses_to_restart_session_critical_units(self, ctx):
        """Restarting dbus mid-session would take the desktop down with it."""
        ctx.is_root = True
        self._failed(ctx, "dbus.service loaded failed failed D-Bus\n")

        plan = plan_fix(ctx, RestartFailedUnits())

        assert plan.empty
        assert any("dbus.service" in note for note in plan.notes)

    def test_without_root_it_explains_rather_than_failing(self, ctx):
        ctx.is_root = False
        self._failed(ctx, "nginx.service loaded failed failed Web server\n")

        plan = plan_fix(ctx, RestartFailedUnits())

        assert plan.empty
        assert any("sudo" in note for note in plan.notes)

    def test_nothing_failed_means_nothing_to_do(self, ctx):
        ctx.is_root = True
        self._failed(ctx, "")

        assert plan_fix(ctx, RestartFailedUnits()).empty


class TestFlushDns:
    def test_blocked_when_no_cache_service_exists(self, ctx):
        ctx.is_root = True
        assert plan_fix(ctx, FlushDns()).blocked

    def test_uses_resolvectl_when_available(self, ctx):
        ctx.is_root = True
        ctx.provide("resolvectl")
        ctx.stub(["systemctl", "is-active", "systemd-resolved"], "active\n")

        plan = plan_fix(ctx, FlushDns())
        assert plan.actions[0].argv == ["resolvectl", "flush-caches"]

    def test_also_starts_the_resolver_when_it_is_down(self, ctx):
        ctx.is_root = True
        ctx.provide("resolvectl", "systemctl")
        ctx.stub(["systemctl", "is-active", "systemd-resolved"], "inactive\n", returncode=3)

        plan = plan_fix(ctx, FlushDns())

        assert len(plan.actions) == 2
        assert "restart" in plan.actions[1].argv


class TestRiskLevels:
    """Risk ratings are a promise to the user; check they are coherent."""

    def test_irreversible_deletions_are_never_rated_safe(self):
        from medic.core import registry

        for fix in registry.all_fixes():
            if fix.id in ("clean.tmp", "clean.trash", "updates.apply"):
                assert fix.risk > Risk.SAFE, f"{fix.id} should not be rated safe"

    def test_package_updates_are_the_highest_risk(self):
        from medic.core import registry

        assert registry.get_fix("updates.apply").risk is Risk.RISKY
