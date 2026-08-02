"""`block` / `unblock`, and running local-only commands without a server."""

from __future__ import annotations

import pytest

from dropwatch import block_cmd
from dropwatch.cli import main
from dropwatch.config import load_config
from dropwatch.errors import ConfigError, ExitCode, DropWatchError
from dropwatch.models import (
    DECISION_BLOCKED,
    DECISION_OWNED,
    DeezerArtist,
    Ownership,
)
from dropwatch.state import MappingTarget, Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.sqlite3") as opened:
        yield opened


class FakeProvider:
    """Only the two calls block_cmd makes of a provider."""

    def __init__(self, artists=None, albums=None, fail=False):
        self.artists = artists or {}
        self.albums = albums or {}
        self.fail = fail

    def get_artist(self, deezer_id):
        if self.fail:
            raise DropWatchError("network is down")
        return self.artists.get(str(deezer_id))

    def album_summary(self, album_id):
        return self.albums.get(str(album_id))


class TestArtistTargetSpelling:
    """A local name, a Deezer id and a Deezer URL all name the same artist."""

    def test_a_plain_name_is_taken_verbatim(self, store):
        names, error = block_cmd.resolve_artist(store, None, "Karaoke Hits Vol 3")
        assert (names, error) == (["Karaoke Hits Vol 3"], None)

    def test_a_mapped_id_resolves_to_the_local_name(self, store):
        store.set_mapping("My Ghost", [MappingTarget("42", "Ghost")])
        names, error = block_cmd.resolve_artist(store, None, "42")
        assert (names, error) == (["My Ghost"], None)

    def test_a_mapped_url_resolves_to_the_local_name(self, store):
        store.set_mapping("My Ghost", [MappingTarget("42", "Ghost")])
        names, _ = block_cmd.resolve_artist(
            store, None, "https://www.deezer.com/us/artist/42"
        )
        assert names == ["My Ghost"]

    def test_every_local_artist_on_one_deezer_id_is_returned(self, store):
        store.set_mapping("Ghost", [MappingTarget("42", "Ghost")])
        store.set_mapping("Ghost B.C.", [MappingTarget("42", "Ghost")])
        names, _ = block_cmd.resolve_artist(store, None, "42")
        assert sorted(names) == ["Ghost", "Ghost B.C."]

    def test_an_unmapped_id_falls_back_to_the_deezer_name(self, store):
        provider = FakeProvider({"42": DeezerArtist(id="42", name="Ghost", nb_fan=1)})
        names, error = block_cmd.resolve_artist(store, provider, "42")
        assert (names, error) == (["Ghost"], None)

    def test_an_unknown_id_is_an_error(self, store):
        names, error = block_cmd.resolve_artist(store, FakeProvider(), "42")
        assert names == []
        assert "no artist with id 42" in error

    def test_a_deezer_failure_is_reported_not_raised(self, store):
        names, error = block_cmd.resolve_artist(store, FakeProvider(fail=True), "42")
        assert names == []
        assert "could not reach Deezer" in error


class TestBlockArtist:
    def test_blocking_by_id_marks_the_local_artist(self, store, capsys):
        store.set_mapping("My Ghost", [MappingTarget("42", "Ghost")])
        assert block_cmd.block_artist(store, None, "42") == ExitCode.OK
        assert store.get_mapping("My Ghost").is_blocked
        assert "My Ghost" in capsys.readouterr().out

    def test_the_deezer_id_survives_a_block(self, store):
        """So `unblock --artist <same id>` still finds the artist."""
        store.set_mapping("My Ghost", [MappingTarget("42", "Ghost")])
        block_cmd.block_artist(store, None, "42")
        assert store.get_mapping("My Ghost").deezer_ids == ["42"]
        assert block_cmd.unblock_artist(store, None, "42") == ExitCode.OK
        assert store.get_mapping("My Ghost") is None

    def test_unblock_leaves_a_mapping_that_is_not_blocked_alone(self, store, capsys):
        store.set_mapping("My Ghost", [MappingTarget("42", "Ghost")])
        assert block_cmd.unblock_artist(store, None, "My Ghost") == ExitCode.OK
        assert store.get_mapping("My Ghost") is not None
        out = capsys.readouterr().out
        assert "is not blocked" in out and "unmap" in out

    def test_unblock_of_an_unknown_artist_is_graceful(self, store, capsys):
        assert block_cmd.unblock_artist(store, None, "Nobody") == ExitCode.OK
        assert "not blocked" in capsys.readouterr().out


class TestBlockAlbum:
    def test_a_url_records_a_block_decision(self, store, capsys):
        provider = FakeProvider(
            albums={"77": {"title": "Some Record", "artist": {"name": "Ghost"}}}
        )
        code = block_cmd.block_album(
            store, provider, "https://www.deezer.com/album/77"
        )
        assert code == ExitCode.OK
        assert store.release_decisions() == {"77": DECISION_BLOCKED}
        assert "Ghost — Some Record" in capsys.readouterr().out

    def test_a_bare_id_works_too(self, store):
        block_cmd.block_album(store, None, "77")
        assert store.release_decisions() == {"77": DECISION_BLOCKED}

    def test_unblock_clears_it(self, store):
        block_cmd.block_album(store, None, "77")
        assert block_cmd.unblock_album(store, None, "77") == ExitCode.OK
        assert store.release_decisions() == {}

    def test_unblock_also_clears_an_owned_decision(self, store):
        """`unblock --album` is the undo for `review --own` as well."""
        store.set_release_decision("77", DECISION_OWNED)
        block_cmd.unblock_album(store, None, "77")
        assert store.release_decisions() == {}

    def test_junk_is_rejected(self, store, capsys):
        assert block_cmd.block_album(store, None, "wat") == ExitCode.USAGE
        assert "not a Deezer album id or URL" in capsys.readouterr().err
        assert store.release_decisions() == {}

    def test_a_blocked_release_is_not_reported(self):
        from dropwatch.release_match import LocalIndex, determine_ownership
        from dropwatch.models import (
            DeezerRelease,
            LocalArtist,
            ReleaseDate,
            ReleaseType,
        )

        release = DeezerRelease(
            id="77",
            title="Some Record",
            artist_id="1",
            artist_name="Ghost",
            release_date=ReleaseDate.parse("2025-01-01"),
        )
        index = LocalIndex(LocalArtist(id="1", name="Ghost", albums=[]))
        verdict = determine_ownership(
            release, index, ReleaseType.ALBUM, {"77": DECISION_BLOCKED}
        )
        assert verdict.ownership is Ownership.IGNORED
        assert not verdict.reportable


class TestRunsWithoutNavidrome:
    """Commands that only touch the state file must not demand credentials."""

    @pytest.fixture
    def state(self, tmp_path, monkeypatch):
        for key in (
            "DROPWATCH_URL",
            "DROPWATCH_USERNAME",
            "DROPWATCH_PASSWORD",
            "DROPWATCH_CACHE_PATH",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("DROPWATCH_CACHE_PATH", str(tmp_path / "state.sqlite3"))
        return tmp_path

    @pytest.mark.parametrize(
        "argv",
        [
            ["cache"],
            ["artists"],
            ["artists", "--mappings"],
            ["block", "--artist", "Some Artist"],
            ["block", "Some Artist"],
            ["unblock", "--artist", "Some Artist"],
            ["block", "--album", "77"],
            ["unblock", "--album", "77"],
            ["unmap", "Some Artist"],
        ],
    )
    def test_no_configuration_is_needed(self, state, argv):
        assert main(argv) == ExitCode.OK

    def test_scan_still_demands_configuration(self, state, capsys):
        assert main(["scan"]) == ExitCode.CONFIG
        assert "No configuration found" in capsys.readouterr().err

    def test_check_still_demands_configuration(self, state):
        assert main(["check"]) == ExitCode.CONFIG

    def test_the_relaxed_loader_records_what_is_missing(self, state):
        config = load_config(require_navidrome=False)
        assert config.has_navidrome is False
        # Reported as the key you would set, not the variable behind it.
        assert "url" in config.missing_navidrome

    def test_the_strict_loader_still_raises(self, state):
        with pytest.raises(ConfigError):
            load_config()

    def test_a_partial_configuration_is_tolerated(self, state, monkeypatch):
        monkeypatch.setenv("DROPWATCH_URL", "http://example:4533")
        config = load_config(require_navidrome=False)
        assert config.navidrome_url == "http://example:4533"
        assert config.missing_navidrome == ("username", "password")


class TestArgumentHandling:
    @pytest.fixture(autouse=True)
    def _state(self, tmp_path, monkeypatch):
        for key in ("DROPWATCH_URL", "DROPWATCH_USERNAME", "DROPWATCH_PASSWORD"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("DROPWATCH_CACHE_PATH", str(tmp_path / "state.sqlite3"))

    def test_nothing_given_is_a_usage_error(self, capsys):
        assert main(["block"]) == ExitCode.USAGE
        assert "nothing to block" in capsys.readouterr().err

    def test_an_artist_and_an_album_together_are_refused(self, capsys):
        assert main(["block", "Some Artist", "--album", "77"]) == ExitCode.USAGE
        assert "not both" in capsys.readouterr().err

    def test_artist_and_album_flags_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            main(["block", "--artist", "X", "--album", "77"])
