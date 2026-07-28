"""Tests for utilities, models, config, journal, registry, and reporting."""

from __future__ import annotations

import json

import pytest

from medic.core import registry
from medic.core.config import Config
from medic.core.journal import Journal
from medic.core.model import CheckReport, Finding, Risk, Severity
from medic.core.probe import Filesystem, Memory
from medic.core.report import Printer, render_diagnosis, to_markdown
from medic.core.runner import Diagnosis
from medic.core.shell import run
from medic.core.util import dir_size, human_bytes, human_duration, parse_size, truncate


class TestHumanBytes:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (0, "0 B"),
            (512, "512 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (1024**2, "1.0 MB"),
            (1024**3, "1.0 GB"),
            (None, "?"),
        ],
    )
    def test_formats(self, value, expected):
        assert human_bytes(value) == expected

    def test_negative_values_keep_their_sign(self):
        assert human_bytes(-2048) == "-2.0 KB"


class TestHumanDuration:
    @pytest.mark.parametrize(
        "value,expected",
        [(30, "30s"), (90, "1m"), (3600, "1h"), (93784, "1d 2h"), (None, "?")],
    )
    def test_formats(self, value, expected):
        assert human_duration(value) == expected


class TestParseSize:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("500M", 500 * 1024**2),
            ("1.5G", int(1.5 * 1024**3)),
            ("900k", 900 * 1024),
            ("1024", 1024),
            ("2GiB", 2 * 1024**3),
        ],
    )
    def test_parses(self, text, expected):
        assert parse_size(text) == expected

    @pytest.mark.parametrize("text", ["", "abc", "M", None])
    def test_rejects_nonsense(self, text):
        assert parse_size(text) is None


class TestDirSize:
    def test_sums_a_tree(self, tmp_path):
        (tmp_path / "a").write_bytes(b"x" * 100)
        nested = tmp_path / "nested"
        nested.mkdir()
        (nested / "b").write_bytes(b"y" * 200)

        assert dir_size(str(tmp_path)) >= 300

    def test_does_not_follow_symlinks_into_a_huge_tree(self, tmp_path):
        big = tmp_path / "big"
        big.mkdir()
        (big / "file").write_bytes(b"z" * 10_000)

        small = tmp_path / "small"
        small.mkdir()
        (small / "link").symlink_to(big)

        # Only the link itself is counted, not the tree behind it.
        assert dir_size(str(small)) < 1000

    def test_missing_directory_is_zero(self, tmp_path):
        assert dir_size(str(tmp_path / "nope")) == 0


class TestTruncate:
    def test_short_text_is_unchanged(self):
        assert truncate("hello", 20) == "hello"

    def test_long_text_gets_an_ellipsis(self):
        assert truncate("x" * 50, 10).endswith("…")
        assert len(truncate("x" * 50, 10)) == 10


class TestSeverity:
    def test_ordering(self):
        assert Severity.OK < Severity.INFO < Severity.WARN < Severity.CRITICAL

    def test_parse_roundtrip(self):
        for level in Severity:
            assert Severity.parse(level.label) is level

    def test_parse_rejects_nonsense(self):
        with pytest.raises(ValueError):
            Severity.parse("catastrophic")


class TestRisk:
    def test_ordering(self):
        assert Risk.SAFE < Risk.MODERATE < Risk.RISKY

    def test_parse_roundtrip(self):
        for level in Risk:
            assert Risk.parse(level.label) is level


class TestCheckReport:
    def test_no_findings_means_ok(self):
        assert CheckReport("a", "A", "test").severity is Severity.OK

    def test_severity_is_the_worst_finding(self):
        report = CheckReport(
            "a", "A", "test",
            findings=[
                Finding("a", "minor", Severity.INFO),
                Finding("a", "major", Severity.CRITICAL),
                Finding("a", "middling", Severity.WARN),
            ],
        )
        assert report.severity is Severity.CRITICAL

    def test_error_makes_it_unknown(self):
        assert CheckReport("a", "A", "test", error="boom").severity is Severity.UNKNOWN

    def test_serialises_to_json(self):
        report = CheckReport("a", "A", "test", findings=[Finding("a", "thing", Severity.WARN)])
        assert json.loads(json.dumps(report.to_dict()))["severity"] == "warn"


class TestFilesystem:
    def test_percent_used_matches_df_semantics(self):
        """Usage is measured against user-usable space, excluding reserve."""
        fs = Filesystem("/dev/sda1", "/", total=1000, used=800, free=100)
        assert fs.percent_used == pytest.approx(800 / 900 * 100)

    def test_zero_size_does_not_divide_by_zero(self):
        assert Filesystem("none", "/x", total=0).percent_used == 0.0

    def test_inode_percentage(self):
        fs = Filesystem("/dev/sda1", "/", inodes_total=100, inodes_used=95)
        assert fs.inodes_percent_used == 95.0


class TestMemory:
    def test_used_and_percentages(self):
        mem = Memory(total=1000, available=250, swap_total=500, swap_free=100)
        assert mem.used == 750
        assert mem.percent_used == 75.0
        assert mem.swap_used == 400
        assert mem.swap_percent_used == 80.0

    def test_no_swap_reports_zero_not_an_error(self):
        assert Memory(total=1000, available=500).swap_percent_used == 0.0


class TestConfig:
    def test_defaults_are_sane(self):
        config = Config()
        assert config.disk_warn_percent < config.disk_critical_percent
        assert config.mem_warn_percent < config.mem_critical_percent
        assert config.load_warn_ratio < config.load_critical_ratio

    def test_roundtrips_through_disk(self, tmp_path):
        path = str(tmp_path / "config.json")
        original = Config(disk_warn_percent=70.0)
        original.save(path)

        assert Config.load(path).disk_warn_percent == 70.0

    def test_missing_file_returns_defaults(self, tmp_path):
        assert Config.load(str(tmp_path / "absent.json")).disk_warn_percent == 85.0

    def test_corrupt_file_returns_defaults_rather_than_crashing(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json at all")
        assert Config.load(str(path)).disk_warn_percent == 85.0

    def test_unknown_keys_are_ignored(self, tmp_path):
        path = tmp_path / "future.json"
        path.write_text(json.dumps({"disk_warn_percent": 60.0, "invented_setting": True}))

        config = Config.load(str(path))
        assert config.disk_warn_percent == 60.0
        assert not hasattr(config, "invented_setting")


class TestJournal:
    def test_records_and_reads_back(self, tmp_path):
        journal = Journal(str(tmp_path / "j.jsonl"))
        journal.record("fix.end", fix_id="clean.tmp", ok=True)

        entries = journal.read()
        assert len(entries) == 1
        assert entries[0]["fix_id"] == "clean.tmp"
        assert "time" in entries[0]

    def test_appends_rather_than_overwrites(self, tmp_path):
        journal = Journal(str(tmp_path / "j.jsonl"))
        for index in range(3):
            journal.record("diagnose", run=index)

        assert len(journal.read()) == 3

    def test_limit_returns_the_most_recent(self, tmp_path):
        journal = Journal(str(tmp_path / "j.jsonl"))
        for index in range(5):
            journal.record("diagnose", run=index)

        assert [e["run"] for e in journal.read(limit=2)] == [3, 4]

    def test_corrupt_lines_are_skipped(self, tmp_path):
        path = tmp_path / "j.jsonl"
        path.write_text('{"event": "good"}\nnot json\n{"event": "also good"}\n')

        assert [e["event"] for e in Journal(str(path)).read()] == ["good", "also good"]

    def test_missing_file_reads_as_empty(self, tmp_path):
        assert Journal(str(tmp_path / "absent.jsonl")).read() == []

    def test_unwritable_path_does_not_raise(self, tmp_path):
        """A broken journal must never take down a repair that is working."""
        journal = Journal("/proc/definitely/not/writable/j.jsonl")
        journal.record("fix.end", fix_id="x")  # must not raise


class TestRegistry:
    def test_discovers_the_built_in_checks(self):
        ids = {check.id for check in registry.all_checks()}
        assert {"disk.space", "mem.pressure", "net.connectivity"} <= ids

    def test_discovers_the_built_in_fixes(self):
        ids = {fix.id for fix in registry.all_fixes()}
        assert {"clean.user-cache", "clean.journal"} <= ids

    def test_every_check_has_an_id_name_and_description(self):
        for check in registry.all_checks():
            assert check.id and check.name and check.description

    def test_every_fix_documents_itself(self):
        for fix in registry.all_fixes():
            assert fix.id and fix.name and fix.description

    def test_fix_ids_referenced_by_checks_all_exist(self):
        """A finding must never point the user at a fix that does not exist.

        Every ``fix_ids=`` argument in the check modules is read out of the
        source and validated against the registry, so a renamed fix cannot
        silently leave a dangling suggestion behind.
        """
        import ast
        import importlib
        import inspect

        known = {fix.id for fix in registry.all_fixes()}
        referenced: set[str] = set()

        for check in registry.all_checks():
            module = importlib.import_module(type(check).__module__)
            tree = ast.parse(inspect.getsource(module))

            for node in ast.walk(tree):
                if not isinstance(node, ast.keyword) or node.arg != "fix_ids":
                    continue
                value = node.value
                if isinstance(value, ast.List):
                    referenced.update(
                        element.value
                        for element in value.elts
                        if isinstance(element, ast.Constant) and isinstance(element.value, str)
                    )
                elif isinstance(value, ast.Name):
                    # e.g. fix_ids=CLEANUP_FIXES - resolve the constant.
                    resolved = getattr(module, value.id, None)
                    if isinstance(resolved, (list, tuple)):
                        referenced.update(item for item in resolved if isinstance(item, str))

        assert referenced, "no fix references found - the scan is broken, not the code"

        dangling = referenced - known
        assert not dangling, f"checks suggest fixes that do not exist: {sorted(dangling)}"

    def test_dynamically_built_fix_references_exist(self):
        """disk.hogs derives its suggestions from a table; validate that too."""
        from medic.checks.disk import SpaceHogs

        known = {fix.id for fix in registry.all_fixes()}
        referenced = {fix_id for _what, fix_id in SpaceHogs.INTERESTING.values() if fix_id}

        assert referenced - known == set()

    def test_fix_addresses_reference_real_checks(self):
        check_ids = {check.id for check in registry.all_checks()}
        for fix in registry.all_fixes():
            for check_id in fix.addresses:
                assert check_id in check_ids, f"{fix.id} addresses unknown check {check_id}"

    def test_resolve_matches_exact_ids(self):
        assert registry.resolve(["disk.space"], ["disk.space", "disk.inodes"]) == ["disk.space"]

    def test_resolve_expands_a_category_prefix(self):
        assert set(registry.resolve(["disk"], ["disk.space", "disk.inodes", "mem.swap"])) == {
            "disk.space",
            "disk.inodes",
        }

    def test_resolve_supports_globs(self):
        assert registry.resolve(["*.space"], ["disk.space", "mem.swap"]) == ["disk.space"]

    def test_resolve_deduplicates(self):
        assert registry.resolve(["disk", "disk.space"], ["disk.space"]) == ["disk.space"]

    def test_unmatched_reports_typos(self):
        assert registry.unmatched(["disk.spce"], ["disk.space"]) == ["disk.spce"]


class TestShellRun:
    def test_captures_stdout(self):
        result = run(["echo", "hello"])
        assert result.ok
        assert result.stdout.strip() == "hello"

    def test_nonzero_exit_is_reported_not_raised(self):
        result = run(["false"])
        assert not result.ok
        assert result.returncode != 0

    def test_missing_command_is_flagged(self):
        result = run(["definitely-not-a-real-command-xyz"])
        assert result.not_found
        assert "not found" in result.failure_reason

    def test_timeout_is_flagged(self):
        result = run(["sleep", "5"], timeout=0.2)
        assert result.timed_out
        assert "timed out" in result.failure_reason

    def test_empty_argv_is_rejected(self):
        with pytest.raises(ValueError):
            run([])

    def test_output_is_locale_independent(self):
        result = run(["sh", "-c", "echo $LC_ALL"])
        assert result.stdout.strip() == "C"


class TestReporting:
    def _diagnosis(self) -> Diagnosis:
        return Diagnosis(
            reports=[
                CheckReport(
                    "disk.space", "Disk space", "disk",
                    findings=[
                        Finding(
                            "disk.space", "Filesystem / is 95% full", Severity.CRITICAL,
                            detail="detail text", fix_ids=["clean.tmp"],
                            advice="free some space",
                        )
                    ],
                )
            ],
            host="testhost",
            platform="Test Linux",
        )

    def test_markdown_contains_the_finding(self):
        markdown = to_markdown(self._diagnosis())
        assert "# System diagnostic report" in markdown
        assert "95% full" in markdown
        assert "`medic fix clean.tmp`" in markdown

    def test_markdown_for_a_clean_system(self):
        markdown = to_markdown(Diagnosis(host="h", platform="p"))
        assert "No problems found." in markdown

    def test_terminal_output_includes_advice_and_fix_command(self, capsys):
        printer = Printer(color=False)
        render_diagnosis(self._diagnosis(), printer)

        output = capsys.readouterr().out
        assert "95% full" in output
        assert "free some space" in output
        assert "medic fix clean.tmp" in output

    def test_colour_is_disabled_when_not_a_tty(self):
        assert Printer(color=False).paint("x", "\033[31m") == "x"

    def test_no_color_env_disables_colour(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        assert Printer().color is False
