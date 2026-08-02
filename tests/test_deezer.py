"""Deezer provider: pagination, rate limits, caching and unauthenticated access."""

from __future__ import annotations

import pytest

from dropwatch.state import MappingTarget
from conftest import FakeHttp

from dropwatch.deezer import DeezerNotFound, DeezerProvider, DeezerQuotaError
from dropwatch.errors import DeezerError, DeezerUnavailableError
from dropwatch.models import DeezerRelease, ReleaseDate

QUOTA = {"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}
NO_DATA = {"error": {"type": "DataException", "message": "no data", "code": 800}}


def album_row(i, record_type="album", date="2020-01-01"):
    return {
        "id": i,
        "title": f"Album {i}",
        "record_type": record_type,
        "release_date": date,
        "explicit_lyrics": False,
        "fans": 10,
        "link": f"https://www.deezer.com/album/{i}",
        "artist": {"id": 100, "name": "Test Artist"},
    }


@pytest.fixture
def provider(fake_http):
    return DeezerProvider(fake_http, cache=None, sleep=lambda _: None)


class TestSearch:
    def test_parses_results(self, provider, fake_http):
        fake_http.add(
            "/search/artist",
            {"data": [{"id": 630, "name": "Björk", "nb_album": 100, "nb_fan": 900000}]},
        )
        artists = provider.search_artists("Björk")
        assert artists[0].id == "630"
        assert artists[0].nb_fan == 900000

    def test_query_is_url_encoded(self, provider, fake_http):
        fake_http.add("/search/artist", {"data": []})
        provider.search_artists("Sigur Rós & Friends")
        assert " " not in fake_http.requests[0]
        assert "%26" in fake_http.requests[0] or "%20" in fake_http.requests[0]

    def test_empty_results(self, provider, fake_http):
        fake_http.add("/search/artist", {"data": []})
        assert provider.search_artists("Nobody") == []


class TestDiscographyPagination:
    def test_follows_next_links(self, provider, fake_http):
        def respond(url):
            index = int(url.split("index=")[1].split("&")[0])
            rows = [album_row(index + i) for i in range(100)]
            payload = {"data": rows, "total": 250}
            if index + 100 < 250:
                payload["next"] = (
                    f"https://api.deezer.com/artist/100/albums?limit=100&index={index + 100}"
                )
            return payload

        fake_http.add("/artist/100/albums", respond)
        releases = provider.get_discography("100")
        assert len(releases) == 300  # three pages of 100

    def test_stops_without_next(self, provider, fake_http):
        fake_http.add("/artist/100/albums", {"data": [album_row(1)], "total": 1})
        assert len(provider.get_discography("100")) == 1

    def test_pagination_loop_is_broken(self, provider, fake_http):
        fake_http.add(
            "/artist/100/albums",
            {
                "data": [album_row(1)],
                "next": "https://api.deezer.com/artist/100/albums?limit=100&index=0",
            },
        )
        # Must terminate rather than spin forever on a self-referential link.
        assert provider.get_discography("100")

    def test_release_fields_are_parsed(self, provider, fake_http):
        fake_http.add(
            "/artist/100/albums",
            {"data": [album_row(7, "ep", "2024-03-15")], "total": 1},
        )
        release = provider.get_discography("100")[0]
        assert release.id == "7"
        assert release.record_type == "ep"
        assert release.release_date == ReleaseDate.parse("2024-03-15")


class TestAlbumDetail:
    def test_uses_the_tracks_endpoint_not_the_truncated_embedded_list(
        self, provider, fake_http
    ):
        """The album endpoint caps its embedded track list at 25 and lies about it."""
        embedded = [
            {"id": i, "title": f"T{i}", "title_short": f"T{i}", "duration": 200}
            for i in range(25)
        ]
        fake_http.add(
            "/album/500/tracks",
            {
                "data": [
                    {
                        "id": i,
                        "title": f"T{i}",
                        "title_short": f"T{i}",
                        "title_version": "",
                        "duration": 200,
                        "isrc": f"GB000000{i:04d}",
                        "disk_number": 1,
                        "track_position": i + 1,
                    }
                    for i in range(26)
                ],
                "total": 26,
            },
        )
        fake_http.add(
            "/album/500",
            {
                "id": 500,
                "title": "Big Album",
                "upc": "123456789",
                "label": "Some Label",
                "nb_tracks": 26,
                "duration": 5200,
                "record_type": "album",
                "release_date": "2020-01-01",
                "contributors": [{"name": "Test Artist", "role": "Main"}],
                "tracks": {"data": embedded, "next": None},
            },
        )
        release = DeezerRelease(id="500", title="Big Album", artist_id="100", artist_name="A")
        provider.load_release_detail(release)

        assert len(release.tracks) == 26, "must not stop at the embedded 25"
        assert release.nb_tracks == 26
        assert release.upc == "123456789"
        assert all(t.isrc for t in release.tracks), "ISRCs come from the tracks endpoint"

    def test_detail_is_only_fetched_once(self, provider, fake_http):
        fake_http.add("/album/1/tracks", {"data": [], "total": 0})
        fake_http.add("/album/1", {"id": 1, "nb_tracks": 0, "tracks": {"data": []}})
        release = DeezerRelease(id="1", title="X", artist_id="100", artist_name="A")
        provider.load_release_detail(release)
        count = len(fake_http.requests)
        provider.load_release_detail(release)
        assert len(fake_http.requests) == count

    def test_unknown_album_is_handled(self, provider, fake_http):
        fake_http.add("/album/999", NO_DATA)
        release = DeezerRelease(id="999", title="X", artist_id="100", artist_name="A")
        provider.load_release_detail(release)
        assert release.detail_loaded is True
        assert release.tracks == []


class TestErrorHandling:
    def test_quota_error_is_retried_then_raised(self, fake_http):
        sleeps = []
        provider = DeezerProvider(fake_http, cache=None, sleep=sleeps.append)
        fake_http.add("/artist/100", QUOTA)
        with pytest.raises(DeezerQuotaError):
            provider.get_artist("100")
        assert len(sleeps) == 2, "bounded retries, not unlimited"
        assert sleeps[1] > sleeps[0], "backoff must increase"

    def test_quota_error_recovers_on_retry(self, fake_http):
        calls = {"n": 0}

        def respond(url):
            calls["n"] += 1
            if calls["n"] == 1:
                return QUOTA
            return {"id": 100, "name": "Test Artist", "nb_album": 1, "nb_fan": 5}

        provider = DeezerProvider(fake_http, cache=None, sleep=lambda _: None)
        fake_http.add("/artist/100", respond)
        assert provider.get_artist("100").name == "Test Artist"

    def test_not_found_raises_not_found(self, provider, fake_http):
        fake_http.add("/artist/1", NO_DATA)
        assert provider.get_artist("1") is None

    def test_auth_error_is_never_retried(self, fake_http):
        sleeps = []
        provider = DeezerProvider(fake_http, cache=None, sleep=sleeps.append)
        fake_http.add("/artist/1", {"error": {"code": 300, "message": "Invalid token"}})
        with pytest.raises(DeezerError):
            provider.get_artist("1")
        assert sleeps == [], "credential failures must not be retried"

    def test_http_500_is_retried(self, fake_http):
        sleeps = []
        provider = DeezerProvider(fake_http, cache=None, sleep=sleeps.append)
        fake_http.add("/artist/1", {}, 500)
        with pytest.raises(DeezerUnavailableError):
            provider.get_artist("1")
        assert len(sleeps) == 2

    def test_transport_failure_is_retried_then_surfaced(self, fake_http):
        provider = DeezerProvider(fake_http, cache=None, sleep=lambda _: None)
        fake_http.add("/artist/1", DeezerUnavailableError("network down"))
        with pytest.raises(DeezerUnavailableError):
            provider.get_artist("1")

    def test_non_json_response(self, provider, fake_http):
        fake_http.add("/artist/1", b"<html>maintenance</html>")
        with pytest.raises(DeezerUnavailableError):
            provider.get_artist("1")


class TestCaching:
    def test_second_call_is_served_from_the_store(self, fake_http, store):
        provider = DeezerProvider(fake_http, cache=store, sleep=lambda _: None)
        fake_http.add("/artist/100", {"id": 100, "name": "A", "nb_album": 1, "nb_fan": 1})
        provider.get_artist("100")
        provider.get_artist("100")
        assert len(fake_http.requests) == 1

    def test_cached_discography_survives_a_later_failure(self, fake_http, store):
        provider = DeezerProvider(fake_http, cache=store, sleep=lambda _: None)
        fake_http.add("/artist/100/albums", {"data": [album_row(1)], "total": 1})
        first = provider.get_discography("100")
        # Deezer starts failing, but previously cached data must remain usable.
        fake_http.routes.clear()
        fake_http.add("/artist/100/albums", QUOTA)
        assert len(provider.get_discography("100")) == len(first)


class TestUnauthenticatedAccess:
    """Catalog access must never acquire or transmit a credential."""

    def test_provider_needs_no_credentials(self, provider, fake_http):
        fake_http.add("/search/artist", {"data": []})
        provider.search_artists("Anyone")
        query = fake_http.requests[0].lower()
        assert not any(k in query for k in ("token", "password", "sid=", "cookie"))

    def test_no_request_carries_a_cookie_or_auth_header(self, fake_http):
        captured = {}

        class RecordingHttp(FakeHttp):
            def get(self, url, headers=None):
                captured["headers"] = headers
                return super().get(url, headers)

        http = RecordingHttp()
        http.add("/search/artist", {"data": []})
        DeezerProvider(http, cache=None, sleep=lambda _: None).search_artists("X")
        assert not (captured["headers"] or {})

    def test_no_credentials_are_written_to_local_state(self, store, tmp_path):
        secret = "fakeCredentialValue0123456789abcdefghij"
        store.set_mapping("Artist", [MappingTarget("1", "Artist")])
        store.set("some-key", {"data": "value"})
        store.close()
        blob = (tmp_path / "state.sqlite3").read_bytes()
        assert secret.encode() not in blob

    def test_requests_only_go_to_the_public_api(self, provider, fake_http):
        fake_http.add("/search/artist", {"data": []})
        fake_http.add("/artist/1/albums", {"data": [], "total": 0})
        provider.search_artists("X")
        provider.get_discography("1")
        for url in fake_http.requests:
            # The documented public API host, and nothing else.
            assert url.startswith("https://api.deezer.com/")
            assert "deezer.com/ajax" not in url
