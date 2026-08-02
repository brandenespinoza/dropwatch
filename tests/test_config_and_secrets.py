"""Configuration validation and credential redaction."""

from __future__ import annotations

import copy
import io
import json
import logging
import pickle
import sys
import traceback
from pathlib import Path

import pytest

from dropwatch.config import (
    invocation_name,
    load_config,
    load_dotenv,
    normalize_url,
    warn_if_world_readable,
)
from dropwatch.errors import ConfigError
from dropwatch.logging_setup import RedactionFilter, setup_logging
from dropwatch.secrets import REDACTED, Secret, registry, scrub_text

SAMPLE_SECRET = "aVeryLongFakeSecretValue0123456789abcdef"
PASSWORD = "hunter2-not-real-password"


@pytest.fixture(autouse=True)
def clean_registry():
    registry.clear()
    yield
    registry.clear()


class TestUrlValidation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("http://example:4533", "http://example:4533"),
            ("http://example:4533/", "http://example:4533"),
            ("example:4533", "http://example:4533"),
            ("https://music.example.ts.net", "https://music.example.ts.net"),
            ("http://example:4533/music", "http://example:4533/music"),
            ("http://example:4533/rest", "http://example:4533"),  # common mix-up
            ("http://your-server.tail1234.ts.net:4533", "http://your-server.tail1234.ts.net:4533"),
        ],
    )
    def test_accepted_forms(self, raw, expected):
        assert normalize_url(raw) == expected

    def test_rest_path_is_appended_once(self):
        from dropwatch.config import Config
        from dropwatch.secrets import Secret as S

        config = Config(
            navidrome_url=normalize_url("http://example:4533/rest"),
            navidrome_username="u",
            navidrome_password=S("p"),
        )
        assert config.rest_base == "http://example:4533/rest"

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "ftp://your-server", "http://", "http://example:4533?x=1", "http://your-server:99999"],
    )
    def test_rejected_forms(self, raw):
        with pytest.raises(ConfigError):
            normalize_url(raw)

    def test_credentials_in_url_are_rejected(self):
        with pytest.raises(ConfigError, match="Do not put credentials"):
            normalize_url("http://user:pass@example:4533")

    def test_no_localhost_default_is_assumed(self):
        with pytest.raises(ConfigError):
            normalize_url("")


class TestSecret:
    def test_str_and_repr_are_redacted(self):
        secret = Secret(SAMPLE_SECRET)
        assert SAMPLE_SECRET not in str(secret)
        assert SAMPLE_SECRET not in repr(secret)
        assert SAMPLE_SECRET not in f"{secret}"
        assert SAMPLE_SECRET not in "{}".format(secret)
        assert SAMPLE_SECRET not in f"{secret!r}"

    def test_value_is_reachable_only_explicitly(self):
        assert Secret(SAMPLE_SECRET).reveal() == SAMPLE_SECRET

    def test_cannot_be_pickled_or_copied(self):
        with pytest.raises(TypeError):
            pickle.dumps(Secret(SAMPLE_SECRET))
        with pytest.raises(TypeError):
            copy.deepcopy(Secret(SAMPLE_SECRET))

    def test_not_json_serialisable(self):
        with pytest.raises(TypeError):
            json.dumps({"password": Secret(SAMPLE_SECRET)})

    def test_absent_from_tracebacks(self):
        # Tracebacks render locals with repr, so a leak here would be silent.
        def explode(secret):
            raise RuntimeError("boom")

        try:
            explode(Secret(SAMPLE_SECRET))
        except RuntimeError:
            text = "".join(traceback.format_exc())
        assert SAMPLE_SECRET not in text


class TestRedaction:
    def test_registered_values_are_scrubbed(self):
        registry.register(Secret(SAMPLE_SECRET))
        assert SAMPLE_SECRET not in scrub_text(f"sending {SAMPLE_SECRET} now")
        assert REDACTED in scrub_text(f"sending {SAMPLE_SECRET} now")

    def test_credential_query_parameters_are_scrubbed_even_if_unregistered(self):
        url = "http://example:4533/rest/ping.view?u=me&t=abc123def456&s=salt99"
        scrubbed = scrub_text(url)
        assert "abc123def456" not in scrubbed
        assert "salt99" not in scrubbed
        assert "u=me" in scrubbed

    def test_short_values_are_not_registered(self):
        # Registering "abc" would redact ordinary words everywhere.
        registry.register("abc")
        assert scrub_text("abc def") == "abc def"

    def test_log_records_are_redacted(self):
        registry.register(Secret(PASSWORD))
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactionFilter())
        logger = logging.getLogger("redaction-test")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        logger.info("connecting with %s", PASSWORD)
        logger.debug("url http://x/rest/ping?p=%s", PASSWORD)
        assert PASSWORD not in stream.getvalue()

    def test_debug_logging_still_redacts(self):
        registry.register(Secret(SAMPLE_SECRET))
        setup_logging(2)
        logger = logging.getLogger("dropwatch.test")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactionFilter())
        logging.getLogger("dropwatch").handlers = [handler]
        logger.debug("password=%s", SAMPLE_SECRET)
        assert SAMPLE_SECRET not in stream.getvalue()


class TestLoadConfig:
    def _env_file(self, tmp_path, **overrides):
        values = {
            "DROPWATCH_URL": "http://example:4533",
            "DROPWATCH_USERNAME": "tester",
            "DROPWATCH_PASSWORD": PASSWORD,
        }
        values.update(overrides)
        path = tmp_path / ".env"
        path.write_text("\n".join(f"{k}={v}" for k, v in values.items() if v is not None))
        return path

    def test_loads_from_env_file(self, tmp_path):
        path = self._env_file(tmp_path)
        config = load_config(environ={"DROPWATCH_ENV": str(path)})
        assert config.navidrome_url == "http://example:4533"
        assert config.navidrome_password.reveal() == PASSWORD

    def test_real_environment_wins_over_file(self, tmp_path):
        path = self._env_file(tmp_path)
        config = load_config(
            environ={
                "DROPWATCH_ENV": str(path),
                "DROPWATCH_URL": "http://other:9000",
            },
        )
        assert config.navidrome_url == "http://other:9000"

    def test_missing_credentials_raise_config_error(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("DROPWATCH_URL=http://example:4533\n")
        with pytest.raises(ConfigError, match="username"):
            load_config(environ={"DROPWATCH_ENV": str(path)})

    def test_invalid_timeout_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="timeout"):
            load_config(
                environ={
                    "DROPWATCH_ENV": str(
                        self._env_file(tmp_path, DROPWATCH_TIMEOUT="-3")
                    )
                }
            )

    def test_dotenv_parsing_handles_quotes_comments_and_export(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text(
            "# a comment\n"
            'DROPWATCH_URL="http://example:4533"\n'
            "\n"
            "export DROPWATCH_USERNAME=tester\n"
            "DROPWATCH_PASSWORD='pass word'\n"
            "IGNORED_LINE\n"
        )
        values = load_dotenv(path)
        assert values["DROPWATCH_URL"] == "http://example:4533"
        assert values["DROPWATCH_USERNAME"] == "tester"
        assert values["DROPWATCH_PASSWORD"] == "pass word"
        assert "IGNORED_LINE" not in values

    def test_world_readable_env_file_warns(self, tmp_path):
        path = self._env_file(tmp_path)
        path.chmod(0o644)
        stream = io.StringIO()
        assert warn_if_world_readable(path, stream) is True
        assert "chmod 600" in stream.getvalue()

    def test_private_env_file_does_not_warn(self, tmp_path):
        path = self._env_file(tmp_path)
        path.chmod(0o600)
        stream = io.StringIO()
        assert warn_if_world_readable(path, stream) is False
        assert stream.getvalue() == ""


class TestEnvFileDiscovery:
    """An installed command runs from anywhere, so cwd cannot be the only source."""

    def _write(self, path: Path, url: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"DROPWATCH_URL={url}\nDROPWATCH_USERNAME=u\nDROPWATCH_PASSWORD=p\n"
        )
        return path

    def test_there_is_one_settings_path(self, tmp_path, monkeypatch):
        from dropwatch.config import settings_path

        monkeypatch.chdir(tmp_path)
        assert settings_path(environ={"DROPWATCH_CONFIG_DIR": "/cfg"}) == Path(
            "/cfg/.env"
        )

    def test_dropwatch_env_moves_it(self, tmp_path):
        from dropwatch.config import settings_path

        assert settings_path(
            environ={"DROPWATCH_ENV": "/elsewhere/other.env", "DROPWATCH_CONFIG_DIR": "/cfg"}
        ) == Path("/elsewhere/other.env")

    def test_falls_back_to_the_user_config_directory(self, tmp_path, monkeypatch):
        # The scenario that matters: installed command, run from elsewhere.
        cfg = tmp_path / "cfg"
        self._write(cfg / ".env", "http://example:4533")
        workdir = tmp_path / "somewhere-else"
        workdir.mkdir()
        monkeypatch.chdir(workdir)
        config = load_config(environ={"DROPWATCH_CONFIG_DIR": str(cfg)})
        assert config.navidrome_url == "http://example:4533"

    def test_a_dot_env_in_the_working_directory_is_ignored(self, tmp_path, monkeypatch):
        """.env files litter project directories. One must not decide which
        server gets scanned just because you happened to `cd` there."""
        cfg = tmp_path / "cfg"
        self._write(cfg / ".env", "http://mine:1")
        workdir = tmp_path / "some-unrelated-project"
        self._write(workdir / ".env", "http://not-mine:2")
        monkeypatch.chdir(workdir)
        config = load_config(environ={"DROPWATCH_CONFIG_DIR": str(cfg)})
        assert config.navidrome_url == "http://mine:1"

    def test_dropwatch_env_override_wins(self, tmp_path, monkeypatch):
        self._write(tmp_path / "cfg" / ".env", "http://mine:1")
        explicit = self._write(tmp_path / "custom.env", "http://from-override:3")
        config = load_config(
            environ={
                "DROPWATCH_ENV": str(explicit),
                "DROPWATCH_CONFIG_DIR": str(tmp_path / "cfg"),
            }
        )
        assert config.navidrome_url == "http://from-override:3"

    def test_a_dropwatch_env_pointing_nowhere_says_so(self, tmp_path):
        # A typo in the variable is a different problem from having no config,
        # and reporting it as the latter sends you to `setup` for no reason.
        with pytest.raises(ConfigError, match="No such configuration file"):
            load_config(environ={"DROPWATCH_ENV": str(tmp_path / "typo.env")})

    def test_missing_config_names_the_one_place_it_looked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["/usr/local/bin/dropwatch"])
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ConfigError) as excinfo:
            load_config(environ={"DROPWATCH_CONFIG_DIR": str(tmp_path / "cfg")})
        hint = excinfo.value.hint or ""
        assert "dropwatch setup" in hint
        assert str(tmp_path / "cfg" / ".env") in hint
        # The working directory is not a source, so naming it would mislead.
        assert str(tmp_path / ".env") not in hint

    def test_explicit_missing_file_is_an_error_not_a_silent_fallback(self, tmp_path):
        with pytest.raises(ConfigError, match="No such configuration file"):
            load_config(environ={"DROPWATCH_ENV": str(tmp_path / "nope.env")})

    def test_environment_alone_needs_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = load_config(
            environ={
                "DROPWATCH_URL": "http://example:4533",
                "DROPWATCH_USERNAME": "u",
                "DROPWATCH_PASSWORD": "p",
                "DROPWATCH_CONFIG_DIR": str(tmp_path / "cfg"),
            }
        )
        assert config.navidrome_username == "u"


class TestInvocationName:
    """Hint text must name the command the user actually typed."""

    @pytest.mark.parametrize(
        "argv0,expected",
        [
            ("/usr/local/bin/dropwatch", "dropwatch"),
            ("/Users/x/proj/dropwatch.py", "python3 dropwatch.py"),
            ("dropwatch.py", "python3 dropwatch.py"),
            ("/Users/x/proj/src/dropwatch/__main__.py", "python3 -m dropwatch"),
            ("", "dropwatch"),
        ],
    )
    def test_each_invocation_spelling(self, argv0, expected, monkeypatch):
        monkeypatch.setattr(sys, "argv", [argv0])
        assert invocation_name() == expected

    def test_unresolved_hint_is_copy_pasteable(self, monkeypatch, capsys):
        from dropwatch.models import DeezerArtist, UnresolvedArtist
        from dropwatch.report import print_unresolved

        monkeypatch.setattr(sys, "argv", ["/usr/local/bin/dropwatch"])
        print_unresolved(
            [UnresolvedArtist("Ghost", "ambiguous", [DeezerArtist(id="42", name="Ghost")])],
            sys.stderr,
        )
        err = capsys.readouterr().err
        assert "dropwatch fix" in err
        assert 'dropwatch map "<artist>" <id>' in err
        # This used to also assert that the package spelling never leaked into
        # hint text, back when the package and the command were spelled
        # differently and hints had printed the wrong one. A single-word name
        # removes that trap rather than guarding against it.

    def test_hint_follows_the_script_form_too(self, monkeypatch, capsys):
        from dropwatch.models import UnresolvedArtist
        from dropwatch.report import print_unresolved

        monkeypatch.setattr(sys, "argv", ["/Users/x/proj/dropwatch.py"])
        print_unresolved([UnresolvedArtist("Ghost", "ambiguous", [])], sys.stderr)
        assert "python3 dropwatch.py map" in capsys.readouterr().err
