"""CLI argument handling and exit codes."""

from __future__ import annotations

import pytest

from dropwatch.cli import _insert_default_command, build_parser, main
from dropwatch.errors import ExitCode


class TestDefaultCommand:
    @pytest.mark.parametrize(
        "argv,expected_first",
        [
            ([], "scan"),
            (["-v"], "scan"),
            (["--artist", "Björk"], "scan"),
            (["--env-file", "x.env"], "scan"),
            (["--refresh"], "scan"),
            (["scan"], "scan"),
            (["check"], "check"),
            (["status"], "status"),
            (["fix"], "fix"),
            (["artists"], "artists"),  # legacy spelling still routes
            (["map", "Ghost", "42"], "map"),
        ],
    )
    def test_bare_flags_imply_scan(self, argv, expected_first):
        assert _insert_default_command(argv)[0] == expected_first

    def test_help_and_version_are_left_alone(self):
        assert _insert_default_command(["--help"]) == ["--help"]
        assert _insert_default_command(["--version"]) == ["--version"]

    def test_env_file_value_is_not_mistaken_for_a_command(self):
        # "check.env" must not be read as the `check` subcommand.
        assert _insert_default_command(["--env-file", "check.env"])[0] == "scan"


class TestParser:
    def test_scan_defaults(self):
        args = build_parser().parse_args(["scan"])
        assert args.command == "scan"
        assert args.artist == []
        assert args.refresh is False

    def test_global_flags_work_after_the_subcommand(self):
        args = build_parser().parse_args(["scan", "-v", "--artist", "X"])
        assert args.verbose == 1
        assert args.artist == ["X"]

    def test_repeated_verbose_counts(self):
        assert build_parser().parse_args(["-vv", "scan"]).verbose == 2

    def test_type_choices_are_validated(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["scan", "--type", "bogus"])

    def test_type_aliases_accepted(self):
        args = build_parser().parse_args(["scan", "--type", "albums", "--type", "single"])
        assert args.type == ["albums", "single"]


class TestExitCodes:
    def test_distinct_codes_for_distinct_failures(self):
        assert len(
            {
                ExitCode.OK,
                ExitCode.FAILURE,
                ExitCode.USAGE,
                ExitCode.CONFIG,
                ExitCode.NAVIDROME_CONNECTION,
                ExitCode.NAVIDROME_AUTH,
                ExitCode.DEEZER,
                ExitCode.PARTIAL,
            }
        ) == 8

    def test_missing_configuration_exits_with_config_code(self, tmp_path, monkeypatch, capsys):
        for key in ("NAVIDROME_URL", "NAVIDROME_USERNAME", "NAVIDROME_PASSWORD"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.chdir(tmp_path)
        code = main(["check"])
        assert code == ExitCode.CONFIG
        err = capsys.readouterr().err
        assert "NAVIDROME_URL" in err
        assert "error:" in err

    def test_error_hint_is_printed(self, tmp_path, monkeypatch, capsys):
        env = tmp_path / ".env"
        env.write_text("NAVIDROME_URL=ftp://your-server\nNAVIDROME_USERNAME=u\nNAVIDROME_PASSWORD=p\n")
        monkeypatch.chdir(tmp_path)
        for key in ("NAVIDROME_URL", "NAVIDROME_USERNAME", "NAVIDROME_PASSWORD"):
            monkeypatch.delenv(key, raising=False)
        assert main(["check", "--env-file", str(env)]) == ExitCode.CONFIG
        assert "http or https" in capsys.readouterr().err


class TestStateCommands:
    def _env(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text(
            "NAVIDROME_URL=http://example:4533\n"
            "NAVIDROME_USERNAME=u\n"
            "NAVIDROME_PASSWORD=p\n"
            f"CACHE_PATH={tmp_path / 'state.sqlite3'}\n"
        )
        for key in ("NAVIDROME_URL", "NAVIDROME_USERNAME", "NAVIDROME_PASSWORD", "CACHE_PATH"):
            monkeypatch.delenv(key, raising=False)
        return ["--env-file", str(env)]

    def test_block_and_unmap_roundtrip(self, tmp_path, monkeypatch, capsys):
        env = self._env(tmp_path, monkeypatch)
        assert main(["block", "Some Artist", *env]) == ExitCode.OK
        assert "Blocking" in capsys.readouterr().out
        assert main(["unmap", "Some Artist", *env]) == ExitCode.OK
        assert "Cleared" in capsys.readouterr().out

    def test_cache_status_reports_the_path(self, tmp_path, monkeypatch, capsys):
        env = self._env(tmp_path, monkeypatch)
        assert main(["cache", *env]) == ExitCode.OK
        assert "State file" in capsys.readouterr().out

    def test_status_without_a_scan_is_graceful(self, tmp_path, monkeypatch, capsys):
        env = self._env(tmp_path, monkeypatch)
        assert main(["status", *env]) == ExitCode.OK
        assert "No scan recorded yet" in capsys.readouterr().out

