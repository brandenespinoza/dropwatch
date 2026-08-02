"""Interactive artist resolution."""

from __future__ import annotations

import sys

import pytest
from conftest import deezer_release

from dropwatch.errors import ExitCode
from dropwatch.models import DeezerArtist
from dropwatch.resolve_ui import _extract_id, _parse_selection, run_resolve
from dropwatch.state import MappingTarget


class StubProvider:
    def __init__(self, artists=None, discographies=None):
        self.artists = artists or {}
        self.discographies = discographies or {}

    def get_artist(self, artist_id):
        return self.artists.get(str(artist_id))

    def get_discography(self, artist_id):
        return list(self.discographies.get(str(artist_id), []))


@pytest.fixture
def provider():
    return StubProvider(
        artists={
            "1": DeezerArtist(id="1", name="Ghost", nb_fan=1_234_567),
            "2": DeezerArtist(id="2", name="Ghost", nb_fan=2145),
            "99": DeezerArtist(id="99", name="Ghost (Sweden)", nb_fan=500),
        },
        discographies={
            "1": [deezer_release("Meliora", release_id="m1")],
            "2": [deezer_release("Opus Eponymous", release_id="o1")],
        },
    )


@pytest.fixture
def unresolved(store):
    store.save_unresolved(
        [
            {
                "name": "Ghost",
                "reason": "several Deezer artists share this name",
                "candidates": [
                    {"id": "1", "name": "Ghost", "fans": 1_234_567},
                    {"id": "2", "name": "Ghost", "fans": 2145},
                ],
            }
        ]
    )
    return store


def drive(monkeypatch, answers):
    """Feed scripted answers to input(), pretending to be a terminal."""
    queue = list(answers)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": queue.pop(0) if queue else "q")


class TestSelectionParsing:
    @pytest.mark.parametrize(
        "answer,expected",
        [
            ("1", [0]),
            ("2", [1]),
            ("1 2", [0, 1]),
            ("1,2", [0, 1]),
            ("2 1", [1, 0]),
            ("1 1", [0]),
            ("  1   2  ", [0, 1]),
        ],
    )
    def test_valid(self, answer, expected):
        assert _parse_selection(answer, 3) == expected

    @pytest.mark.parametrize("answer", ["", "s", "q", "0", "4", "1 9", "abc", "1 x"])
    def test_invalid(self, answer):
        assert _parse_selection(answer, 3) is None


class TestIdExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1160651", "1160651"),
            ("  1160651 ", "1160651"),
            ("https://www.deezer.com/artist/1160651", "1160651"),
            ("https://www.deezer.com/en/artist/1160651", "1160651"),
            ("https://www.deezer.com/artist/1160651?x=1", "1160651"),
            ("not an id", None),
            ("", None),
        ],
    )
    def test_accepts_id_or_url(self, text, expected):
        assert _extract_id(text) == expected


class TestActions:
    def test_select_one(self, unresolved, provider, monkeypatch, capsys):
        drive(monkeypatch, ["1"])
        assert run_resolve(unresolved, provider, set()) == ExitCode.OK
        assert unresolved.get_mapping("Ghost").deezer_ids == ["1"]

    def test_select_several_merges_them(self, unresolved, provider, monkeypatch, capsys):
        drive(monkeypatch, ["1 2"])
        run_resolve(unresolved, provider, set())
        assert unresolved.get_mapping("Ghost").deezer_ids == ["1", "2"]
        assert "merged" in capsys.readouterr().out

    def test_skip_changes_nothing(self, unresolved, provider, monkeypatch, capsys):
        drive(monkeypatch, ["s"])
        run_resolve(unresolved, provider, set())
        assert unresolved.get_mapping("Ghost") is None
        assert "Nothing changed" in capsys.readouterr().out

    def test_enter_key_is_skip(self, unresolved, provider, monkeypatch):
        drive(monkeypatch, [""])
        run_resolve(unresolved, provider, set())
        assert unresolved.get_mapping("Ghost") is None

    def test_block(self, unresolved, provider, monkeypatch):
        drive(monkeypatch, ["b"])
        run_resolve(unresolved, provider, set())
        assert unresolved.get_mapping("Ghost").is_blocked

    def test_clear_returns_an_artist_to_unresolved(self, unresolved, provider, monkeypatch):
        unresolved.set_mapping("Ghost", [MappingTarget("1", "Ghost"), MappingTarget("2", "Ghost")])
        drive(monkeypatch, ["c"])
        run_resolve(unresolved, provider, set())
        assert unresolved.get_mapping("Ghost") is None

    def test_manual_id(self, unresolved, provider, monkeypatch):
        drive(monkeypatch, ["d 99"])
        run_resolve(unresolved, provider, set())
        assert unresolved.get_mapping("Ghost").deezer_ids == ["99"]

    def test_manual_url(self, unresolved, provider, monkeypatch):
        drive(monkeypatch, ["https://www.deezer.com/artist/99"])
        run_resolve(unresolved, provider, set())
        assert unresolved.get_mapping("Ghost").deezer_ids == ["99"]

    def test_unknown_id_reprompts_rather_than_saving(self, unresolved, provider, monkeypatch, capsys):
        drive(monkeypatch, ["d 12345", "s"])
        run_resolve(unresolved, provider, set())
        assert unresolved.get_mapping("Ghost") is None
        assert "no artist with id" in capsys.readouterr().out

    def test_garbage_reprompts(self, unresolved, provider, monkeypatch, capsys):
        drive(monkeypatch, ["nonsense", "1"])
        run_resolve(unresolved, provider, set())
        assert unresolved.get_mapping("Ghost").deezer_ids == ["1"]
        assert "Enter one or more numbers" in capsys.readouterr().out

    def test_out_of_range_reprompts(self, unresolved, provider, monkeypatch):
        drive(monkeypatch, ["9", "2"])
        run_resolve(unresolved, provider, set())
        assert unresolved.get_mapping("Ghost").deezer_ids == ["2"]

    def test_quit_stops_immediately(self, unresolved, provider, monkeypatch):
        drive(monkeypatch, ["q"])
        assert run_resolve(unresolved, provider, set()) == ExitCode.OK
        assert unresolved.get_mapping("Ghost") is None


class TestGuards:
    def test_needs_a_terminal(self, unresolved, provider, monkeypatch, capsys):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert run_resolve(unresolved, provider, set()) == ExitCode.CONFIG
        err = capsys.readouterr().err
        assert "interactive terminal" in err
        assert "map" in err, "must point at the scriptable alternative"

    def test_nothing_to_do(self, store, provider, monkeypatch, capsys):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        assert run_resolve(store, provider, set()) == ExitCode.OK
        assert "Nothing to resolve" in capsys.readouterr().out


class TestDisplay:
    def test_shared_album_titles_are_surfaced_first(self, unresolved, provider, monkeypatch, capsys):
        # The decisive evidence is which candidate matches the local library.
        drive(monkeypatch, ["s"])
        run_resolve(unresolved, provider, {"opus eponymous"})
        out = capsys.readouterr().out
        assert "Opus Eponymous" in out
        assert "Meliora" in out


class TestReopenByName:
    """A mapped artist never returns to the unresolved list, so it needs a way back."""

    def test_named_artist_is_searched_fresh(self, store, provider, monkeypatch, capsys):
        store.set_mapping("Ghost", [MappingTarget("1", "Ghost")])
        provider.search_results = {"Ghost": [DeezerArtist(id="2", name="Ghost", nb_fan=2145)]}
        provider.search_artists = lambda name, limit=6: provider.search_results.get(name, [])
        drive(monkeypatch, ["1"])
        assert run_resolve(store, provider, set(), only="Ghost") == ExitCode.OK
        assert store.get_mapping("Ghost").deezer_ids == ["2"], "the wrong mapping is replaced"

    def test_current_mapping_is_shown_before_choosing(self, store, provider, monkeypatch, capsys):
        store.set_mapping("Ghost", [MappingTarget("1", "Ghost")])
        provider.search_artists = lambda name, limit=6: []
        drive(monkeypatch, ["s"])
        run_resolve(store, provider, set(), only="Ghost")
        assert "currently mapped to" in capsys.readouterr().out

    def test_clear_works_on_a_named_artist(self, store, provider, monkeypatch):
        store.set_mapping("Ghost", [MappingTarget("1", "Ghost")])
        provider.search_artists = lambda name, limit=6: []
        drive(monkeypatch, ["c"])
        run_resolve(store, provider, set(), only="Ghost")
        assert store.get_mapping("Ghost") is None

    def test_works_with_an_empty_unresolved_queue(self, store, provider, monkeypatch, capsys):
        provider.search_artists = lambda name, limit=6: []
        drive(monkeypatch, ["s"])
        assert run_resolve(store, provider, set(), only="Nobody") == ExitCode.OK
        assert "Nothing to resolve" not in capsys.readouterr().out
