"""CLI argument handling and exit codes."""

from __future__ import annotations

import pytest

from dropwatch.cli import _misplaced_scan_flags, build_parser, main
from dropwatch.errors import ExitCode


class TestNoSubcommand:
    """A bare invocation does nothing. Scanning has to be asked for by name."""

    def test_bare_invocation_prints_help_and_succeeds(self, capsys):
        assert main([]) == ExitCode.OK
        out = capsys.readouterr().out
        assert "scan" in out and "usage:" in out

    def test_bare_invocation_touches_nothing(self, capsys, monkeypatch):
        # No config is set here; a scan would fail on it. Help must not care.
        for key in ("NAVIDROME_URL", "NAVIDROME_USERNAME", "NAVIDROME_PASSWORD"):
            monkeypatch.delenv(key, raising=False)
        assert main([]) == ExitCode.OK
        assert "error" not in capsys.readouterr().err

    def test_global_flags_alone_still_do_nothing(self, capsys):
        assert main(["-v"]) == ExitCode.OK
        assert "usage:" in capsys.readouterr().out


class TestMisplacedScanFlags:
    """`dropwatch --since 2025` used to work; it must say where --since went."""

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["--since", "2025"], ["--since"]),
            (["--since=2025"], ["--since"]),
            (["--refresh"], ["--refresh"]),
            (["--type", "album", "--flat"], ["--type", "--flat"]),
            (["--artist", "Björk"], ["--artist"]),
        ],
    )
    def test_detected_before_a_subcommand(self, argv, expected):
        assert _misplaced_scan_flags(argv) == expected

    @pytest.mark.parametrize(
        "argv",
        [
            ["scan", "--since", "2025"],   # correct placement
            ["-v", "status"],
            ["--env-file", "x.env", "check"],
            ["status", "--decided"],
            [],
            ["--version"],
        ],
    )
    def test_not_flagged_when_placed_correctly(self, argv):
        assert _misplaced_scan_flags(argv) == []

    def test_the_error_names_the_flag_and_the_fix(self, capsys):
        assert main(["--since", "2025"]) == ExitCode.USAGE
        err = capsys.readouterr().err
        assert "--since" in err
        assert "scan --since 2025" in err

    def test_env_file_value_is_not_mistaken_for_a_subcommand(self):
        # "--env-file check.env" must not read "check.env" as ending the
        # top-level region early. It is a value, not a subcommand.
        assert _misplaced_scan_flags(["--env-file", "check.env", "--since", "2025"]) == [
            "--since"
        ]


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

