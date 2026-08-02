"""Settings storage, provenance, and the setup/config commands."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from dropwatch.cli import main
from dropwatch.config import (
    SETTINGS,
    SETTINGS_BY_KEY,
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


def _file_keys(path) -> set[str]:
    """Config keys actually present in the settings file."""
    from dropwatch.config import SETTINGS_BY_ENV, load_dotenv
    from dropwatch.config import settings_path

    values = load_dotenv(settings_path())
    return {SETTINGS_BY_ENV[k].key for k in values if k in SETTINGS_BY_ENV}


class TestSaveSettings:
    def test_file_is_private(self, tmp_path):
        path = tmp_path / "cfg" / ".env"
        save_settings(path, {"DROPWATCH_URL": "http://example:4533"})
        assert path.stat().st_mode & 0o077 == 0
        assert path.parent.stat().st_mode & 0o077 == 0

    def test_roundtrip(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(path, {"DROPWATCH_URL": "http://example:4533", "DROPWATCH_TYPES": "album"})
        values = load_dotenv(path)
        assert values["DROPWATCH_URL"] == "http://example:4533"
        assert values["DROPWATCH_TYPES"] == "album"

    def test_empty_values_are_omitted(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(path, {"DROPWATCH_URL": "http://a:1", "DROPWATCH_TYPES": ""})
        assert "DROPWATCH_TYPES" not in load_dotenv(path)

    def test_hand_added_keys_are_preserved(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("DROPWATCH_URL=http://a:1\nMY_OWN_NOTE=keepme\n")
        save_settings(path, {"DROPWATCH_URL": "http://b:2"})
        values = load_dotenv(path)
        assert values["MY_OWN_NOTE"] == "keepme"
        assert values["DROPWATCH_URL"] == "http://b:2"

    def test_write_is_atomic(self, tmp_path, monkeypatch):
        # A crash mid-write must not destroy a working config.
        path = tmp_path / ".env"
        save_settings(path, {"DROPWATCH_URL": "http://good:1"})

        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(RuntimeError):
            save_settings(path, {"DROPWATCH_URL": "http://bad:2"})
        assert load_dotenv(path)["DROPWATCH_URL"] == "http://good:1"
        leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".env.")]
        assert leftovers == [], "temporary files must be cleaned up"


class TestProvenance:
    def test_environment_beats_file(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(path, {"DROPWATCH_URL": "http://from-file:1"})
        rows = {r.setting.key: r for r in describe_settings({"DROPWATCH_ENV": str(path), "DROPWATCH_URL": "http://from-env:2"})}
        assert rows["url"].value == "http://from-env:2"
        assert rows["url"].source == "environment"

    def test_file_is_reported_by_path(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(path, {"DROPWATCH_URL": "http://example:4533"})
        rows = {r.setting.key: r for r in describe_settings({"DROPWATCH_ENV": str(path)})}
        assert rows["url"].source == str(path)

    def test_unset_optional_falls_back_to_default(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(path, {"DROPWATCH_URL": "http://example:4533"})
        rows = {r.setting.key: r for r in describe_settings({"DROPWATCH_ENV": str(path)})}
        assert rows["timeout"].value == "20"
        assert rows["timeout"].source == "default"

    def test_password_is_masked_in_display(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(path, {"DROPWATCH_PASSWORD": "hunter2-not-real"})
        rows = {r.setting.key: r for r in describe_settings({"DROPWATCH_ENV": str(path)})}
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
        assert values["DROPWATCH_URL"] == "http://example:4533"
        assert values["DROPWATCH_USERNAME"] == "branden"
        assert values["DROPWATCH_TYPES"] == "album,ep"

    def test_unset(self, cfg, capsys):
        main(["config", "set", "types", "album"])
        assert main(["config", "unset", "types"]) == ExitCode.OK
        assert "DROPWATCH_TYPES" not in load_dotenv(cfg)

    def test_list_reports_what_is_missing(self, cfg, capsys):
        main(["config"])
        captured = capsys.readouterr()
        assert "Not configured" in captured.err
        assert "setup" in captured.err
        # The advice must not contaminate the listing itself.
        assert "Not configured" not in captured.out

    def test_list_warns_when_the_environment_masks_the_file(self, cfg, monkeypatch, capsys):
        main(["config", "set", "url", "example:4533"])
        monkeypatch.setenv("DROPWATCH_URL", "http://override:9")
        capsys.readouterr()
        main(["config"])
        out = capsys.readouterr().out
        assert "http://override:9" in out
        # The column names the variable doing the masking, so it can be unset.
        assert "$DROPWATCH_URL" in out

    def test_set_notes_an_environment_override(self, cfg, monkeypatch, capsys):
        monkeypatch.setenv("DROPWATCH_URL", "http://override:9")
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

    def test_missing_env_file_hint_points_at_setup(self, cfg, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("DROPWATCH_ENV", str(tmp_path / "nope.env"))
        assert main(["check"]) == ExitCode.CONFIG
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
                "DROPWATCH_PASSWORD": "not-a-real-password",
            }
        )
        assert config.release_types == frozenset({"Album", "EP"})

    def test_workers_setting_is_gone(self):
        from dropwatch.config import SETTINGS

        assert "workers" not in {s.key for s in SETTINGS}


class TestValidationOnWrite:
    """A bad value is refused by the command that caused it.

    Every one of these used to be accepted, written to the file, reported as
    success, and then blow up on some unrelated later command.
    """

    @pytest.mark.parametrize(
        "key,value,message",
        [
            ("timeout", "abc", "must be a number"),
            ("timeout", "0", "greater than 0"),
            ("timeout", "-3", "greater than 0"),
            ("cache-max-age", "nope", "must be a number"),
            ("cache-max-age", "-5", "greater than 0"),
            ("types", "bogus", "Unknown release type"),
            ("url", "ftp://host", "http or https"),
            ("url", "http://", "hostname"),
        ],
    )
    def test_rejected_and_nothing_written(self, cfg, capsys, key, value, message):
        assert main(["config", "set", key, value]) == ExitCode.CONFIG
        assert message in capsys.readouterr().err
        assert key not in _file_keys(cfg)

    @pytest.mark.parametrize(
        "key,given,stored",
        [
            ("timeout", "30.0", "30"),
            ("cache-max-age", "12.50", "12.5"),
            ("types", "SINGLES, albums", "album,single"),
            ("url", "example:4533", "http://example:4533"),
            ("url", "http://example:4533/rest", "http://example:4533"),
        ],
    )
    def test_accepted_values_are_canonicalised(self, cfg, capsys, key, given, stored):
        assert main(["config", "set", key, given]) == ExitCode.OK
        rows = {r.setting.key: r for r in describe_settings()}
        assert rows[key].value == stored


class TestUnset:
    def test_unsetting_a_required_setting_warns(self, cfg, capsys):
        main(["config", "set", "url", "http://example:4533"])
        capsys.readouterr()
        assert main(["config", "unset", "url"]) == ExitCode.OK
        assert "required" in capsys.readouterr().err

    def test_unsetting_an_optional_one_names_the_default(self, cfg, capsys):
        main(["config", "set", "timeout", "45"])
        capsys.readouterr()
        assert main(["config", "unset", "timeout"]) == ExitCode.OK
        assert "Back to the default: 20" in capsys.readouterr().out


class TestEnvVarNaming:
    """The variable is derived from the key, so the two can never drift."""

    def test_every_setting_is_namespaced(self):
        for setting in SETTINGS:
            assert setting.env_var.startswith("DROPWATCH_")

    def test_the_variable_is_the_key(self):
        assert SETTINGS_BY_KEY["cache-max-age"].env_var == "DROPWATCH_CACHE_MAX_AGE"
        assert SETTINGS_BY_KEY["url"].env_var == "DROPWATCH_URL"

    def test_a_generic_shell_variable_cannot_win(self, cfg, monkeypatch):
        """The bug this prevents: an unrelated CACHE_PATH redirecting state."""
        monkeypatch.setenv("CACHE_PATH", "/tmp/somebody-elses.db")
        monkeypatch.setenv("NAVIDROME_PASSWORD", "not-mine")
        rows = {r.setting.key: r for r in describe_settings()}
        assert rows["cache-path"].source != "environment"
        assert rows["password"].source != "environment"


class TestDisplay:
    def test_settings_with_computed_defaults_are_not_shown_as_unset(self, cfg, capsys):
        main(["config"])
        out = capsys.readouterr().out
        # cache-path has no literal default but always has an effective value.
        assert "cache-path" in out
        assert "(not set)" not in out.split("cache-path")[1].split("\n")[0]
        assert "state.sqlite3" in out


class TestValueRoundTrip:
    """Whatever is saved must read back byte-identical.

    Passwords are why. A value written bare and read back stripped meant
    `setup` could test a password against the server, report success, save a
    different string, and leave every later command failing authentication
    with nothing to explain it.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "hunter2",
            "hunter2 ",                 # trailing space
            " hunter2",                 # leading space
            "   ",                      # only spaces
            '"hunter2"',                # literal double quotes
            "'hunter2'",                # literal single quotes
            "a=b=c",                    # equals signs
            "pa#ss",                    # a hash mid-value
            "#hunter2",                 # a leading hash
            "one\ntwo",                 # a newline would have split the line
            "back\\slash",
            "tab\there",
            "üñïçø∂é",
            "export FOO=bar",           # looks like a directive
        ],
    )
    def test_survives_save_and_load(self, tmp_path, value):
        path = tmp_path / ".env"
        save_settings(path, {"DROPWATCH_PASSWORD": value})
        assert load_dotenv(path)["DROPWATCH_PASSWORD"] == value

    def test_a_newline_does_not_corrupt_neighbouring_keys(self, tmp_path):
        path = tmp_path / ".env"
        save_settings(
            path, {"DROPWATCH_PASSWORD": "one\ntwo", "DROPWATCH_USERNAME": "me"}
        )
        values = load_dotenv(path)
        assert values["DROPWATCH_PASSWORD"] == "one\ntwo"
        assert values["DROPWATCH_USERNAME"] == "me"

    def test_hand_written_forms_still_parse(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text(
            "# a comment\n"
            "export DROPWATCH_URL=http://example:4533\n"
            "DROPWATCH_USERNAME = spaced \n"
            "DROPWATCH_PASSWORD='literal single'\n"
        )
        values = load_dotenv(path)
        assert values["DROPWATCH_URL"] == "http://example:4533"
        assert values["DROPWATCH_USERNAME"] == "spaced"
        assert values["DROPWATCH_PASSWORD"] == "literal single"

    def test_edge_whitespace_survives_into_the_config(self, tmp_path, monkeypatch):
        """load_config strips a url but must not strip a password."""
        from dropwatch.config import load_config

        path = tmp_path / ".env"
        save_settings(
            path,
            {
                "DROPWATCH_URL": "http://example:4533",
                "DROPWATCH_USERNAME": "me",
                "DROPWATCH_PASSWORD": " pad ded ",
            },
        )
        config = load_config(environ={"DROPWATCH_ENV": str(path)})
        assert config.navidrome_password.reveal() == " pad ded "


class TestSetupPreservesTheFile:
    def test_settings_it_does_not_ask_about_are_kept(self, tmp_path):
        from dropwatch.setup_wizard import _save

        path = tmp_path / ".env"
        save_settings(
            path,
            {
                "DROPWATCH_URL": "http://old:1",
                "DROPWATCH_USERNAME": "old",
                "DROPWATCH_PASSWORD": "old",
                "DROPWATCH_TYPES": "album,ep",
                "DROPWATCH_TIMEOUT": "45",
            },
        )
        _save(path, "http://new:2", "new", "new")
        values = load_dotenv(path)
        assert values["DROPWATCH_URL"] == "http://new:2"
        assert values["DROPWATCH_TYPES"] == "album,ep"
        assert values["DROPWATCH_TIMEOUT"] == "45"

    def test_a_shadowed_setting_is_not_dropped(self, tmp_path, monkeypatch):
        """The specific loss: rebuilding the file from resolved settings saw
        `types` as coming from the environment and wrote nothing for it."""
        from dropwatch.setup_wizard import _save

        path = tmp_path / ".env"
        save_settings(
            path,
            {
                "DROPWATCH_URL": "http://old:1",
                "DROPWATCH_USERNAME": "u",
                "DROPWATCH_PASSWORD": "p",
                "DROPWATCH_TYPES": "album,ep",
            },
        )
        monkeypatch.setenv("DROPWATCH_TYPES", "single")
        _save(path, "http://old:1", "u", "p")
        assert load_dotenv(path)["DROPWATCH_TYPES"] == "album,ep"
