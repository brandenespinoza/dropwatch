"""Settings storage, provenance, and the setup/config commands."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from dropwatch.cli import main
from dropwatch.config import (
    SETTINGS,
    describe_settings,
    load_dotenv,
    save_settings,
)
from dropwatch.errors import ExitCode


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """An isolated config directory, with the real environment neutralised."""
    directory = tmp_path / "cfg"
    monkeypatch.setenv("DROPWATCH_CONFIG_DIR", str(directory))
    for setting in SETTINGS:
        monkeypatch.delenv(setting.env_var, raising=False)
    monkeypatch.delenv("DROPWATCH_ENV", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/dropwatch"])
    return directory / ".env"


class TestSaveSettings:
    def test_file_is_private(self, tmp_path):
        path = tmp_path / "cfg" / ".env"
        save_settings(path, {"NAVIDROME_URL": "http://example:4533"})
        assert path.stat().st_mode & 0o077 == 0
        assert path.parent.stat().st_mode & 0o077 == 0

    def test_roundtrip(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(path, {"NAVIDROME_URL": "http://example:4533", "RELEASE_TYPES": "album"})
        values = load_dotenv(path)
        assert values["NAVIDROME_URL"] == "http://example:4533"
        assert values["RELEASE_TYPES"] == "album"

    def test_empty_values_are_omitted(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(path, {"NAVIDROME_URL": "http://a:1", "RELEASE_TYPES": ""})
        assert "RELEASE_TYPES" not in load_dotenv(path)

    def test_hand_added_keys_are_preserved(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("NAVIDROME_URL=http://a:1\nMY_OWN_NOTE=keepme\n")
        save_settings(path, {"NAVIDROME_URL": "http://b:2"})
        values = load_dotenv(path)
        assert values["MY_OWN_NOTE"] == "keepme"
        assert values["NAVIDROME_URL"] == "http://b:2"

    def test_write_is_atomic(self, tmp_path, monkeypatch):
        # A crash mid-write must not destroy a working config.
        path = tmp_path / ".env"
        save_settings(path, {"NAVIDROME_URL": "http://good:1"})

        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(RuntimeError):
            save_settings(path, {"NAVIDROME_URL": "http://bad:2"})
        assert load_dotenv(path)["NAVIDROME_URL"] == "http://good:1"
        leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".env.")]
        assert leftovers == [], "temporary files must be cleaned up"


class TestProvenance:
    def test_environment_beats_file(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(path, {"NAVIDROME_URL": "http://from-file:1"})
        rows = {r.setting.key: r for r in describe_settings(path, {"NAVIDROME_URL": "http://from-env:2"})}
        assert rows["url"].value == "http://from-env:2"
        assert rows["url"].source == "environment"

    def test_file_is_reported_by_path(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(path, {"NAVIDROME_URL": "http://example:4533"})
        rows = {r.setting.key: r for r in describe_settings(path, {})}
        assert rows["url"].source == str(path)

    def test_unset_optional_falls_back_to_default(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(path, {"NAVIDROME_URL": "http://example:4533"})
        rows = {r.setting.key: r for r in describe_settings(path, {})}
        assert rows["timeout"].value == "20"
        assert rows["timeout"].source == "default"

    def test_password_is_masked_in_display(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(path, {"NAVIDROME_PASSWORD": "hunter2-not-real"})
        rows = {r.setting.key: r for r in describe_settings(path, {})}
        assert rows["password"].display == "********"
        assert "hunter2-not-real" not in rows["password"].display


class TestConfigCommand:
    def test_set_and_list(self, cfg, capsys):
        assert main(["config", "set", "url", "example:4533"]) == ExitCode.OK
        capsys.readouterr()
        assert main(["config"]) == ExitCode.OK
        out = capsys.readouterr().out
        assert "http://example:4533" in out, "the URL should be normalised on the way in"

    def test_set_rejects_an_invalid_url(self, cfg, capsys):
        # main() turns DropWatchError into an exit code, it does not raise.
        assert main(["config", "set", "url", "ftp://nope"]) == ExitCode.CONFIG
        assert "http or https" in capsys.readouterr().err
        assert not cfg.exists(), "a rejected value must not be written"

    def test_password_is_refused_on_the_command_line(self, cfg, capsys):
        code = main(["config", "set", "password", "hunter2"])
        err = capsys.readouterr().err
        assert code == ExitCode.CONFIG
        assert "shell history" in err
        assert "config password" in err
        assert not cfg.exists(), "nothing should have been written"

    def test_password_never_reaches_the_file_via_set(self, cfg, capsys):
        main(["config", "set", "url", "example:4533"])
        main(["config", "set", "password", "hunter2"])
        assert "hunter2" not in cfg.read_text()

    def test_set_one_key_leaves_others_alone(self, cfg):
        main(["config", "set", "url", "example:4533"])
        main(["config", "set", "username", "branden"])
        main(["config", "set", "types", "album,ep"])
        values = load_dotenv(cfg)
        assert values["NAVIDROME_URL"] == "http://example:4533"
        assert values["NAVIDROME_USERNAME"] == "branden"
        assert values["RELEASE_TYPES"] == "album,ep"

    def test_unset(self, cfg, capsys):
        main(["config", "set", "types", "album"])
        assert main(["config", "unset", "types"]) == ExitCode.OK
        assert "RELEASE_TYPES" not in load_dotenv(cfg)

    def test_list_reports_what_is_missing(self, cfg, capsys):
        main(["config"])
        captured = capsys.readouterr()
        assert "Not configured" in captured.err
        assert "setup" in captured.err
        # The advice must not contaminate the listing itself.
        assert "Not configured" not in captured.out

    def test_list_warns_when_the_environment_masks_the_file(self, cfg, monkeypatch, capsys):
        main(["config", "set", "url", "example:4533"])
        monkeypatch.setenv("NAVIDROME_URL", "http://override:9")
        capsys.readouterr()
        main(["config"])
        out = capsys.readouterr().out
        assert "http://override:9" in out
        assert "environment" in out

    def test_set_notes_an_environment_override(self, cfg, monkeypatch, capsys):
        monkeypatch.setenv("NAVIDROME_URL", "http://override:9")
        main(["config", "set", "url", "example:4533"])
        assert "still" in capsys.readouterr().err

    def test_path(self, cfg, capsys):
        assert main(["config", "path"]) == ExitCode.OK
        assert capsys.readouterr().out.strip().endswith(".env")

    def test_bare_config_shows_settings(self, cfg, capsys):
        assert main(["config"]) == ExitCode.OK
        out = capsys.readouterr().out
        assert "url" in out
        assert "config set" in out, "should point at how to change one"

    def test_there_is_no_separate_list_subcommand(self, cfg):
        # `config` and `config list` doing the same thing was confusing.
        import pytest as _pytest

        with _pytest.raises(SystemExit):
            main(["config", "list"])


class TestSetupCommand:
    def test_requires_a_terminal(self, cfg, monkeypatch, capsys):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert main(["setup"]) == ExitCode.CONFIG
        err = capsys.readouterr().err
        assert "interactive terminal" in err
        assert "config set" in err, "must point at the scriptable alternative"

    def test_init_is_gone(self):
        from dropwatch.cli import SUBCOMMANDS

        assert "init" not in SUBCOMMANDS
        assert {"setup", "config"} <= SUBCOMMANDS


class TestHintsNameRealCommands:
    """A fresh install's first message must not point at a removed command."""

    def test_no_config_hint_points_at_setup(self, cfg, capsys):
        assert main(["check"]) == ExitCode.CONFIG
        hint = capsys.readouterr().err
        assert "setup" in hint
        assert "init" not in hint, "`init` was replaced by `setup`"

    def test_missing_env_file_hint_points_at_setup(self, cfg, capsys, tmp_path):
        assert main(["check", "--env-file", str(tmp_path / "nope.env")]) == ExitCode.CONFIG
        hint = capsys.readouterr().err
        assert "setup" in hint
        assert "init" not in hint

    def test_every_command_named_in_a_hint_exists(self, cfg, capsys):
        import re

        from dropwatch.cli import SUBCOMMANDS

        main(["check"])
        main(["config"])
        text = capsys.readouterr()
        blob = text.out + text.err
        # Pull `dropwatch <word>` style references out of the guidance.
        for match in re.finditer(r"dropwatch ([a-z-]+)", blob):
            assert match.group(1) in SUBCOMMANDS, f"hint names unknown command {match.group(1)!r}"


class TestPersistedTypeFilter:
    """The default output should match how the user actually collects."""

    def test_parse_accepts_aliases_and_spacing(self):
        from dropwatch.config import parse_release_types

        assert parse_release_types("album,ep") == frozenset({"Album", "EP"})
        assert parse_release_types("albums, singles") == frozenset({"Album", "Single"})
        assert parse_release_types(" EP ") == frozenset({"EP"})

    def test_empty_means_every_type(self):
        from dropwatch.config import parse_release_types

        assert parse_release_types("") == frozenset()
        assert parse_release_types(None) == frozenset()

    def test_unknown_type_is_rejected_with_the_valid_list(self):
        from dropwatch.config import ConfigError, parse_release_types

        with pytest.raises(ConfigError) as excinfo:
            parse_release_types("album,lp")
        assert "album" in (excinfo.value.hint or "")

    def test_setting_survives_a_roundtrip(self, cfg, capsys):
        from dropwatch.config import load_config

        main(["config", "set", "url", "example:4533"])
        main(["config", "set", "username", "branden"])
        main(["config", "set", "types", "album,ep"])
        capsys.readouterr()
        config = load_config(
            environ={
                "DROPWATCH_CONFIG_DIR": str(cfg.parent),
                "NAVIDROME_PASSWORD": "not-a-real-password",
            }
        )
        assert config.release_types == frozenset({"Album", "EP"})

    def test_workers_setting_is_gone(self):
        from dropwatch.config import SETTINGS

        assert "workers" not in {s.key for s in SETTINGS}
