"""`fix` and `status`, and the legacy spellings they replaced."""

from __future__ import annotations

import pytest

from dropwatch.cli import _apply_aliases, main
from dropwatch.errors import ExitCode
from dropwatch.models import DECISION_BLOCKED, DECISION_OWNED
from dropwatch.state import MappingTarget, Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A configured-enough environment with an empty state file."""
    for key in ("DROPWATCH_URL", "DROPWATCH_USERNAME", "DROPWATCH_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DROPWATCH_CACHE_PATH", str(tmp_path / "state.sqlite3"))
    return tmp_path / "state.sqlite3"


def open_store(path):
    return Store(path)


class TestLegacySpellings:
    """The old names still work; they are just no longer advertised."""

    @pytest.mark.parametrize(
        "argv,expected",
        [
            (["artists"], ["status"]),
            (["artists", "--mappings"], ["status", "--decided"]),
            (["resolve"], ["fix"]),
            (["resolve", "Ghost"], ["fix", "Ghost"]),
            (["review"], ["fix"]),
            # `review <id>` named a release positionally; `fix` reserves the
            # positional for an artist, so it has to become --album.
            (["review", "558123"], ["fix", "--album", "558123"]),
            (["review", "558123", "--own"], ["fix", "--album", "558123", "--own"]),
            (["review", "--clear"], ["fix", "--clear"]),
        ],
    )
    def test_rewrites(self, argv, expected):
        assert _apply_aliases(argv) == expected

    @pytest.mark.parametrize("argv", [["scan"], ["fix"], ["status"], [], ["--since", "2024"]])
    def test_leaves_everything_else_alone(self, argv):
        assert _apply_aliases(argv) == argv

    def test_a_legacy_name_reaches_its_replacement(self, env, capsys):
        # This used to guard against `artists` being rewritten to
        # ["scan", "artists"]. Nothing implies `scan` any more, so what is
        # worth checking is that the old name still lands on the new command.
        assert main(["artists"]) == ExitCode.OK
        assert "No scan recorded yet" in capsys.readouterr().out


class TestStatus:
    def test_a_fresh_install_says_to_scan(self, env, capsys):
        assert main(["status"]) == ExitCode.OK
        assert "No scan recorded yet" in capsys.readouterr().out

    def test_pending_work_is_counted(self, env, capsys):
        with open_store(env) as store:
            store.save_unresolved([{"name": "Ghost", "reason": "six matches", "candidates": []}])
            store.save_review([{"id": "1", "artist": "A", "title": "T", "type": "", "date": "", "reason": "r"}])
        assert main(["status"]) == ExitCode.OK
        out = capsys.readouterr().out
        assert "1 artist(s) could not be matched" in out
        assert "1 release(s) need a decision" in out
        assert "fix" in out

    def test_saved_work_is_summarised(self, env, capsys):
        with open_store(env) as store:
            store.set_mapping("Ghost", [MappingTarget("42", "Ghost")])
            store.block_artist("Karaoke Hits Vol 3")
            store.set_release_decision("1", DECISION_OWNED)
            store.set_release_decision("2", DECISION_BLOCKED)
        assert main(["status"]) == ExitCode.OK
        out = capsys.readouterr().out
        assert "Nothing pending" in out
        assert "1 artist mapping(s)" in out
        assert "1 blocked artist(s)" in out
        assert "1 blocked, 1 owned" in out

    def test_decided_lists_each_kind_separately(self, env, capsys):
        with open_store(env) as store:
            store.set_mapping("Ghost", [MappingTarget("42", "Ghost")])
            store.block_artist("Karaoke Hits Vol 3")
        assert main(["status", "--decided"]) == ExitCode.OK
        out = capsys.readouterr().out
        assert "Artist mappings (1)" in out
        assert "Blocked artists (1)" in out
        # A blocked artist must not be listed as a mapping: they undo differently.
        assert "Karaoke Hits Vol 3" in out.split("Blocked artists")[1]

    def test_decided_with_nothing_stored(self, env, capsys):
        assert main(["status", "--decided"]) == ExitCode.OK
        assert "Nothing decided yet" in capsys.readouterr().out


class TestFix:
    def test_nothing_pending(self, env, capsys):
        assert main(["fix"]) == ExitCode.OK
        assert "Nothing to fix" in capsys.readouterr().out

    def test_album_decisions_are_recorded(self, env):
        assert main(["fix", "--album", "558123", "--own"]) == ExitCode.OK
        with open_store(env) as store:
            assert store.release_decisions() == {"558123": DECISION_OWNED}

    def test_album_decisions_can_be_cleared(self, env):
        main(["fix", "--album", "558123", "--own"])
        assert main(["fix", "--album", "558123", "--clear"]) == ExitCode.OK
        with open_store(env) as store:
            assert store.release_decisions() == {}

    def test_a_decision_without_an_album_is_refused(self, env, capsys):
        assert main(["fix", "--own"]) == ExitCode.USAGE
        assert "--album" in capsys.readouterr().err

    def test_an_artist_and_an_album_together_are_refused(self, env, capsys):
        assert main(["fix", "Ghost", "--album", "558123"]) == ExitCode.USAGE
        assert "not both" in capsys.readouterr().err

    def test_artist_and_album_flags_are_mutually_exclusive(self, env):
        with pytest.raises(SystemExit):
            main(["fix", "--artist", "Ghost", "--album", "558123"])

    def test_the_legacy_review_spelling_still_decides(self, env):
        assert main(["review", "558123", "--own"]) == ExitCode.OK
        with open_store(env) as store:
            assert store.release_decisions() == {"558123": DECISION_OWNED}
