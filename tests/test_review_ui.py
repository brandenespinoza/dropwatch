"""Review workflow: ambiguous releases must be answerable, once."""

from __future__ import annotations

import sys

import pytest
from conftest import deezer_release, local_album, local_artist

from dropwatch.errors import ExitCode
from dropwatch.models import DECISION_MISSING, DECISION_OWNED, Ownership, ReleaseType
from dropwatch.release_match import LocalIndex, determine_ownership
from dropwatch.review_ui import run_review


@pytest.fixture
def queued(store):
    store.save_review(
        [
            {
                "id": "555",
                "artist": "Alabama",
                "title": "American Christmas",
                "type": "Album",
                "date": "2017-10-06",
                "reason": "title closely resembles local album 'Christmas'",
                "url": "https://www.deezer.com/album/555",
            }
        ]
    )
    return store


def drive(monkeypatch, answers):
    queue = list(answers)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": queue.pop(0) if queue else "q")


class TestDecisions:
    def test_mark_owned(self, queued, monkeypatch):
        drive(monkeypatch, ["o"])
        assert run_review(queued) == ExitCode.OK
        assert queued.release_decisions() == {"555": DECISION_OWNED}

    def test_mark_missing(self, queued, monkeypatch):
        drive(monkeypatch, ["m"])
        run_review(queued)
        assert queued.release_decisions() == {"555": DECISION_MISSING}

    def test_skip_records_nothing(self, queued, monkeypatch, capsys):
        drive(monkeypatch, ["s"])
        run_review(queued)
        assert queued.release_decisions() == {}
        assert "Nothing changed" in capsys.readouterr().out

    def test_enter_is_skip(self, queued, monkeypatch):
        drive(monkeypatch, [""])
        run_review(queued)
        assert queued.release_decisions() == {}

    def test_undo_clears_a_previous_decision(self, queued, monkeypatch):
        queued.set_release_decision("555", DECISION_OWNED)
        drive(monkeypatch, ["u"])
        run_review(queued)
        assert queued.release_decisions() == {}

    def test_garbage_reprompts(self, queued, monkeypatch, capsys):
        drive(monkeypatch, ["what", "o"])
        run_review(queued)
        assert queued.release_decisions() == {"555": DECISION_OWNED}
        assert "Enter o, m, s, u or q" in capsys.readouterr().out

    def test_quit_keeps_earlier_decisions(self, store, monkeypatch):
        store.save_review(
            [
                {"id": "1", "artist": "A", "title": "T1", "type": "Album",
                 "date": "2024-01-01", "reason": "r", "url": ""},
                {"id": "2", "artist": "A", "title": "T2", "type": "Album",
                 "date": "2024-01-01", "reason": "r", "url": ""},
            ]
        )
        drive(monkeypatch, ["o", "q"])
        run_review(store)
        assert store.release_decisions() == {"1": DECISION_OWNED}

    def test_current_decision_is_shown(self, queued, monkeypatch, capsys):
        queued.set_release_decision("555", DECISION_OWNED)
        drive(monkeypatch, ["s"])
        run_review(queued)
        assert "currently marked: owned" in capsys.readouterr().out


class TestGuards:
    def test_needs_a_terminal(self, queued, monkeypatch, capsys):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert run_review(queued) == ExitCode.CONFIG
        assert "interactive terminal" in capsys.readouterr().err

    def test_empty_queue(self, store, monkeypatch, capsys):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        assert run_review(store) == ExitCode.OK
        assert "Nothing to review" in capsys.readouterr().out


class TestDecisionsAffectOwnership:
    """A recorded decision must actually change the next scan's verdict."""

    def _setup(self):
        index = LocalIndex(local_artist("A", [local_album("Christmas", "A", 2000)]))
        release = deezer_release("American Christmas", release_id="555", date="2017-10-06")
        return release, index

    def test_without_a_decision_it_is_ambiguous(self):
        release, index = self._setup()
        verdict = determine_ownership(release, index, ReleaseType.ALBUM, {})
        assert verdict.ownership is Ownership.AMBIGUOUS

    def test_owned_decision_suppresses_it(self):
        release, index = self._setup()
        verdict = determine_ownership(
            release, index, ReleaseType.ALBUM, {"555": DECISION_OWNED}
        )
        assert verdict.ownership is Ownership.IGNORED
        assert not verdict.reportable

    def test_missing_decision_forces_it_into_the_report(self):
        release, index = self._setup()
        verdict = determine_ownership(
            release, index, ReleaseType.ALBUM, {"555": DECISION_MISSING}
        )
        assert verdict.ownership is Ownership.MISSING
        assert verdict.reportable

    def test_a_decision_for_another_release_is_ignored(self):
        release, index = self._setup()
        verdict = determine_ownership(
            release, index, ReleaseType.ALBUM, {"999": DECISION_OWNED}
        )
        assert verdict.ownership is Ownership.AMBIGUOUS


class TestDecideOneRelease:
    """Ignoring a specific album from the results list, not just the queue."""

    def _provider(self):
        class P:
            def album_summary(self, release_id):
                return {
                    "title": "American Christmas",
                    "artist": {"name": "Alabama"},
                    "release_date": "2017-10-06",
                    "record_type": "album",
                }

        return P()

    def test_own_by_bare_id(self, store, capsys):
        from dropwatch.review_ui import decide_one

        assert decide_one(store, self._provider(), "558123", "owned") == ExitCode.OK
        assert store.release_decisions() == {"558123": DECISION_OWNED}
        assert "Marked Alabama — American Christmas as owned" in capsys.readouterr().out

    def test_own_by_url_from_the_results_list(self, store):
        from dropwatch.review_ui import decide_one

        url = "https://www.deezer.com/album/558123"
        decide_one(store, self._provider(), url, "owned")
        assert store.release_decisions() == {"558123": DECISION_OWNED}

    def test_localised_url_form(self, store):
        from dropwatch.review_ui import decide_one

        decide_one(store, self._provider(), "https://www.deezer.com/en/album/558123", "owned")
        assert store.release_decisions() == {"558123": DECISION_OWNED}

    def test_missing_forces_it_into_the_report(self, store):
        from dropwatch.review_ui import decide_one

        decide_one(store, self._provider(), "558123", "missing")
        assert store.release_decisions() == {"558123": DECISION_MISSING}

    def test_clear_undoes_it(self, store, capsys):
        from dropwatch.review_ui import decide_one

        decide_one(store, self._provider(), "558123", "owned")
        decide_one(store, self._provider(), "558123", "clear")
        assert store.release_decisions() == {}

    def test_clear_when_nothing_stored(self, store, capsys):
        from dropwatch.review_ui import decide_one

        assert decide_one(store, self._provider(), "558123", "clear") == ExitCode.OK
        assert "No decision was stored" in capsys.readouterr().out

    def test_garbage_target_is_rejected(self, store, capsys):
        from dropwatch.errors import ExitCode as EC
        from dropwatch.review_ui import decide_one

        assert decide_one(store, self._provider(), "not-an-id", "owned") == EC.USAGE
        assert store.release_decisions() == {}
        assert "not a Deezer album id or URL" in capsys.readouterr().err

    def test_works_without_a_provider(self, store, capsys):
        from dropwatch.review_ui import decide_one

        assert decide_one(store, None, "558123", "owned") == ExitCode.OK
        assert store.release_decisions() == {"558123": DECISION_OWNED}

    def test_queue_entry_is_preferred_over_a_fetch(self, queued, capsys):
        from dropwatch.review_ui import decide_one

        decide_one(queued, self._provider(), "555", "owned")
        assert "American Christmas" in capsys.readouterr().out


class TestExtractAlbumId:
    def test_forms(self):
        from dropwatch.review_ui import extract_album_id

        assert extract_album_id("558123") == "558123"
        assert extract_album_id("https://www.deezer.com/album/558123") == "558123"
        assert extract_album_id("https://www.deezer.com/fr/album/558123?x=1") == "558123"
        assert extract_album_id("nonsense") is None
        assert extract_album_id("") is None
        # An artist URL is not an album URL.
        assert extract_album_id("https://www.deezer.com/artist/558123") is None
