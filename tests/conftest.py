"""Shared fixtures. No test in this suite performs real network I/O."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dropwatch.config import Config  # noqa: E402
from dropwatch.http import Response  # noqa: E402
from dropwatch.models import (  # noqa: E402
    DeezerRelease,
    DeezerTrack,
    LocalAlbum,
    LocalArtist,
    LocalTrack,
    ReleaseDate,
)
from dropwatch.secrets import Secret  # noqa: E402
from dropwatch.state import Store  # noqa: E402


class FakeHttp:
    """Stands in for HttpClient, matching request URLs against substrings."""

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes: dict[str, object] = dict(routes or {})
        self.requests: list[str] = []
        self.default: object | None = None

    def add(self, fragment: str, payload, status: int = 200) -> None:
        self.routes[fragment] = (payload, status)

    def get(self, url: str, headers=None) -> Response:
        self.requests.append(url)
        for fragment, entry in self.routes.items():
            if fragment in url:
                return self._respond(entry, url)
        if self.default is not None:
            return self._respond(self.default, url)
        raise AssertionError(f"No fake route matched {url}")

    def _respond(self, entry, url: str) -> Response:
        payload, status = entry if isinstance(entry, tuple) else (entry, 200)
        # Routes may be callables so a test can vary the reply per request.
        if callable(payload):
            result = payload(url)
            payload, status = result if isinstance(result, tuple) else (result, status)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, bytes):
            body, content_type = payload, "text/html"
        else:
            body, content_type = json.dumps(payload).encode(), "application/json"
        return Response(status=status, body=body, content_type=content_type, url=url)


@pytest.fixture(autouse=True)
def isolate_user_directories(tmp_path_factory, monkeypatch):
    """Point config and state at throwaway directories for every test.

    Without this, a test that forgets to isolate itself silently writes into
    the developer's real ~/.config and ~/.local/state.
    """
    base = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("DROPWATCH_CONFIG_DIR", str(base / "config"))
    monkeypatch.setenv("DROPWATCH_STATE_DIR", str(base / "state"))
    monkeypatch.delenv("DROPWATCH_ENV", raising=False)


@pytest.fixture
def fake_http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        navidrome_url="http://example:4533",
        navidrome_username="tester",
        navidrome_password=Secret("not-a-real-password"),
        request_timeout=5.0,
        cache_path=tmp_path / "state.sqlite3",
        cache_max_age_hours=24.0,
    )


@pytest.fixture
def store(tmp_path: Path):
    with Store(tmp_path / "state.sqlite3") as s:
        yield s


# --- builders --------------------------------------------------------------


def subsonic(payload: dict) -> dict:
    return {"subsonic-response": {"status": "ok", "version": "1.16.1", **payload}}


def subsonic_error(code: int, message: str) -> dict:
    return {
        "subsonic-response": {
            "status": "failed",
            "version": "1.16.1",
            "error": {"code": code, "message": message},
        }
    }


def local_album(
    name: str,
    artist: str = "Test Artist",
    year: int | None = 2020,
    tracks: list[tuple] | None = None,
    album_id: str = "",
    artists: list[str] | None = None,
) -> LocalAlbum:
    """A local album. Track tuples are (title, duration) or (title, duration,
    credits) — where credits is the song's own artist string, or a list of
    structured per-track credit names for a guest appearance."""
    track_list = []
    for i, entry in enumerate(tracks or []):
        title, duration, credits = (*entry, None)[:3]
        names = [credits] if isinstance(credits, str) else list(credits or [])
        track_list.append(
            LocalTrack(
                title=title,
                duration=duration,
                track=i + 1,
                artist=names[0] if names else artist,
                artists=tuple(names[1:]),
                album_artist=artist,
            )
        )
    album = LocalAlbum(
        id=album_id or f"al-{abs(hash((name, artist))) % 100000}",
        name=name,
        artist=artist,
        year=year,
        song_count=len(track_list),
        duration=sum(t.duration or 0 for t in track_list) or None,
        tracks=track_list,
        tracks_loaded=True,
        artists=tuple(artists or ()),
    )
    return album


def local_artist(name: str = "Test Artist", albums: list[LocalAlbum] | None = None) -> LocalArtist:
    albums = albums or []
    return LocalArtist(
        id=f"ar-{abs(hash(name)) % 100000}",
        name=name,
        album_count=len(albums),
        albums=albums,
    )


def deezer_release(
    title: str,
    release_id: str = "1",
    date: str = "2024-01-01",
    record_type: str = "album",
    tracks: list[tuple[str, int]] | None = None,
    artist_name: str = "Test Artist",
    artist_id: str = "100",
    versions: list[str] | None = None,
    isrcs: list[str] | None = None,
    upc: str | None = None,
    detail_loaded: bool | None = None,
) -> DeezerRelease:
    track_objects = []
    for i, (track_title, duration) in enumerate(tracks or []):
        track_objects.append(
            DeezerTrack(
                id=f"{release_id}-{i}",
                title=track_title,
                title_short=track_title,
                title_version=(versions[i] if versions and i < len(versions) else ""),
                duration=duration,
                isrc=(isrcs[i] if isrcs and i < len(isrcs) else None),
                position=i + 1,
                artist=artist_name,
            )
        )
    return DeezerRelease(
        id=release_id,
        title=title,
        artist_id=artist_id,
        artist_name=artist_name,
        link=f"https://www.deezer.com/album/{release_id}",
        record_type=record_type,
        release_date=ReleaseDate.parse(date),
        nb_tracks=len(track_objects) or None,
        duration=sum(t.duration or 0 for t in track_objects) or None,
        upc=upc,
        tracks=track_objects,
        detail_loaded=detail_loaded if detail_loaded is not None else bool(track_objects),
    )
