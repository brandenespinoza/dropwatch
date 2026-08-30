"""End-to-end scans with both APIs mocked.

These exercise the whole pipeline: Navidrome read, artist resolution, Deezer
discography, ownership, dedupe, sorting and output.
"""

from __future__ import annotations

import io

import pytest
from conftest import FakeHttp, subsonic

from dropwatch.deezer import DeezerProvider
from dropwatch.errors import ConnectionTimeoutError
from dropwatch.models import ReleaseDate, ReleaseType
from dropwatch.navidrome import NavidromeClient
from dropwatch.rip_ui import in_scope
from dropwatch.report import build_summary, print_summary, render_table, sort_releases
from dropwatch.scan import ScanOptions, Scanner
from dropwatch.state import ScanScope

KID_A = [
    ("Everything In Its Right Place", 251),
    ("Kid A", 267),
    ("The National Anthem", 351),
    ("How to Disappear Completely", 356),
    ("Treefingers", 222),
    ("Optimistic", 315),
    ("In Limbo", 191),
    ("Idioteque", 289),
]


def navidrome_routes(http: FakeHttp) -> None:
    http.add("ping.view", subsonic({"type": "navidrome", "serverVersion": "0.53.3"}))
    http.add(
        "getArtists.view",
        subsonic(
            {
                "artists": {
                    "index": [
                        {
                            "name": "R",
                            "artist": [{"id": "1", "name": "Radiohead", "albumCount": 1}],
                        }
                    ]
                }
            }
        ),
    )
    http.add(
        "getAlbumList2.view",
        lambda url: subsonic(
            {
                "albumList2": {
                    "album": (
                        []
                        if "offset=500" in url
                        else [
                            {
                                "id": "a1",
                                "name": "Kid A",
                                "artist": "Radiohead",
                                "artistId": "1",
                                "year": 2000,
                                "songCount": len(KID_A),
                                "duration": sum(d for _, d in KID_A),
                            }
                        ]
                    )
                }
            }
        ),
    )
    http.add(
        "getAlbum.view",
        subsonic(
            {
                "album": {
                    "id": "a1",
                    "song": [
                        {"id": f"s{i}", "title": t, "duration": d, "track": i + 1}
                        for i, (t, d) in enumerate(KID_A)
                    ],
                }
            }
        ),
    )


def deezer_routes(http: FakeHttp, releases: list[dict], details: dict | None = None) -> None:
    http.add(
        "/search/artist",
        {"data": [{"id": 399, "name": "Radiohead", "nb_album": 20, "nb_fan": 5_000_000}]},
    )
    http.add("/artist/399/albums", {"data": releases, "total": len(releases)})
    http.add("/artist/399", {"id": 399, "name": "Radiohead", "nb_album": 20, "nb_fan": 5_000_000})

    details = details or {}
    # Detail must agree with the listing unless a test deliberately differs,
    # since the real API returns the same values from both endpoints.
    rows = {str(r["id"]): r for r in releases}

    def album(url):
        album_id = url.split("/album/")[1].split("?")[0].split("/")[0]
        entry = details.get(album_id, {})
        row = rows.get(album_id, {})
        return {
            "id": album_id,
            "title": entry.get("title", "Unknown"),
            "nb_tracks": len(entry.get("tracks", [])),
            "duration": sum(d for _, d in entry.get("tracks", [])),
            "record_type": entry.get("record_type", row.get("record_type", "album")),
            "release_date": entry.get("date", row.get("release_date", "2020-01-01")),
            "upc": entry.get("upc"),
            "contributors": [],
            "tracks": {"data": []},
        }

    def album_tracks(url):
        album_id = url.split("/album/")[1].split("/tracks")[0]
        entry = details.get(album_id, {})
        # `versions` parallels `tracks`, for a release whose marker Deezer puts
        # on the tracks rather than in the title.
        versions = entry.get("versions") or []
        return {
            "data": [
                {
                    "id": f"{album_id}-{i}",
                    "title": t + (f" {versions[i]}" if i < len(versions) else ""),
                    "title_short": t,
                    "title_version": versions[i] if i < len(versions) else "",
                    "duration": d,
                    "isrc": None,
                    "disk_number": 1,
                    "track_position": i + 1,
                }
                for i, (t, d) in enumerate(entry.get("tracks", []))
            ],
            "total": len(entry.get("tracks", [])),
        }

    http.add("/tracks", album_tracks)  # matched before the bare album route
    http.add("/album/", album)


def release_row(id_, title, date, record_type="album"):
    return {
        "id": id_,
        "title": title,
        "record_type": record_type,
        "release_date": date,
        "explicit_lyrics": False,
        "fans": 100,
        # The real discography endpoint always includes `link`.
        "link": f"https://www.deezer.com/album/{id_}",
        "artist": {"id": 399, "name": "Radiohead"},
    }


@pytest.fixture
def scanner(config, store):
    nav_http = FakeHttp()
    dz_http = FakeHttp()
    navidrome_routes(nav_http)
    client = NavidromeClient(config, nav_http)
    provider = DeezerProvider(dz_http, cache=store, sleep=lambda _: None)
    scanner = Scanner(client, provider, store, config)
    scanner.nav_http = nav_http
    scanner.dz_http = dz_http
    return scanner


class TestCollaborationAlbums:
    """An album belongs to every library artist credited on it."""

    def _library(self, artists, albums):
        http = FakeHttp()
        http.add("ping.view", subsonic({"type": "navidrome"}))
        http.add(
            "getArtists.view",
            subsonic(
                {
                    "artists": {
                        "index": [
                            {
                                "artist": [
                                    {"id": i, "name": n, "albumCount": 1}
                                    for i, n in artists
                                ]
                            }
                        ]
                    }
                }
            ),
        )
        http.add(
            "getAlbumList2.view",
            lambda url: subsonic(
                {"albumList2": {"album": [] if "offset=500" in url else albums}}
            ),
        )
        return http

    def _scanner(self, config, store, http):
        return Scanner(NavidromeClient(config, http), None, store, config)

    def test_a_duo_album_reaches_both_artists(self, config, store):
        http = self._library(
            [("1", "Keith Urban"), ("2", "Michael McDonald")],
            [
                {
                    "id": "a1",
                    "name": "The Speed of Now",
                    "artist": "Keith Urban \u2022 Michael McDonald",
                    "artists": [
                        {"id": "1", "name": "Keith Urban"},
                        {"id": "2", "name": "Michael McDonald"},
                    ],
                    "songCount": 8,
                }
            ],
        )
        artists = self._scanner(config, store, http).read_library(ScanOptions())
        assert [[al.name for al in a.albums] for a in artists] == [
            ["The Speed of Now"],
            ["The Speed of Now"],
        ]

    def test_a_credited_album_is_no_longer_an_orphan(self, config, store):
        # The display string matches no album-artist entry at all, which used
        # to drop the album from the library entirely.
        http = self._library(
            [("1", "Keith Urban")],
            [
                {
                    "id": "a1",
                    "name": "Collab",
                    "artist": "Somebody \u2022 Keith Urban",
                    "artists": [{"id": "1", "name": "Keith Urban"}],
                    "songCount": 8,
                }
            ],
        )
        artists = self._scanner(config, store, http).read_library(ScanOptions())
        assert [al.name for al in artists[0].albums] == ["Collab"]

    def test_an_uncredited_album_still_reaches_its_album_artist(self, config, store):
        # A server without the OpenSubsonic arrays must behave as before.
        http = self._library(
            [("1", "Radiohead")],
            [{"id": "a1", "name": "Kid A", "artist": "Radiohead", "songCount": 10}],
        )
        artists = self._scanner(config, store, http).read_library(ScanOptions())
        assert [al.name for al in artists[0].albums] == ["Kid A"]

    def test_an_album_is_not_attached_twice(self, config, store):
        http = self._library(
            [("1", "Radiohead")],
            [
                {
                    "id": "a1",
                    "name": "Kid A",
                    "artist": "Radiohead",
                    "artistId": "1",
                    "artists": [{"id": "1", "name": "Radiohead"}],
                    "songCount": 10,
                }
            ],
        )
        artists = self._scanner(config, store, http).read_library(ScanOptions())
        assert len(artists[0].albums) == 1


class TestFullScan:
    def test_owned_album_is_not_reported_and_new_one_is(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [
                release_row(10, "Kid A", "2000-10-02"),
                release_row(11, "A Moon Shaped Pool", "2016-05-08"),
            ],
            {
                "10": {"title": "Kid A", "tracks": KID_A, "date": "2000-10-02"},
                "11": {
                    "title": "A Moon Shaped Pool",
                    "tracks": [("Burn the Witch", 220), ("Daydreaming", 384)],
                    "date": "2016-05-08",
                },
            },
        )
        result = scanner.run(ScanOptions(progress=False))
        titles = [m.release.title for m in result.missing]
        assert titles == ["A Moon Shaped Pool"]
        assert result.unresolved == []

    def test_advance_single_from_an_owned_album_is_suppressed(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [
                release_row(10, "Kid A", "2000-10-02"),
                release_row(20, "Optimistic", "2000-08-01", "single"),
            ],
            {
                "10": {"title": "Kid A", "tracks": KID_A},
                "20": {
                    "title": "Optimistic",
                    "tracks": [("Optimistic", 315)],
                    "record_type": "single",
                    "date": "2000-08-01",
                },
            },
        )
        result = scanner.run(ScanOptions(progress=False))
        assert [m.release.title for m in result.missing] == []

    def test_single_with_an_exclusive_track_is_reported(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [
                release_row(10, "Kid A", "2000-10-02"),
                release_row(21, "Optimistic", "2000-08-01", "single"),
            ],
            {
                "10": {"title": "Kid A", "tracks": KID_A},
                "21": {
                    "title": "Optimistic",
                    "tracks": [("Optimistic", 315), ("Cuttooth", 320)],
                    "record_type": "single",
                    "date": "2000-08-01",
                },
            },
        )
        result = scanner.run(ScanOptions(progress=False))
        assert [m.release.title for m in result.missing] == ["Optimistic"]
        assert result.missing[0].release_type is ReleaseType.SINGLE

    def test_duplicate_listings_are_collapsed(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [
                release_row(10, "Kid A", "2000-10-02"),
                release_row(30, "New Album", "2024-01-01"),
                release_row(31, "New Album", "2024-03-01"),
                release_row(32, "New Album (Deluxe Edition)", "2024-06-01"),
            ],
            {
                "10": {"title": "Kid A", "tracks": KID_A},
                "30": {"title": "New Album", "tracks": [("One", 200), ("Two", 210)]},
                "31": {"title": "New Album", "tracks": [("One", 200), ("Two", 210)]},
                "32": {"title": "New Album", "tracks": [("One", 200), ("Two", 210)]},
            },
        )
        result = scanner.run(ScanOptions(progress=False))
        assert len(result.missing) == 1, [m.release.title for m in result.missing]

    def test_results_are_globally_sorted_newest_first(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [
                release_row(10, "Kid A", "2000-10-02"),
                release_row(40, "Old One", "2005-01-01"),
                release_row(41, "New One", "2026-07-24", "single"),
                release_row(42, "Middle One", "2015-06-06", "ep"),
            ],
            {
                "10": {"title": "Kid A", "tracks": KID_A},
                "40": {"title": "Old One", "tracks": [(f"T{i}", 200) for i in range(9)]},
                "41": {
                    "title": "New One",
                    "tracks": [("Brand New", 200)],
                    "record_type": "single",
                },
                "42": {
                    "title": "Middle One",
                    "tracks": [(f"E{i}", 200) for i in range(5)],
                    "record_type": "ep",
                },
            },
        )
        result = scanner.run(ScanOptions(progress=False))
        items = sort_releases(result.missing)
        assert [i.release.title for i in items] == ["New One", "Middle One", "Old One"]
        assert [i.release_type for i in items] == [
            ReleaseType.SINGLE,
            ReleaseType.EP,
            ReleaseType.ALBUM,
        ]

    def test_output_matches_the_documented_shape(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [release_row(10, "Kid A", "2000-10-02"), release_row(50, "Fresh", "2026-07-24")],
            {
                "10": {"title": "Kid A", "tracks": KID_A},
                "50": {"title": "Fresh", "tracks": [(f"T{i}", 200) for i in range(9)]},
            },
        )
        result = scanner.run(ScanOptions(progress=False))
        items = sort_releases(result.missing)
        lines = render_table(items, width=140)
        assert lines[0].startswith("RELEASE DATE")
        assert "Albums (1)" in lines
        row = lines[lines.index("Albums (1)") + 1]
        assert row.startswith("2026-07-24")
        assert "Radiohead" in row and "Album" in row and "Fresh" in row
        assert row.endswith("https://www.deezer.com/album/50")

        out = io.StringIO()
        print_summary(build_summary(items, result.unresolved, result.review, 1, []), out)
        assert out.getvalue().startswith("1 missing release: 1 album")


class TestFilters:
    def test_since_year_filter(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [release_row(60, "Old", "1999-01-01"), release_row(61, "New", "2025-01-01")],
            {
                "60": {"title": "Old", "tracks": [(f"T{i}", 200) for i in range(9)]},
                "61": {"title": "New", "tracks": [(f"N{i}", 200) for i in range(9)]},
            },
        )
        result = scanner.run(ScanOptions(progress=False, since=ReleaseDate.parse("2020")))
        assert [m.release.title for m in result.missing] == ["New"]

    def test_type_filter(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [
                release_row(70, "An Album", "2025-01-01"),
                release_row(71, "A Single", "2025-02-01", "single"),
            ],
            {
                "70": {"title": "An Album", "tracks": [(f"T{i}", 200) for i in range(9)]},
                "71": {
                    "title": "A Single",
                    "tracks": [("Solo", 200)],
                    "record_type": "single",
                },
            },
        )
        result = scanner.run(ScanOptions(progress=False, types={ReleaseType.SINGLE}))
        assert [m.release.title for m in result.missing] == ["A Single"]

    def test_artist_filter_selects_a_subset(self, scanner):
        deezer_routes(scanner.dz_http, [], {})
        result = scanner.run(ScanOptions(progress=False, artist_filters=["Nobody Here"]))
        assert result.artists_scanned == 0


class TestPartialFailures:
    def test_deezer_failure_for_one_artist_does_not_abort_the_scan(self, scanner):
        scanner.dz_http.add("/search/artist", ConnectionTimeoutError("timed out"))
        result = scanner.run(ScanOptions(progress=False))
        assert result.artists_scanned == 1
        assert result.unresolved, "the artist should be reported as unresolved"

    def test_navidrome_track_failure_is_reported_as_partial(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [release_row(80, "Something New", "2025-01-01")],
            {"80": {"title": "Something New", "tracks": [(f"T{i}", 200) for i in range(9)]}},
        )
        scanner.nav_http.add("getAlbum.view", ConnectionTimeoutError("timed out"))
        result = scanner.run(ScanOptions(progress=False))
        assert result.partial_reasons
        summary = build_summary(result.missing, [], [], 1, result.partial_reasons)
        assert summary.partial

    def test_unresolved_artist_still_lets_the_scan_finish(self, scanner):
        scanner.dz_http.add("/search/artist", {"data": []})
        result = scanner.run(ScanOptions(progress=False))
        assert result.unresolved[0].name == "Radiohead"
        assert result.missing == []


class TestCachingAcrossRuns:
    def test_second_run_makes_no_new_deezer_requests(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [release_row(10, "Kid A", "2000-10-02")],
            {"10": {"title": "Kid A", "tracks": KID_A}},
        )
        scanner.run(ScanOptions(progress=False))
        first = len(scanner.dz_http.requests)
        scanner.run(ScanOptions(progress=False))
        assert len(scanner.dz_http.requests) == first


class TestMissingQueueIsPersisted:
    """`rip` runs in a separate process, so the results have to outlive the scan."""

    def test_notes_travel_with_the_entry(self, scanner):
        # A remix single whose release title says nothing: the marker is on
        # the track, and `rip` has to see it as plainly as the scan table did.
        deezer_routes(
            scanner.dz_http,
            [release_row(20, "Castaway", "2025-12-12", record_type="single")],
            {
                "20": {
                    "title": "Castaway",
                    "tracks": [("Castaway", 271)],
                    "versions": ["(Sunset Tsunami & MO2 Remix)"],
                    "date": "2025-12-12",
                    "record_type": "single",
                }
            },
        )
        scanner.run(ScanOptions(progress=False))

        saved = scanner.store.load_missing()
        assert [e["title"] for e in saved] == ["Castaway"]
        assert saved[0]["notes"] == "remix"

    def test_an_unremarkable_release_stores_empty_notes(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [release_row(21, "A Moon Shaped Pool", "2016-05-08")],
            {
                "21": {
                    "title": "A Moon Shaped Pool",
                    "tracks": [(f"Track {i}", 220) for i in range(1, 8)],
                    "date": "2016-05-08",
                }
            },
        )
        scanner.run(ScanOptions(progress=False))
        assert scanner.store.load_missing()[0]["notes"] == ""

    def test_reported_releases_are_saved(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [
                release_row(10, "Kid A", "2000-10-02"),
                release_row(11, "A Moon Shaped Pool", "2016-05-08"),
            ],
            {
                "10": {"title": "Kid A", "tracks": KID_A, "date": "2000-10-02"},
                "11": {
                    "title": "A Moon Shaped Pool",
                    # Seven tracks: fewer and the classifier reads the shape as
                    # an EP or single regardless of the declared record_type.
                    "tracks": [(f"Track {i}", 220) for i in range(1, 8)],
                    "date": "2016-05-08",
                },
            },
        )
        scanner.run(ScanOptions(progress=False))

        saved = scanner.store.load_missing()
        assert [e["title"] for e in saved] == ["A Moon Shaped Pool"]
        entry = saved[0]
        assert entry["id"] == "11"
        assert entry["artist"] == "Radiohead"
        assert entry["type"] == ReleaseType.ALBUM.value
        assert entry["date"] == "2016-05-08"
        assert entry["url"].endswith("/album/11")

    def test_owned_releases_are_not_saved(self, scanner):
        deezer_routes(
            scanner.dz_http,
            [release_row(10, "Kid A", "2000-10-02")],
            {"10": {"title": "Kid A", "tracks": KID_A, "date": "2000-10-02"}},
        )
        scanner.run(ScanOptions(progress=False))
        assert scanner.store.load_missing() == []

    def test_a_type_filtered_scan_saves_only_what_it_reported(self, scanner):
        """The queue reflects the report, so `rip` cannot offer a filtered-out release."""
        deezer_routes(
            scanner.dz_http,
            [
                release_row(10, "Kid A", "2000-10-02"),
                release_row(43, "New Album", "2024-01-01"),
                release_row(44, "New Single", "2024-02-01", "single"),
            ],
            {
                "10": {"title": "Kid A", "tracks": KID_A, "date": "2000-10-02"},
                "43": {
                    "title": "New Album",
                    "tracks": [(f"A{i}", 200) for i in range(1, 8)],
                },
                "44": {"title": "New Single", "tracks": [("C", 190)]},
            },
        )
        scanner.run(ScanOptions(progress=False, types={ReleaseType.ALBUM}))
        assert [e["title"] for e in scanner.store.load_missing()] == ["New Album"]

    @staticmethod
    def _one_album(scanner):
        deezer_routes(
            scanner.dz_http,
            [release_row(43, "New Album", "2024-01-01")],
            {
                "43": {
                    "title": "New Album",
                    "tracks": [(f"A{i}", 200) for i in range(1, 8)],
                }
            },
        )

    def test_the_queue_stamps_nothing_about_favourite_ness(self, scanner):
        """Entries carry no favourite-ness of their own. A stamp described the
        artist at write time and had no way to stop being true."""
        self._one_album(scanner)
        scanner.run(ScanOptions(progress=False))
        assert all("favorites" not in e for e in scanner.store.load_missing())

    def test_a_favourites_scan_records_who_the_favourites_were(self, scanner):
        """The one axis `rip` cannot recover from a stored entry, so the scan
        records the names and the walk replays them."""
        self._one_album(scanner)
        scanner.nav_http.add(
            "getStarred2.view",
            subsonic({"starred2": {"artist": [{"id": "1", "name": "Radiohead"}]}}),
        )
        scanner.run(ScanOptions(progress=False, favorites=True))
        scope = scanner.store.load_scan_scope()
        assert scope.favorites is True
        assert scope.favorite_artists == ("Radiohead",)

    def test_an_ex_favourite_leaves_the_walk_when_it_leaves_the_favourites(
        self, scanner
    ):
        """The reported bug, end to end.

        A favourites scan finds a release; the artist is later unstarred. No
        favourites scan covers them again, so the merge can never purge the
        entry — the walk has to stop offering it on the strength of the scope
        alone, or it offers a release the scan did not report.
        """
        self._one_album(scanner)
        scanner.nav_http.add(
            "getStarred2.view",
            subsonic({"starred2": {"artist": [{"id": "1", "name": "Radiohead"}]}}),
        )
        scanner.run(ScanOptions(progress=False, favorites=True))
        assert [e["title"] for e in scanner.store.load_missing()] == ["New Album"]

        # Unstarred, and a second favourite scanned in their place so the run
        # still has something to cover.
        scanner.nav_http.add(
            "getStarred2.view",
            subsonic({"starred2": {"artist": [{"id": "2", "name": "Nobody"}]}}),
        )
        scanner.run(ScanOptions(progress=False, favorites=True))

        # The merge cannot reach it: the artist was not covered by that run.
        assert [e["title"] for e in scanner.store.load_missing()] == ["New Album"]
        # The walk must exclude it anyway.
        store = scanner.store
        assert in_scope(store.load_missing(), store.load_scan_scope()) == []

    def test_the_scan_records_the_scope_it_ran_with(self, scanner):
        """`rip` replays this, so it must describe the scan as resolved — not
        as typed. A flag and the setting it overrode are the same fact here."""
        self._one_album(scanner)
        scanner.run(
            ScanOptions(
                progress=False,
                since=ReleaseDate.parse("2020"),
                types={ReleaseType.ALBUM},
                artist_filters=["Radiohead"],
            )
        )
        assert scanner.store.load_scan_scope() == ScanScope(
            favorites=False,
            since=ReleaseDate.parse("2020"),
            types=frozenset({"Album"}),
            artists=("Radiohead",),
        )

    def test_a_wider_scan_clears_the_previous_scope(self, scanner):
        """"No cutoff" has to be recorded, not merely left behind: otherwise a
        later unfiltered scan would still be walked through the old window."""
        self._one_album(scanner)
        scanner.run(
            ScanOptions(
                progress=False,
                since=ReleaseDate.parse("2020"),
                types={ReleaseType.ALBUM},
            )
        )
        scanner.run(ScanOptions(progress=False))
        assert scanner.store.load_scan_scope() == ScanScope()

    def test_limit_is_not_part_of_the_scope(self, scanner):
        """It truncates how much work the scan does rather than describing
        which releases belong in the results, so there is nothing to replay."""
        self._one_album(scanner)
        scanner.run(ScanOptions(progress=False, limit=1))
        assert scanner.store.load_scan_scope() == ScanScope()

    def test_another_artist_survives_a_filtered_scan(self, scanner):
        scanner.store.save_missing(
            [{"id": "9", "artist": "Someone Else", "title": "Theirs", "type": "Album",
              "date": "2024-01-01", "ownership": "missing", "url": ""}]
        )
        deezer_routes(scanner.dz_http, [], {})
        scanner.dz_http.add("/search/artist", {"data": []})
        scanner.run(ScanOptions(progress=False, artist_filters=["Radiohead"]))
        assert "Someone Else" in {e["artist"] for e in scanner.store.load_missing()}


class TestQueuesSurviveFilteredScans:
    """A single-artist rescan must not discard everyone else's pending questions."""

    def test_unresolved_from_other_artists_is_kept(self, scanner):
        scanner.store.save_unresolved(
            [{"name": "Someone Else", "reason": "ambiguous", "candidates": []}]
        )
        deezer_routes(scanner.dz_http, [], {})
        scanner.dz_http.add("/search/artist", {"data": []})
        scanner.run(ScanOptions(progress=False, artist_filters=["Radiohead"]))
        names = {e["name"] for e in scanner.store.load_unresolved()}
        assert "Someone Else" in names, "another artist's entry was discarded"

    def test_review_items_from_other_artists_are_kept(self, scanner):
        scanner.store.save_review(
            [{"id": "9", "artist": "Someone Else", "title": "T", "type": "Album",
              "date": "2024-01-01", "reason": "unclear", "url": ""}]
        )
        deezer_routes(scanner.dz_http, [], {})
        scanner.dz_http.add("/search/artist", {"data": []})
        scanner.run(ScanOptions(progress=False, artist_filters=["Radiohead"]))
        artists = {e["artist"] for e in scanner.store.load_review()}
        assert "Someone Else" in artists

    def test_a_rescanned_artist_gets_refreshed_not_duplicated(self, scanner):
        scanner.store.save_unresolved(
            [{"name": "Radiohead", "reason": "stale reason", "candidates": []}]
        )
        scanner.dz_http.add("/search/artist", {"data": []})
        deezer_routes(scanner.dz_http, [], {})
        scanner.dz_http.add("/search/artist", {"data": []})
        scanner.run(ScanOptions(progress=False, artist_filters=["Radiohead"]))
        entries = [e for e in scanner.store.load_unresolved() if e["name"] == "Radiohead"]
        assert len(entries) == 1, "the stale entry should be replaced, not appended"
        assert entries[0]["reason"] != "stale reason"

    def test_a_full_scan_still_replaces_everything(self, scanner):
        scanner.store.save_unresolved(
            [{"name": "Radiohead", "reason": "stale", "candidates": []}]
        )
        deezer_routes(
            scanner.dz_http,
            [release_row(10, "Kid A", "2000-10-02")],
            {"10": {"title": "Kid A", "tracks": KID_A}},
        )
        scanner.run(ScanOptions(progress=False))
        assert scanner.store.load_unresolved() == []
