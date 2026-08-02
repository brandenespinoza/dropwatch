"""Scanning only the artists you have starred in Navidrome."""

from __future__ import annotations

import pytest

from conftest import subsonic
from dropwatch.config import SETTINGS_BY_KEY, is_true
from dropwatch.errors import ConfigError
from dropwatch.navidrome import NavidromeClient
from dropwatch.scan import ScanOptions, Scanner


def starred(*artists) -> dict:
    return subsonic(
        {"starred2": {"artist": [{"id": i, "name": n} for i, n in artists]}}
    )


@pytest.fixture
def scanner(config, fake_http, store):
    """A Scanner whose Navidrome answers are canned."""
    client = NavidromeClient(config, fake_http)
    return Scanner(client, provider=None, store=store, config=config)


def library(fake_http, *artists):
    fake_http.add(
        "getArtists.view",
        subsonic(
            {
                "artists": {
                    "index": [
                        {"artist": [{"id": i, "name": n, "albumCount": 1} for i, n in artists]}
                    ]
                }
            }
        ),
    )
    fake_http.add("getAlbumList2.view", subsonic({"albumList2": {"album": []}}))


class TestFavoritesFilter:
    def test_only_starred_artists_are_scanned(self, scanner, fake_http):
        library(fake_http, ("1", "Kept"), ("2", "Dropped"), ("3", "Also Kept"))
        fake_http.add("getStarred2.view", starred(("1", "Kept"), ("3", "Also Kept")))
        names = [a.name for a in scanner.read_library(ScanOptions(favorites=True))]
        assert names == ["Also Kept", "Kept"]

    def test_off_by_default(self, scanner, fake_http):
        library(fake_http, ("1", "Kept"), ("2", "Dropped"))
        fake_http.add("getStarred2.view", starred(("1", "Kept")))
        assert len(scanner.read_library(ScanOptions())) == 2

    def test_matched_by_name_when_ids_differ(self, scanner, fake_http):
        # Navidrome can report a different id for the same artist across
        # endpoints; the name is the fallback so a favourite is not lost.
        library(fake_http, ("album-artist-1", "Ghost"))
        fake_http.add("getStarred2.view", starred(("starred-9", "ghost")))
        names = [a.name for a in scanner.read_library(ScanOptions(favorites=True))]
        assert names == ["Ghost"]

    def test_a_starred_non_album_artist_is_skipped(self, scanner, fake_http):
        """Starring a single stars an artist with no albums to scan."""
        library(fake_http, ("1", "Has Albums"))
        fake_http.add(
            "getStarred2.view", starred(("1", "Has Albums"), ("99", "Singles Only"))
        )
        names = [a.name for a in scanner.read_library(ScanOptions(favorites=True))]
        assert names == ["Has Albums"]

    def test_no_starred_artists_is_an_explained_error(self, scanner, fake_http):
        library(fake_http, ("1", "Anyone"))
        fake_http.add("getStarred2.view", subsonic({"starred2": {}}))
        with pytest.raises(ConfigError, match="No starred artists"):
            scanner.read_library(ScanOptions(favorites=True))

    def test_combines_with_an_artist_filter(self, scanner, fake_http):
        library(fake_http, ("1", "A"), ("2", "B"))
        fake_http.add("getStarred2.view", starred(("1", "A"), ("2", "B")))
        options = ScanOptions(favorites=True, artist_filters=["B"])
        assert [a.name for a in scanner.read_library(options)] == ["B"]


class TestFavoritesSetting:
    def test_it_is_a_normal_setting_with_an_env_var(self):
        setting = SETTINGS_BY_KEY["favorites"]
        assert setting.env_var == "DROPWATCH_FAVORITES"
        assert setting.effective_default == "false"

    @pytest.mark.parametrize(
        "given,stored",
        [("yes", "true"), ("TRUE", "true"), ("on", "true"), ("1", "true"),
         ("no", "false"), ("False", "false"), ("off", "false"), ("0", "false")],
    )
    def test_spellings_are_canonicalised(self, given, stored):
        assert SETTINGS_BY_KEY["favorites"].validate(given) == stored

    def test_nonsense_is_refused(self):
        with pytest.raises(ConfigError, match="true or false"):
            SETTINGS_BY_KEY["favorites"].validate("maybe")

    def test_is_true_reads_what_was_stored(self):
        assert is_true("true") is True
        assert is_true("false") is False
        assert is_true(None) is False


class TestFlagBeatsSetting:
    """`--favorites` and `--all-artists` override the saved value either way."""

    def _options(self, argv, saved):
        from dropwatch.cli import build_parser

        args = build_parser().parse_args(argv)
        return saved if args.favorites is None else args.favorites

    def test_flag_absent_uses_the_setting(self):
        assert self._options(["scan"], saved=True) is True
        assert self._options(["scan"], saved=False) is False

    def test_favorites_flag_wins(self):
        assert self._options(["scan", "--favorites"], saved=False) is True

    def test_all_artists_flag_wins(self):
        assert self._options(["scan", "--all-artists"], saved=True) is False

    def test_the_two_flags_are_mutually_exclusive(self):
        from dropwatch.cli import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["scan", "--favorites", "--all-artists"])
