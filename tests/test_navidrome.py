"""Navidrome client: error taxonomy, auth and partial failures."""

from __future__ import annotations

import socket
import ssl
import urllib.error

import pytest
from conftest import FakeHttp, subsonic, subsonic_error

from dropwatch.errors import (
    BadPathError,
    ConnectionTimeoutError,
    HostResolutionError,
    HostUnreachableError,
    NavidromeAuthError,
    NotSubsonicError,
    TLSError,
    UnexpectedResponseError,
)
from dropwatch.http import _translate_url_error
from dropwatch.models import LocalAlbum
from dropwatch.navidrome import NavidromeClient


@pytest.fixture
def client(config, fake_http):
    return NavidromeClient(config, fake_http)


class TestErrorTaxonomy:
    """Network problems must never be reported as credential problems."""

    def test_dns_failure(self):
        error = _translate_url_error(
            urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided")),
            "http://example:4533",
        )
        assert isinstance(error, HostResolutionError)
        assert "tailscale" in (error.hint or "").lower()

    def test_connection_refused_points_at_the_port(self):
        error = _translate_url_error(
            urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
            "http://example:4533",
        )
        assert isinstance(error, HostUnreachableError)
        assert "port" in (error.hint or "").lower()

    def test_no_route_to_host(self):
        error = _translate_url_error(
            urllib.error.URLError(OSError(65, "No route to host")), "http://example:4533"
        )
        assert isinstance(error, HostUnreachableError)

    def test_timeout(self):
        error = _translate_url_error(
            urllib.error.URLError(TimeoutError("timed out")), "http://example:4533"
        )
        assert isinstance(error, ConnectionTimeoutError)

    def test_tls_verification_failure(self):
        error = _translate_url_error(
            urllib.error.URLError(ssl.SSLCertVerificationError("self signed certificate")),
            "https://example:4533",
        )
        assert isinstance(error, TLSError)

    def test_each_condition_has_a_distinct_type(self):
        types = {
            type(
                _translate_url_error(urllib.error.URLError(reason), "http://example:4533")
            )
            for reason in [
                socket.gaierror(8, "x"),
                ConnectionRefusedError(61, "x"),
                TimeoutError("x"),
                ssl.SSLCertVerificationError("x"),
            ]
        }
        assert len(types) == 4


class TestPing:
    def test_successful_ping_reports_the_server(self, client, fake_http):
        fake_http.add("ping.view", subsonic({"type": "navidrome", "serverVersion": "0.53.3"}))
        assert client.ping() == "navidrome 0.53.3"

    def test_wrong_password_is_an_auth_error(self, client, fake_http):
        fake_http.add("ping.view", subsonic_error(40, "Wrong username or password"))
        with pytest.raises(NavidromeAuthError) as excinfo:
            client.ping()
        assert "credentials" in str(excinfo.value).lower()

    def test_password_never_appears_in_the_request(self, client, fake_http):
        fake_http.add("ping.view", subsonic({"type": "navidrome"}))
        client.ping()
        assert "not-a-real-password" not in fake_http.requests[0]
        # Salted token auth: a token and salt are sent instead.
        assert "t=" in fake_http.requests[0] and "s=" in fake_http.requests[0]

    def test_salt_changes_between_requests(self, client, fake_http):
        fake_http.add("ping.view", subsonic({"type": "navidrome"}))
        client.ping()
        client.ping()
        assert fake_http.requests[0] != fake_http.requests[1]

    def test_html_response_is_not_subsonic(self, client, fake_http):
        fake_http.add("ping.view", b"<!DOCTYPE html><html><body>Navidrome UI</body></html>")
        with pytest.raises(NotSubsonicError):
            client.ping()

    def test_json_from_another_service_is_not_subsonic(self, client, fake_http):
        fake_http.add("ping.view", {"message": "hello from some other api"})
        with pytest.raises(NotSubsonicError):
            client.ping()

    def test_404_is_a_path_problem(self, client, fake_http):
        fake_http.add("ping.view", {"error": "not found"}, 404)
        with pytest.raises(BadPathError) as excinfo:
            client.ping()
        # Hints name the setting you would change, not the variable.
        assert "url" in (excinfo.value.hint or "")

    def test_server_error_is_unexpected_response(self, client, fake_http):
        fake_http.add("ping.view", {}, 503)
        with pytest.raises(UnexpectedResponseError):
            client.ping()

    def test_unexpected_subsonic_error_is_not_an_auth_error(self, client, fake_http):
        fake_http.add("ping.view", subsonic_error(0, "A generic error"))
        with pytest.raises(UnexpectedResponseError):
            client.ping()


class TestLibraryReads:
    def test_artists_are_flattened_from_index_buckets(self, client, fake_http):
        fake_http.add(
            "getArtists.view",
            subsonic(
                {
                    "artists": {
                        "index": [
                            {"name": "R", "artist": [{"id": "1", "name": "Radiohead", "albumCount": 9}]},
                            {"name": "B", "artist": [{"id": "2", "name": "Björk", "albumCount": 10}]},
                        ]
                    }
                }
            ),
        )
        artists = client.get_artists()
        assert {a.name for a in artists} == {"Radiohead", "Björk"}
        assert artists[0].album_count == 9

    def test_empty_library_is_handled(self, client, fake_http):
        fake_http.add("getArtists.view", subsonic({"artists": {}}))
        assert client.get_artists() == []

    def test_album_pagination(self, client, fake_http):
        def respond(url):
            offset = int(url.split("offset=")[1].split("&")[0])
            if offset == 0:
                albums = [
                    {"id": str(i), "name": f"Album {i}", "artist": "A", "artistId": "1"}
                    for i in range(500)
                ]
            elif offset == 500:
                albums = [{"id": "500", "name": "Last", "artist": "A", "artistId": "1"}]
            else:
                albums = []
            return subsonic({"albumList2": {"album": albums}})

        fake_http.add("getAlbumList2.view", respond)
        albums = client.get_all_albums()
        assert len(albums) == 501
        assert albums[-1].name == "Last"

    def test_album_fields_are_parsed(self, client, fake_http):
        fake_http.add(
            "getAlbumList2.view",
            subsonic(
                {
                    "albumList2": {
                        "album": [
                            {
                                "id": "a1",
                                "name": "Kid A",
                                "artist": "Radiohead",
                                "artistId": "1",
                                "year": 2000,
                                "songCount": 10,
                                "duration": 2700,
                            }
                        ]
                    }
                }
            ),
        )
        album = client.get_all_albums()[0]
        assert (album.name, album.year, album.song_count) == ("Kid A", 2000, 10)

    def test_missing_year_does_not_crash(self, client, fake_http):
        fake_http.add(
            "getAlbumList2.view",
            subsonic({"albumList2": {"album": [{"id": "a1", "name": "X", "artist": "A"}]}}),
        )
        assert client.get_all_albums()[0].year is None

    def test_tracks_are_parsed(self, client, fake_http):
        fake_http.add(
            "getAlbum.view",
            subsonic(
                {
                    "album": {
                        "id": "a1",
                        "song": [
                            {"id": "s1", "title": "Idioteque", "duration": 289, "track": 8, "discNumber": 1},
                        ],
                    }
                }
            ),
        )
        tracks = client.get_album_tracks("a1")
        assert tracks[0].title == "Idioteque"
        assert tracks[0].duration == 289
        assert tracks[0].disc == 1


class TestPartialFailures:
    def test_one_failing_album_does_not_abort_the_rest(self, client, fake_http):
        def respond(url):
            if "id=bad" in url:
                raise ConnectionTimeoutError("timed out")
            return subsonic({"album": {"song": [{"id": "s", "title": "Fine", "duration": 100}]}})

        fake_http.add("getAlbum.view", respond)
        albums = [
            LocalAlbum(id="good", name="Good", artist="A"),
            LocalAlbum(id="bad", name="Bad", artist="A"),
        ]
        failures = client.load_tracks(albums, workers=1)
        assert failures == 1
        assert albums[0].tracks_loaded is True
        assert albums[1].tracks_loaded is False
        assert albums[0].tracks[0].title == "Fine"

    def test_read_only_endpoints_only(self, client, fake_http):
        """No request may hit a mutating Subsonic endpoint."""
        fake_http.default = subsonic({"artists": {"index": []}, "albumList2": {"album": []}})
        client.get_artists()
        client.get_all_albums()
        forbidden = (
            "star", "unstar", "setRating", "scrobble", "createPlaylist",
            "updatePlaylist", "deletePlaylist", "startScan", "download", "stream",
        )
        for url in fake_http.requests:
            endpoint = url.split("/rest/")[1].split("?")[0]
            assert not any(endpoint.startswith(name) for name in forbidden), url
