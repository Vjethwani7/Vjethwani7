"""End-to-end tests for the command line interface.

These run against the real machine, so they assert on structure and exit
codes rather than on specific findings.
"""

from __future__ import annotations

import json

import pytest

from medic.cli import EXIT_CRITICAL, EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, main


class TestListCommand:
    def test_lists_checks_and_fixes(self, capsys):
        assert main(["list"]) == EXIT_OK
        output = capsys.readouterr().out
        assert "disk.space" in output
        assert "clean.user-cache" in output

    def test_json_output_is_valid(self, capsys):
        assert main(["list", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)

        assert payload["checks"] and payload["fixes"]
        ids = {check["id"] for check in payload["checks"]}
        assert "disk.space" in ids

    def test_every_fix_declares_a_risk_level(self, capsys):
        main(["list", "--json"])
        payload = json.loads(capsys.readouterr().out)
        for fix in payload["fixes"]:
            assert fix["risk"] in ("safe", "moderate", "risky")


class TestDiagnose:
    def test_json_diagnosis_has_the_expected_shape(self, capsys):
        main(["diagnose", "--profile", "quick", "--offline", "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert payload["schema"] == 1
        assert "severity" in payload
        assert isinstance(payload["reports"], list)
        assert payload["counts"]["checks_run"] > 0

    def test_exit_code_reflects_severity(self, capsys):
        code = main(["diagnose", "--profile", "quick", "--offline", "--json"])
        payload = json.loads(capsys.readouterr().out)

        expected = {
            "ok": EXIT_OK,
            "info": EXIT_OK,
            "unknown": EXIT_OK,
            "warn": EXIT_PROBLEMS,
            "critical": EXIT_CRITICAL,
        }[payload["severity"]]
        assert code == expected

    def test_only_filters_to_one_check(self, capsys):
        main(["diagnose", "--only", "disk.space", "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert [report["check_id"] for report in payload["reports"]] == ["disk.space"]

    def test_category_prefix_selects_a_group(self, capsys):
        main(["diagnose", "--only", "disk", "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert len(payload["reports"]) > 1
        assert all(report["check_id"].startswith("disk.") for report in payload["reports"])

    def test_unknown_check_is_a_usage_error(self, capsys):
        assert main(["diagnose", "--only", "no.such.check"]) == EXIT_USAGE
        assert "unknown check" in capsys.readouterr().out

    def test_skip_removes_a_check(self, capsys):
        main(["diagnose", "--only", "disk", "--skip", "disk.hogs", "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert "disk.hogs" not in [report["check_id"] for report in payload["reports"]]

    def test_writes_a_markdown_report(self, tmp_path, capsys):
        target = tmp_path / "report.md"
        main(["diagnose", "--profile", "quick", "--offline", "--markdown", str(target)])

        content = target.read_text()
        assert content.startswith("# System diagnostic report")
        assert "**Host:**" in content

    def test_diagnose_never_runs_a_mutating_command(self, capsys, monkeypatch):
        """Diagnosis must be read-only. Nothing it runs may change state."""
        seen: list[list[str]] = []
        import medic.core.shell as shell

        original = shell.run

        def spy(argv, **kwargs):
            seen.append(list(argv))
            return original(argv, **kwargs)

        monkeypatch.setattr(shell, "run", spy)
        main(["diagnose", "--offline", "--json"])

        forbidden = ("rm", "mv", "dd", "mkfs", "shutdown", "reboot", "kill", "chmod", "chown")
        mutating = {
            "clean", "install", "upgrade", "remove", "purge", "restart",
            "vacuum", "prune", "set-ntp", "flush-caches",
        }
        # A mutating verb is acceptable only when paired with a flag that
        # makes the command simulate instead of act - that is how package
        # managers are asked "what would you change?".
        simulating = {"--just-print", "--dry-run", "--simulate", "-s", "--assume-no"}

        for argv in seen:
            assert argv[0] not in forbidden, f"diagnose ran a destructive command: {argv}"
            verbs = mutating & set(argv)
            if verbs:
                assert simulating & set(argv), (
                    f"diagnose ran a mutating command without a simulation flag: {argv}"
                )
            if any(flag.startswith("--vacuum") for flag in argv):
                raise AssertionError(f"diagnose ran a journal vacuum: {argv}")


class TestFix:
    def test_requires_an_explicit_selection(self, capsys):
        assert main(["fix"]) == EXIT_USAGE
        assert "name at least one fix" in capsys.readouterr().out

    def test_unknown_fix_is_a_usage_error(self, capsys):
        assert main(["fix", "no.such.fix"]) == EXIT_USAGE

    def test_preview_is_the_default(self, capsys):
        main(["fix", "--all", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True

    def test_preview_never_applies_anything(self, capsys):
        main(["fix", "--all", "--risk", "risky", "--json"])
        payload = json.loads(capsys.readouterr().out)

        for result in payload["results"]:
            assert result["applied"] is False

    def test_risk_limit_excludes_risky_fixes(self, capsys):
        main(["fix", "--all", "--risk", "safe", "--json"])
        payload = json.loads(capsys.readouterr().out)

        excluded_ids = {item["fix_id"] for item in payload["excluded"]}
        assert "updates.apply" in excluded_ids

    def test_apply_without_yes_needs_a_terminal(self, capsys, monkeypatch):
        """Guards against hanging on input() when run from a script."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        assert main(["fix", "clean.user-cache", "--apply"]) == EXIT_USAGE
        assert "needs a terminal" in capsys.readouterr().err


class TestExplain:
    def test_explains_a_check(self, capsys):
        assert main(["explain", "disk.space"]) == EXIT_OK
        output = capsys.readouterr().out
        assert "Check: disk.space" in output
        assert "medic diagnose --only disk.space" in output

    def test_explains_a_fix_including_how_to_apply(self, capsys):
        assert main(["explain", "clean.user-cache"]) == EXIT_OK
        output = capsys.readouterr().out
        assert "risk:" in output
        assert "--apply" in output

    def test_unknown_id_is_a_usage_error(self, capsys):
        assert main(["explain", "nonsense"]) == EXIT_USAGE


class TestConfigCommand:
    def test_shows_defaults(self, capsys):
        assert main(["config"]) == EXIT_OK
        assert "disk_warn_percent" in capsys.readouterr().out

    def test_init_writes_a_file(self, tmp_path, capsys):
        target = tmp_path / "config.json"
        assert main(["config", "--init", "--config", str(target)]) == EXIT_OK

        written = json.loads(target.read_text())
        assert "disk_warn_percent" in written


class TestHistory:
    def test_empty_history_is_not_an_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "medic.core.journal.journal_path", lambda: str(tmp_path / "journal.jsonl")
        )
        assert main(["history"]) == EXIT_OK


class TestTopLevel:
    def test_no_command_prints_help(self, capsys):
        assert main([]) == EXIT_USAGE
        assert "diagnose" in capsys.readouterr().out

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert "medic" in capsys.readouterr().out
