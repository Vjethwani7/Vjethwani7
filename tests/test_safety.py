"""Guardrail tests.

These are the most important tests in the suite: everything else being
wrong produces a bad report, but a bug here deletes someone's files.
"""

from __future__ import annotations

import os

import pytest

from medic.core.safety import (
    UnsafePathError,
    assert_safe,
    collect_stale,
    is_within,
    measure,
    safe_delete,
)


class TestAssertSafe:
    def test_accepts_path_inside_allowed_root(self, tmp_path):
        target = tmp_path / "cache" / "thing"
        target.mkdir(parents=True)
        assert_safe(str(target), [str(tmp_path)])

    @pytest.mark.parametrize("path", ["/", "/etc", "/usr", "/var", "/home", "/System"])
    def test_rejects_system_directories(self, path):
        with pytest.raises(UnsafePathError, match="system directory|too close"):
            assert_safe(path, ["/"])

    def test_rejects_path_outside_allowed_roots(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        with pytest.raises(UnsafePathError, match="outside permitted roots"):
            assert_safe(str(outside), [str(allowed)])

    def test_rejects_symlink_escaping_the_root(self, tmp_path):
        """A link inside the root pointing out of it must not be followed."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        secret = tmp_path / "secret"
        secret.mkdir()
        link = allowed / "escape"
        link.symlink_to(secret)

        with pytest.raises(UnsafePathError, match="outside permitted roots"):
            assert_safe(str(link), [str(allowed)])

    def test_rejects_relative_path(self):
        with pytest.raises(UnsafePathError, match="not an absolute path"):
            assert_safe("relative/path", ["/tmp"])

    def test_rejects_empty_path(self):
        with pytest.raises(UnsafePathError):
            assert_safe("   ", ["/tmp"])

    def test_rejects_home_directory_itself(self):
        # Which rule fires depends on where home is (as root it is /root,
        # which is also on the forbidden list). Either way it must refuse.
        with pytest.raises(UnsafePathError):
            assert_safe(os.path.expanduser("~"), ["/"])

    def test_rejects_home_directory_when_nested(self, monkeypatch, tmp_path):
        home = tmp_path / "users" / "someone"
        home.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        with pytest.raises(UnsafePathError, match="home directory"):
            assert_safe(str(home), [str(tmp_path)])

    def test_rejects_when_no_roots_given(self, tmp_path):
        target = tmp_path / "x"
        target.mkdir()
        with pytest.raises(UnsafePathError, match="no allowed roots"):
            assert_safe(str(target), [])

    def test_rejects_shallow_path(self):
        with pytest.raises(UnsafePathError, match="too close"):
            assert_safe("/nonexistent-top-level", ["/"])


class TestIsWithin:
    def test_direct_child(self, tmp_path):
        assert is_within(str(tmp_path / "a" / "b"), str(tmp_path))

    def test_sibling_is_not_within(self, tmp_path):
        assert not is_within(str(tmp_path.parent / "elsewhere"), str(tmp_path))

    def test_prefix_collision_is_not_containment(self, tmp_path):
        """/foo/barbaz is not inside /foo/bar despite the string prefix."""
        (tmp_path / "bar").mkdir()
        (tmp_path / "barbaz").mkdir()
        assert not is_within(str(tmp_path / "barbaz"), str(tmp_path / "bar"))


class TestSafeDelete:
    def test_dry_run_measures_but_does_not_delete(self, tmp_path):
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "file.bin").write_bytes(b"x" * 2048)

        report = safe_delete([str(victim)], [str(tmp_path)], dry_run=True)

        assert victim.exists(), "dry run must not delete anything"
        assert report.deleted == [str(victim)]
        assert report.bytes_freed >= 2048

    def test_actually_deletes_when_not_dry_run(self, tmp_path):
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "file.bin").write_bytes(b"x" * 1024)

        report = safe_delete([str(victim)], [str(tmp_path)], dry_run=False)

        assert not victim.exists()
        assert report.bytes_freed >= 1024
        assert not report.errors

    def test_skips_unsafe_paths_and_continues(self, tmp_path):
        good = tmp_path / "good"
        good.mkdir()

        report = safe_delete([str(good), "/etc"], [str(tmp_path)], dry_run=False)

        assert not good.exists(), "the safe path should still be processed"
        assert os.path.isdir("/etc"), "the unsafe path must be untouched"
        assert len(report.skipped) == 1

    def test_does_not_follow_symlinks_out_of_the_tree(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep me")

        root = tmp_path / "root"
        root.mkdir()
        (root / "link").symlink_to(outside)

        safe_delete([str(root / "link")], [str(root)], dry_run=False)

        assert (outside / "precious.txt").exists(), "target of the symlink must survive"

    def test_missing_path_is_skipped_not_an_error(self, tmp_path):
        report = safe_delete([str(tmp_path / "gone")], [str(tmp_path)], dry_run=False)
        assert report.deleted == []
        assert report.skipped and "no longer exists" in report.skipped[0][1]


class TestCollectStale:
    def test_finds_only_old_entries(self, tmp_path):
        import time

        old = tmp_path / "old"
        old.mkdir()
        fresh = tmp_path / "fresh"
        fresh.mkdir()

        long_ago = time.time() - (60 * 86400)
        os.utime(old, (long_ago, long_ago))

        stale = collect_stale(str(tmp_path), older_than_days=30)

        assert str(old) in stale
        assert str(fresh) not in stale

    def test_honours_skip_names(self, tmp_path):
        import time

        keep = tmp_path / "keepme"
        keep.mkdir()
        long_ago = time.time() - (60 * 86400)
        os.utime(keep, (long_ago, long_ago))

        stale = collect_stale(
            str(tmp_path), older_than_days=30, skip_names=frozenset({"keepme"})
        )

        assert stale == []

    def test_missing_directory_returns_empty(self, tmp_path):
        assert collect_stale(str(tmp_path / "nope"), older_than_days=1) == []


class TestMeasure:
    def test_file_size(self, tmp_path):
        target = tmp_path / "f"
        target.write_bytes(b"a" * 100)
        assert measure(str(target)) == 100

    def test_directory_sums_children(self, tmp_path):
        (tmp_path / "a").write_bytes(b"a" * 100)
        (tmp_path / "b").write_bytes(b"b" * 200)
        assert measure(str(tmp_path)) >= 300

    def test_missing_path_is_zero(self, tmp_path):
        assert measure(str(tmp_path / "nope")) == 0
