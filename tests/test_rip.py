"""Handing releases to an external downloader.

Nothing here runs a real downloader: `subprocess.run` is replaced throughout,
and the assertions are about the argv that would have been executed.
"""

from __future__ import annotations

import sys

import pytest

from dropwatch.config import (
    DEFAULT_RIP_COMMAND,
    SETTINGS_BY_KEY,
    URL_PLACEHOLDER,
    Config,
)
from dropwatch.errors import ConfigError, ExitCode
from dropwatch.models import Ownership
from dropwatch.rip_ui import NOT_RUN, build_command, order_queue, run_rip
from dropwatch.secrets import Secret

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

        with Store(tmp_path / "s.sqlite3", max_age_hours=0.0000001) as s:
            s.save_missing([{"id": "1", "artist": "A", "title": "T"}])
            assert s.get("__missing__") is None  # expired as a cache read
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

    def test_garbage_reprompts(self, queued, rip_config, runner, monkeypatch, capsys):
        drive(monkeypatch, ["yes please", "r", "q"])
        run_rip(queued, rip_config)
        assert "Enter r, s, a or q" in capsys.readouterr().out
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
