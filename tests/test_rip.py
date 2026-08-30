"""Handing releases to an external downloader.

Nothing here runs a real downloader: `subprocess.run` is replaced throughout,
and the assertions are about the argv that would have been executed.
"""

from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from dropwatch.config import (
    DEFAULT_RIP_COMMAND,
    SETTINGS_BY_KEY,
    URL_PLACEHOLDER,
    Config,
)
from dropwatch.errors import ConfigError, ExitCode
from dropwatch.models import (
    DECISION_BLOCKED,
    DECISION_MISSING,
    DECISION_OWNED,
    Ownership,
    ReleaseDate,
)
from dropwatch.rip_ui import (
    NOT_RUN,
    build_command,
    in_scope,
    order_queue,
    pending,
    run_rip,
    scope_note,
)
from dropwatch.secrets import Secret
from dropwatch.state import ScanScope

ALBUM_URL = "https://www.deezer.com/album/302127"


@pytest.fixture
def queued(store):
    store.save_missing(
        [
            {
                "id": "302127",
                "artist": "Fleetwood Mac",
                "title": "Rumours",
                "type": "Album",
                "date": "2024-06-02",
                "ownership": Ownership.MISSING.value,
                "url": ALBUM_URL,
            },
            {
                "id": "825535241",
                "artist": "Another Artist",
                "title": "Another Release",
                "type": "EP",
                "date": "2024-07-18",
                "ownership": Ownership.PROBABLY_MISSING.value,
                "url": "https://www.deezer.com/album/825535241",
            },
        ]
    )
    return store


@pytest.fixture
def rip_config(tmp_path) -> Config:
    return Config(
        navidrome_url="http://example:4533",
        navidrome_username="tester",
        navidrome_password=Secret("not-a-real-password"),
        cache_path=tmp_path / "state.sqlite3",
    )


#: A favourites scan that resolved to these two artists. The names are what
#: `rip` replays now: the stored entries carry no favourite-ness of their own.
FAVORITES_SCAN = ScanScope(
    favorites=True, favorite_artists=("Alpha", "Starred Artist")
)


@pytest.fixture
def mixed_queue(store):
    """What a full scan followed by a favourites scan leaves behind.

    The favourites scan only refreshes the artists it covered, so the earlier
    scan's non-favourite releases stay in the queue. The album is the one that
    sorts first, so an unfiltered walk opens on it.
    """
    store.save_missing(
        [
            {
                "id": "302127",
                "artist": "Not Starred",
                "title": "Older Album",
                "type": "Album",
                "date": "2024-06-02",
                "ownership": Ownership.MISSING.value,
                "url": ALBUM_URL,
            },
            {
                "id": "825535241",
                "artist": "Starred Artist",
                "title": "Starred EP",
                "type": "EP",
                "date": "2024-07-18",
                "ownership": Ownership.MISSING.value,
                "url": "https://www.deezer.com/album/825535241",
            },
        ]
    )
    return store


class FakeRun:
    """Stands in for subprocess.run, recording argv and returning a status."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, command, *args, **kwargs):
        self.calls.append(list(command))
        return type("Completed", (), {"returncode": self.returncode})()


@pytest.fixture
def runner(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr("dropwatch.rip_ui.subprocess.run", fake)
    # The default command is streamrip, which need not be installed to test.
    monkeypatch.setattr("dropwatch.rip_ui.shutil.which", lambda name: f"/usr/bin/{name}")
    return fake


def drive(monkeypatch, answers):
    queue = list(answers)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": queue.pop(0) if queue else "q")


class TestBuildCommand:
    def test_substitutes_the_url(self):
        assert build_command(DEFAULT_RIP_COMMAND, ALBUM_URL) == ["rip", "url", ALBUM_URL]

    def test_url_is_always_one_argument(self):
        """Splitting before substituting is what prevents argv injection.

        A URL is not user input in the normal case, but it arrives from a
        third-party API, and 'and then it became three arguments' is not an
        acceptable failure mode for something that gets executed.
        """
        hostile = "https://example.com/a b --delete-everything"
        assert build_command(DEFAULT_RIP_COMMAND, hostile) == ["rip", "url", hostile]

    def test_placeholder_anywhere_in_a_token(self):
        command = build_command(f"dl --out=/music/{URL_PLACEHOLDER} --quiet", ALBUM_URL)
        assert command == ["dl", f"--out=/music/{ALBUM_URL}", "--quiet"]

    def test_quoted_arguments_survive(self):
        command = build_command(f'my tool --dir "/my music" {URL_PLACEHOLDER}', ALBUM_URL)
        assert command == ["my", "tool", "--dir", "/my music", ALBUM_URL]

    def test_template_without_placeholder_is_rejected(self):
        with pytest.raises(ConfigError, match=r"\{url\}"):
            build_command("rip url", ALBUM_URL)

    def test_unbalanced_quote_is_rejected(self):
        with pytest.raises(ConfigError, match="valid command line"):
            build_command(f'rip "url {URL_PLACEHOLDER}', ALBUM_URL)

    def test_empty_is_rejected(self):
        with pytest.raises(ConfigError, match="empty"):
            build_command("   ", ALBUM_URL)


class TestSetting:
    """`config set rip-command` must reject what `rip` could not run."""

    def setting(self):
        return SETTINGS_BY_KEY["rip-command"]

    def test_default_is_streamrip(self):
        assert self.setting().default == DEFAULT_RIP_COMMAND
        assert build_command(self.setting().default, ALBUM_URL)[0] == "rip"

    def test_accepts_a_valid_template(self):
        assert self.setting().validate(f"other-tool {URL_PLACEHOLDER}")

    def test_rejects_a_missing_placeholder(self):
        with pytest.raises(ConfigError, match=r"\{url\}"):
            self.setting().validate("rip url")

    def test_rejects_an_unbalanced_quote(self):
        with pytest.raises(ConfigError, match="valid command line"):
            self.setting().validate(f'rip "url {URL_PLACEHOLDER}')

    def test_rejects_empty(self):
        with pytest.raises(ConfigError, match="empty"):
            self.setting().validate("  ")


class TestQueue:
    def test_round_trip(self, store):
        store.save_missing([{"id": "1", "artist": "A", "title": "T"}])
        assert store.load_missing()[0]["title"] == "T"

    def test_absent_queue_is_empty(self, store):
        assert store.load_missing() == []

    def test_does_not_expire(self, tmp_path):
        """The queue is the last scan's report, not a cache entry.

        Ripping the day after a scan must still work, so expiry is bypassed
        the same way the review and unresolved queues bypass it.
        """
        from dropwatch.state import Store

        with Store(tmp_path / "s.sqlite3", max_age_hours=1.0) as s:
            s.save_missing([{"id": "1", "artist": "A", "title": "T"}])
            # Backdated rather than raced against a tiny max_age: the claim is
            # that load_missing ignores age, which needs the entry to be
            # certainly stale, not stale if the assertion is slow enough.
            s._conn.execute(
                "UPDATE cache SET fetched_at = fetched_at - 86400 WHERE key = ?",
                ("__missing__",),
            )
            s._conn.commit()
            assert s.get("__missing__") is None  # stale as a cache read
            assert len(s.load_missing()) == 1  # but still readable as a report


class TestOrder:
    """Albums, then EPs, then singles; newest first inside each group."""

    def entry(self, id_, type_, date, artist="A", title="T"):
        return {"id": str(id_), "artist": artist, "title": title,
                "type": type_, "date": date, "url": f"u{id_}"}

    def test_types_group_in_report_order(self):
        ordered = order_queue([
            self.entry(1, "Single", "2024-01-01"),
            self.entry(2, "Album", "2024-01-01"),
            self.entry(3, "Unknown", "2024-01-01"),
            self.entry(4, "EP", "2024-01-01"),
        ])
        assert [e["type"] for e in ordered] == ["Album", "EP", "Single", "Unknown"]

    def test_newest_first_within_a_group(self):
        ordered = order_queue([
            self.entry(1, "Album", "2020-05-01"),
            self.entry(2, "Album", "2026-01-15"),
            self.entry(3, "Album", "2023-11-30"),
        ])
        assert [e["date"] for e in ordered] == ["2026-01-15", "2023-11-30", "2020-05-01"]

    def test_date_never_outranks_type(self):
        """A brand-new single still comes after every album."""
        ordered = order_queue([
            self.entry(1, "Single", "2026-08-01"),
            self.entry(2, "Album", "1975-02-04"),
        ])
        assert [e["type"] for e in ordered] == ["Album", "Single"]

    def test_imprecise_dates_fall_below_full_ones(self):
        ordered = order_queue([
            self.entry(1, "Album", "2024"),
            self.entry(2, "Album", "2024-06-15"),
            self.entry(3, "Album", "2024-06"),
        ])
        assert [e["date"] for e in ordered] == ["2024-06-15", "2024-06", "2024"]

    def test_unknown_dates_sink_to_the_bottom_of_their_group(self):
        ordered = order_queue([
            self.entry(1, "Album", "unknown"),
            self.entry(2, "Album", "1999-01-01"),
            self.entry(3, "EP", "2026-01-01"),
        ])
        assert [(e["type"], e["date"]) for e in ordered] == [
            ("Album", "1999-01-01"), ("Album", "unknown"), ("EP", "2026-01-01"),
        ]

    def test_ties_break_by_artist_then_title(self):
        ordered = order_queue([
            self.entry(1, "Album", "2024-01-01", artist="Zebra", title="A"),
            self.entry(2, "Album", "2024-01-01", artist="Alpha", title="Z"),
            self.entry(3, "Album", "2024-01-01", artist="Alpha", title="B"),
        ])
        assert [(e["artist"], e["title"]) for e in ordered] == [
            ("Alpha", "B"), ("Alpha", "Z"), ("Zebra", "A"),
        ]

    def test_an_unrecognised_type_sorts_last_rather_than_crashing(self):
        ordered = order_queue([
            self.entry(1, "Mixtape", "2026-01-01"),
            self.entry(2, "Album", "1999-01-01"),
        ])
        assert [e["type"] for e in ordered] == ["Album", "Mixtape"]

    def test_missing_fields_do_not_crash(self):
        assert len(order_queue([{"id": "1"}, {"id": "2", "type": "Album"}])) == 2

    def test_the_walk_uses_it(self, store, rip_config, runner, monkeypatch):
        """The stored order is scan order; the walk must not inherit it."""
        store.save_missing([
            {"id": "1", "artist": "Aardvark", "title": "Old Single",
             "type": "Single", "date": "2020-01-01", "url": "u1"},
            {"id": "2", "artist": "Zebra", "title": "New Album",
             "type": "Album", "date": "2026-07-01", "url": "u2"},
        ])
        drive(monkeypatch, ["a"])
        run_rip(store, rip_config)
        assert [c[-1] for c in runner.calls] == ["u2", "u1"]


class TestWalk:
    def test_rip_one_then_quit(self, queued, rip_config, runner, monkeypatch):
        drive(monkeypatch, ["r", "q"])
        assert run_rip(queued, rip_config) == ExitCode.OK
        assert runner.calls == [["rip", "url", ALBUM_URL]]

    def test_enter_skips(self, queued, rip_config, runner, monkeypatch):
        drive(monkeypatch, ["", "q"])
        run_rip(queued, rip_config)
        assert runner.calls == []

    def test_all_remaining_stops_prompting(self, queued, rip_config, runner, monkeypatch):
        drive(monkeypatch, ["a"])  # one answer, two releases
        run_rip(queued, rip_config)
        assert [c[-1] for c in runner.calls] == [
            ALBUM_URL,
            "https://www.deezer.com/album/825535241",
        ]

    def test_quit_stops_the_walk(self, queued, rip_config, runner, monkeypatch):
        drive(monkeypatch, ["q"])
        run_rip(queued, rip_config)
        assert runner.calls == []

    def test_notes_are_shown_with_the_release(self, store, rip_config, runner, monkeypatch, capsys):
        # The walk is where a release is accepted or rejected, so "remix" has
        # to be on screen there too, not only in the scan table.
        store.save_missing(
            [
                {
                    "id": "863456162",
                    "artist": "The Elovaters",
                    "title": "Castaway",
                    "type": "Single",
                    "date": "2025-12-12",
                    "ownership": Ownership.PROBABLY_MISSING.value,
                    "url": "https://www.deezer.com/album/863456162",
                    "notes": "remix",
                }
            ]
        )
        drive(monkeypatch, ["q"])
        run_rip(store, rip_config)
        assert "Single, 2025-12-12, remix" in capsys.readouterr().out

    def test_an_entry_without_notes_is_unchanged(self, queued, rip_config, runner, monkeypatch, capsys):
        drive(monkeypatch, ["q"])
        run_rip(queued, rip_config)
        assert "Album, 2024-06-02" in capsys.readouterr().out

    def test_garbage_reprompts(self, queued, rip_config, runner, monkeypatch, capsys):
        drive(monkeypatch, ["yes please", "r", "q"])
        run_rip(queued, rip_config)
        assert "Enter r, s, o, b, a or q" in capsys.readouterr().out
        assert len(runner.calls) == 1

    def test_eof_quits(self, queued, rip_config, runner, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

        def raise_eof(_=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)
        assert run_rip(queued, rip_config) == ExitCode.OK
        assert runner.calls == []

    def test_custom_command_is_used(self, queued, rip_config, runner, monkeypatch):
        rip_config.rip_command = f"my-downloader --url {URL_PLACEHOLDER} --flac"
        drive(monkeypatch, ["r", "q"])
        run_rip(queued, rip_config)
        assert runner.calls == [["my-downloader", "--url", ALBUM_URL, "--flac"]]

    def test_id_is_used_when_a_url_is_absent(self, store, rip_config, runner, monkeypatch):
        store.save_missing([{"id": "999", "artist": "A", "title": "T"}])
        drive(monkeypatch, ["r", "q"])
        run_rip(store, rip_config)
        assert runner.calls == [["rip", "url", "https://www.deezer.com/album/999"]]


class TestOwn:
    """`o` during the walk, for a release the scan was wrong about.

    Same round-trip saved as `b`: the alternative was abandoning the walk to
    run `fix --album <id> --own`. Kept distinct from `b` because only this one
    claims the release is in the library.
    """

    EP_URL = "https://www.deezer.com/album/825535241"

    def test_o_records_ownership(self, queued, rip_config, runner, monkeypatch):
        drive(monkeypatch, ["o", "q"])
        assert run_rip(queued, rip_config) == ExitCode.OK
        assert queued.release_decisions() == {"302127": DECISION_OWNED}
        assert runner.calls == []  # saying you have it does not download it

    def test_it_is_not_the_same_decision_as_block(
        self, queued, rip_config, runner, monkeypatch
    ):
        """Both suppress the release; only one is a claim about the library."""
        drive(monkeypatch, ["o", "b"])
        run_rip(queued, rip_config)
        assert queued.release_decisions() == {
            "302127": DECISION_OWNED,
            "825535241": DECISION_BLOCKED,
        }

    def test_the_walk_continues_afterwards(
        self, queued, rip_config, runner, monkeypatch
    ):
        drive(monkeypatch, ["o", "r"])
        run_rip(queued, rip_config)
        assert [c[-1] for c in runner.calls] == [self.EP_URL]

    def test_an_owned_release_is_not_offered_again(
        self, queued, rip_config, runner, monkeypatch, capsys
    ):
        drive(monkeypatch, ["o", "q"])
        run_rip(queued, rip_config)
        capsys.readouterr()

        drive(monkeypatch, ["q"])
        run_rip(queued, rip_config)
        out = capsys.readouterr().out
        assert "1 release(s) from the last scan" in out
        assert "Rumours" not in out

    def test_the_prompt_offers_it(self, queued, rip_config, runner, monkeypatch, capsys):
        drive(monkeypatch, ["q"])
        run_rip(queued, rip_config)
        assert "[o] I own it" in capsys.readouterr().out

    def test_it_is_counted_in_the_summary(
        self, queued, rip_config, runner, monkeypatch, capsys
    ):
        drive(monkeypatch, ["o", "r"])
        run_rip(queued, rip_config)
        assert "1 ripped, 1 marked owned" in capsys.readouterr().out

    def test_it_is_the_decision_clear_reverses(
        self, queued, rip_config, runner, monkeypatch
    ):
        """`fix --album <id> --clear` clears it; so must this one."""
        drive(monkeypatch, ["o", "q"])
        run_rip(queued, rip_config)
        assert queued.clear_release_decision("302127") is True
        assert queued.release_decisions() == {}

    def test_an_entry_without_an_id_cannot_be_marked(
        self, store, rip_config, runner, monkeypatch, capsys
    ):
        store.save_missing([{"artist": "A", "title": "T", "type": "Album"}])
        drive(monkeypatch, ["o", "q"])
        run_rip(store, rip_config)
        assert store.release_decisions() == {}
        assert "no Deezer id" in capsys.readouterr().err


class TestBlock:
    """`b` during the walk, for a release you never want offered again.

    The walk is where you are already looking at the thing you don't want, so
    the alternative was leaving it, copying its URL out of the report and
    running `block --album` afterwards.
    """

    EP_URL = "https://www.deezer.com/album/825535241"

    def test_b_records_a_block(self, queued, rip_config, runner, monkeypatch):
        drive(monkeypatch, ["b", "q"])
        assert run_rip(queued, rip_config) == ExitCode.OK
        assert queued.release_decisions() == {"302127": DECISION_BLOCKED}
        assert runner.calls == []  # blocking is not downloading

    def test_it_is_the_decision_unblock_reverses(
        self, queued, rip_config, runner, monkeypatch
    ):
        """`unblock --album` clears the release decision; so must this one."""
        drive(monkeypatch, ["b", "q"])
        run_rip(queued, rip_config)
        assert queued.clear_release_decision("302127") is True
        assert queued.release_decisions() == {}

    def test_the_walk_continues_afterwards(
        self, queued, rip_config, runner, monkeypatch
    ):
        drive(monkeypatch, ["b", "r"])
        run_rip(queued, rip_config)
        assert [c[-1] for c in runner.calls] == [self.EP_URL]

    def test_a_blocked_release_is_not_offered_again(
        self, queued, rip_config, runner, monkeypatch, capsys
    ):
        drive(monkeypatch, ["b", "q"])
        run_rip(queued, rip_config)
        capsys.readouterr()

        drive(monkeypatch, ["q"])
        run_rip(queued, rip_config)
        out = capsys.readouterr().out
        assert "1 release(s) from the last scan" in out
        assert "Rumours" not in out
        assert "Another Release" in out

    def test_a_release_marked_owned_is_not_offered(
        self, queued, rip_config, runner, monkeypatch, capsys
    ):
        """`fix --album <id> --own` said to stop reporting it; this reports it."""
        queued.set_release_decision("302127", DECISION_OWNED)
        drive(monkeypatch, ["q"])
        run_rip(queued, rip_config)
        assert "Rumours" not in capsys.readouterr().out

    def test_a_release_marked_missing_is_still_offered(
        self, queued, rip_config, runner, monkeypatch, capsys
    ):
        """That decision means "always report it", which is the opposite."""
        queued.set_release_decision("302127", DECISION_MISSING)
        drive(monkeypatch, ["q"])
        run_rip(queued, rip_config)
        assert "Rumours" in capsys.readouterr().out

    def test_a_fully_decided_queue_does_not_advise_a_scan(
        self, queued, rip_config, runner, monkeypatch, capsys
    ):
        for release_id in ("302127", "825535241"):
            queued.set_release_decision(release_id, DECISION_BLOCKED)
        assert run_rip(queued, rip_config) == ExitCode.OK
        out = capsys.readouterr().out
        assert "every release from the last scan is decided" in out
        assert "Run a scan first" not in out

    def test_the_prompt_offers_it(self, queued, rip_config, runner, monkeypatch, capsys):
        drive(monkeypatch, ["q"])
        run_rip(queued, rip_config)
        assert "[b]lock" in capsys.readouterr().out

    def test_it_is_counted_in_the_summary(
        self, queued, rip_config, runner, monkeypatch, capsys
    ):
        drive(monkeypatch, ["b", "r"])
        run_rip(queued, rip_config)
        assert "1 ripped, 1 blocked" in capsys.readouterr().out

    def test_an_entry_without_an_id_cannot_be_blocked(
        self, store, rip_config, runner, monkeypatch, capsys
    ):
        """Rather than writing a decision against an empty id."""
        store.save_missing([{"artist": "A", "title": "T", "type": "Album"}])
        drive(monkeypatch, ["b", "q"])
        run_rip(store, rip_config)
        assert store.release_decisions() == {}
        assert "no Deezer id" in capsys.readouterr().err


class TestPending:
    """Filtering happens at read time, like the ordering, and for the same
    reason: the stored queue is written by scans that know nothing about
    decisions made since."""

    ENTRIES = [{"id": "1"}, {"id": "2"}, {"id": "3"}]

    def test_no_decisions_keeps_everything(self):
        assert pending(self.ENTRIES, {}) == self.ENTRIES

    def test_blocked_and_owned_are_dropped(self):
        kept = pending(
            self.ENTRIES, {"1": DECISION_BLOCKED, "3": DECISION_OWNED}
        )
        assert [e["id"] for e in kept] == ["2"]

    def test_missing_is_kept(self):
        kept = pending(self.ENTRIES, {"1": DECISION_MISSING})
        assert [e["id"] for e in kept] == ["1", "2", "3"]

    def test_an_entry_without_an_id_survives(self):
        """It cannot have been decided, so it cannot have been suppressed."""
        assert pending([{"title": "T"}], {"": DECISION_BLOCKED}) == [{"title": "T"}]


class TestScopeMirrorsTheLastScan:
    """One rule: the walk offers what the last scan reported.

    The stored queue is the union of every scan, so an earlier, wider scan's
    releases stay in it. They must not be walked while a narrower scan is the
    most recent thing the user saw.
    """

    ENTRIES = [
        {"id": "1", "artist": "Alpha", "type": "Album", "date": "2025-04-18"},
        {"id": "2", "artist": "Beta", "type": "Single", "date": "2023-01-01"},
        {"id": "3", "artist": "Gamma", "type": "EP", "date": "2024"},
    ]

    def test_an_unnarrowed_scope_keeps_everything(self):
        assert in_scope(self.ENTRIES, ScanScope()) == self.ENTRIES

    def test_favorites_keeps_only_what_a_favourites_scan_found(self):
        assert [e["id"] for e in in_scope(self.ENTRIES, FAVORITES_SCAN)] == ["1"]

    def test_an_artist_who_stopped_being_a_favourite_is_dropped(self):
        """The bug the name list exists to fix.

        A stamped flag described the artist at write time and stayed true after
        they were unstarred. No later favourites scan covers an ex-favourite,
        so nothing purged the entry and the walk kept offering a release the
        scan no longer reported.
        """
        stale = [{"id": "9", "artist": "Alpha"}, {"id": "10", "artist": "Was Starred"}]
        assert [e["id"] for e in in_scope(stale, FAVORITES_SCAN)] == ["9"]

    def test_a_record_with_no_names_narrows_nothing(self):
        """Written before the names were kept. Offering a few extra releases
        beats hiding the queue, and the next scan fills the list in."""
        assert in_scope(self.ENTRIES, ScanScope(favorites=True)) == self.ENTRIES

    def test_an_entry_with_no_artist_matches_no_favourite(self):
        assert in_scope([{"id": "3"}], FAVORITES_SCAN) == []

    def test_types_keep_only_the_types_the_scan_reported(self):
        kept = in_scope(self.ENTRIES, ScanScope(types=frozenset({"Album", "EP"})))
        assert [e["id"] for e in kept] == ["1", "3"]

    def test_artists_keep_only_the_artists_the_scan_covered(self):
        kept = in_scope(self.ENTRIES, ScanScope(artists=("alpha",)))
        assert [e["id"] for e in kept] == ["1"]

    def test_artist_matching_folds_the_name(self):
        """The same fold the scan filtered the library with."""
        kept = in_scope([{"artist": "Bj\u00f6rk"}], ScanScope(artists=("bjork",)))
        assert len(kept) == 1

    def test_a_cutoff_drops_older_releases(self):
        kept = in_scope(self.ENTRIES, ScanScope(since=ReleaseDate.parse("2024")))
        assert [e["id"] for e in kept] == ["1", "3"]

    def test_imprecise_and_unknown_dates_survive_a_cutoff(self):
        """Matching the scan: excluded on a technicality is worse than shown."""
        entries = [{"id": "y", "date": "2024"}, {"id": "u", "date": "unknown"}]
        kept = in_scope(entries, ScanScope(since=ReleaseDate.parse("2024-06-01")))
        assert [e["id"] for e in kept] == ["y", "u"]

    def test_the_axes_stack(self):
        kept = in_scope(
            self.ENTRIES,
            ScanScope(favorites=True, types=frozenset({"Album"}),
                      since=ReleaseDate.parse("2024"), artists=("Alpha",)),
        )
        assert [e["id"] for e in kept] == ["1"]


class TestScopeIsReadFromTheStore:
    """`rip` replays the scan's record, so a setting alone cannot re-scope it."""

    def test_the_walk_offers_only_favourites(
        self, mixed_queue, rip_config, runner, monkeypatch, capsys
    ):
        """The reported bug: it opened on the album from the earlier full scan,
        because albums sort ahead of EPs."""
        mixed_queue.save_scan_scope(FAVORITES_SCAN)
        drive(monkeypatch, ["r"])
        run_rip(mixed_queue, rip_config)
        assert [call[-1] for call in runner.calls] == [
            "https://www.deezer.com/album/825535241"
        ]
        assert "Older Album" not in capsys.readouterr().out

    def test_a_later_wide_scan_offers_everything_again(
        self, mixed_queue, rip_config, runner, monkeypatch, capsys
    ):
        mixed_queue.save_scan_scope(FAVORITES_SCAN)
        mixed_queue.save_scan_scope(ScanScope())
        drive(monkeypatch, ["a"])
        run_rip(mixed_queue, rip_config)
        assert len(runner.calls) == 2
        assert "Older Album" in capsys.readouterr().out

    def test_a_saved_favorites_setting_does_not_narrow_a_wide_scan(
        self, mixed_queue, rip_config, runner, monkeypatch, capsys
    ):
        """The `--all-artists` case. Under the old setting-driven scope this
        hid the whole queue and advised re-scanning favourites — the opposite
        of what the user had just asked for."""
        mixed_queue.save_scan_scope(ScanScope())  # what `scan --all-artists` records
        drive(monkeypatch, ["a"])
        run_rip(mixed_queue, replace(rip_config, favorites_only=True))
        assert len(runner.calls) == 2
        assert "Older Album" in capsys.readouterr().out

    def test_the_walk_offers_only_the_scanned_artist(
        self, mixed_queue, rip_config, runner, monkeypatch, capsys
    ):
        """`scan --artist X` leaves everyone else's releases in the queue, so
        without this the walk opens on somebody the user did not ask about."""
        mixed_queue.save_scan_scope(ScanScope(artists=("Starred Artist",)))
        drive(monkeypatch, ["a"])
        run_rip(mixed_queue, rip_config)
        assert [call[-1] for call in runner.calls] == [
            "https://www.deezer.com/album/825535241"
        ]
        assert "Older Album" not in capsys.readouterr().out

    def test_no_recorded_scope_narrows_nothing(
        self, queued, rip_config, runner, monkeypatch
    ):
        """A queue from before scopes were recorded still walks in full."""
        drive(monkeypatch, ["a"])
        run_rip(queued, rip_config)
        assert len(runner.calls) == 2

    def test_an_unreadable_record_narrows_nothing(self, queued, rip_config, runner,
                                                  monkeypatch):
        """Walking a narrower list than asked for is worse than ignoring it."""
        queued.set_meta("last_scan_scope", "{not json")
        drive(monkeypatch, ["a"])
        run_rip(queued, rip_config)
        assert len(runner.calls) == 2


class TestScopeNote:
    """A walk shorter than the last scan's results explains itself."""

    def test_an_unnarrowed_scope_says_nothing(self):
        assert scope_note(ScanScope()) == ""

    @pytest.mark.parametrize(
        "scope,expected",
        [
            (ScanScope(favorites=True), " (favourites only)"),
            (ScanScope(since=ReleaseDate.parse("2024")), " (since 2024)"),
            (ScanScope(types=frozenset({"Album"})), " (albums only)"),
            (ScanScope(types=frozenset({"EP"})), " (EPs only)"),
            (ScanScope(types=frozenset({"Album", "EP"})), " (albums and EPs only)"),
            (ScanScope(artists=("Radiohead",)), " (Radiohead)"),
            (ScanScope(artists=("A", "B")), " (A and B)"),
            (ScanScope(artists=("A", "B", "C", "D")), " (A, B and 2 others)"),
        ],
    )
    def test_each_axis_is_named(self, scope, expected):
        assert scope_note(scope) == expected

    def test_types_are_named_in_the_report_s_order(self):
        """Not the set's, which is arbitrary."""
        assert scope_note(ScanScope(types=frozenset({"Single", "Album"}))) == (
            " (albums and singles only)"
        )

    def test_every_axis_together(self):
        note = scope_note(
            ScanScope(favorites=True, since=ReleaseDate.parse("2024"),
                      types=frozenset({"Album"}), artists=("Radiohead",))
        )
        assert note == " (Radiohead, favourites only, albums only, since 2024)"

    def test_the_count_line_carries_it(
        self, mixed_queue, rip_config, runner, monkeypatch, capsys
    ):
        mixed_queue.save_scan_scope(FAVORITES_SCAN)
        drive(monkeypatch, ["q"])
        run_rip(mixed_queue, rip_config)
        out = capsys.readouterr().out
        assert "1 release(s) from the last scan (favourites only)." in out


class TestScopeHidesTheWholeQueue:
    """The message names the filter responsible, not just "run a scan"."""

    def test_favorites_names_the_widening_scan(self, queued, rip_config, capsys):
        """Neither artist in `queued` was a favourite when the scan ran."""
        queued.save_scan_scope(FAVORITES_SCAN)
        assert run_rip(queued, rip_config) == ExitCode.OK
        out = capsys.readouterr().out
        assert "2 release(s) in the queue, none of them from a favourites scan" in out
        assert "scan --all-artists" in out
        assert "Run a scan first" not in out

    def test_a_cutoff_says_which_filter_did_it(self, store, rip_config, capsys):
        store.save_missing([{"id": "1", "artist": "A", "title": "T",
                             "type": "Album", "date": "2009-01-01"}])
        store.save_scan_scope(ScanScope(since=ReleaseDate.parse("2024")))
        assert run_rip(store, rip_config) == ExitCode.OK
        out = capsys.readouterr().out
        assert "none released on or after 2024" in out
        assert "Re-scan without the cutoff" in out
        assert "favourites" not in out  # not the filter responsible

    def test_types_say_which_filter_did_it(self, store, rip_config, capsys):
        store.save_missing([{"id": "1", "artist": "A", "title": "T",
                             "type": "Single", "date": "2025-01-01"}])
        store.save_scan_scope(ScanScope(types=frozenset({"Album"})))
        assert run_rip(store, rip_config) == ExitCode.OK
        out = capsys.readouterr().out
        assert "none of them albums" in out
        assert "config set types all" in out

    def test_artists_say_which_filter_did_it(self, store, rip_config, capsys):
        store.save_missing([{"id": "1", "artist": "A", "title": "T",
                             "type": "Album", "date": "2025-01-01"}])
        store.save_scan_scope(ScanScope(artists=("Radiohead",)))
        assert run_rip(store, rip_config) == ExitCode.OK
        out = capsys.readouterr().out
        assert "none of them by Radiohead" in out
        assert "Re-scan without the artist filter" in out

    def test_only_the_responsible_axes_are_blamed(self, store, rip_config, capsys):
        """Favourites passes something; the cutoff is what empties the walk."""
        store.save_missing([{"id": "1", "artist": "A", "title": "T", "type": "Album",
                             "date": "2009-01-01"}])
        store.save_scan_scope(
            ScanScope(favorites=True, favorite_artists=("A",),
                      since=ReleaseDate.parse("2024"))
        )
        assert run_rip(store, rip_config) == ExitCode.OK
        out = capsys.readouterr().out
        assert "none released on or after 2024" in out
        assert "favourites scan" not in out

    def test_an_empty_intersection_falls_back_to_the_whole_scope(
        self, store, rip_config, capsys
    ):
        """Each axis passes something on its own; only together do they empty
        the walk, so no single filter can honestly be named."""
        store.save_missing([
            {"id": "1", "artist": "A", "title": "Old favourite", "type": "Album",
             "date": "2009-01-01"},
            {"id": "2", "artist": "B", "title": "New stranger", "type": "Album",
             "date": "2025-01-01"},
        ])
        store.save_scan_scope(
            ScanScope(favorites=True, favorite_artists=("A",),
                      since=ReleaseDate.parse("2024"))
        )
        assert run_rip(store, rip_config) == ExitCode.OK
        out = capsys.readouterr().out
        assert "none within the last scan's scope" in out
        assert "Re-scan more widely" in out

    def test_a_fully_decided_queue_is_still_told_apart(
        self, queued, rip_config, capsys
    ):
        """Scope hides nothing here — the user has simply answered it all."""
        for entry in queued.load_missing():
            queued.set_release_decision(entry["id"], DECISION_OWNED)
        assert run_rip(queued, rip_config) == ExitCode.OK
        assert "every release from the last scan is decided" in capsys.readouterr().out


class TestFailures:
    def test_empty_queue_says_so(self, store, rip_config, capsys):
        assert run_rip(store, rip_config) == ExitCode.OK
        assert "Nothing to rip" in capsys.readouterr().out

    def test_needs_a_terminal(self, queued, rip_config, monkeypatch, capsys):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert run_rip(queued, rip_config) == ExitCode.CONFIG
        assert "interactive terminal" in capsys.readouterr().err

    def test_missing_binary_is_caught_before_prompting(
        self, queued, rip_config, monkeypatch, capsys
    ):
        """40 prompts followed by 'command not found' is the wrong order."""
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("dropwatch.rip_ui.shutil.which", lambda name: None)
        called = []
        monkeypatch.setattr("builtins.input", lambda _="": called.append(1) or "q")

        assert run_rip(queued, rip_config) == ExitCode.CONFIG
        assert not called
        assert "not installed or not on PATH" in capsys.readouterr().err

    def test_a_failed_rip_does_not_stop_the_walk(
        self, queued, rip_config, monkeypatch, capsys
    ):
        fake = FakeRun(returncode=1)
        monkeypatch.setattr("dropwatch.rip_ui.subprocess.run", fake)
        monkeypatch.setattr("dropwatch.rip_ui.shutil.which", lambda name: "/usr/bin/rip")
        drive(monkeypatch, ["r", "r"])

        assert run_rip(queued, rip_config) == ExitCode.OK
        assert len(fake.calls) == 2
        assert "2 failed" in capsys.readouterr().out

    def test_interrupting_one_rip_returns_to_the_prompt(
        self, queued, rip_config, monkeypatch, capsys
    ):
        """Ctrl-C abandons a download, not the session."""
        calls = []

        def interrupt(command, *args, **kwargs):
            calls.append(list(command))
            raise KeyboardInterrupt

        monkeypatch.setattr("dropwatch.rip_ui.subprocess.run", interrupt)
        monkeypatch.setattr("dropwatch.rip_ui.shutil.which", lambda name: "/usr/bin/rip")
        drive(monkeypatch, ["r", "r"])

        assert run_rip(queued, rip_config) == ExitCode.OK
        assert len(calls) == 2  # it kept going

    def test_unrunnable_command_reports_and_continues(
        self, queued, rip_config, monkeypatch, capsys
    ):
        def not_found(command, *args, **kwargs):
            raise FileNotFoundError

        monkeypatch.setattr("dropwatch.rip_ui.subprocess.run", not_found)
        monkeypatch.setattr("dropwatch.rip_ui.shutil.which", lambda name: "/usr/bin/rip")
        drive(monkeypatch, ["r", "q"])

        assert run_rip(queued, rip_config) == ExitCode.OK
        assert "not installed" in capsys.readouterr().err


class TestLeavesStateAlone:
    """Ripping is not a claim of ownership. The library stays the authority."""

    def test_a_successful_rip_records_no_decision(
        self, queued, rip_config, runner, monkeypatch
    ):
        drive(monkeypatch, ["a"])
        run_rip(queued, rip_config)
        assert runner.calls  # it really did run
        assert queued.release_decisions() == {}

    def test_the_queue_is_not_consumed(self, queued, rip_config, runner, monkeypatch):
        """A scan replaces the queue; ripping does not empty it."""
        drive(monkeypatch, ["a"])
        run_rip(queued, rip_config)
        assert len(queued.load_missing()) == 2

    def test_the_next_scan_is_the_thing_that_clears_it(
        self, queued, rip_config, runner, monkeypatch, capsys
    ):
        drive(monkeypatch, ["r", "q"])
        run_rip(queued, rip_config)
        assert "Navidrome indexes the files" in capsys.readouterr().out
