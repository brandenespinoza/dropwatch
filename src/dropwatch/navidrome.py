"""Read-only Navidrome client over the Subsonic/OpenSubsonic REST API.

Only GET endpoints that read the library are used. Nothing here writes,
scans, rates, favourites or touches playlists.

Authentication uses Subsonic's salted-token scheme (``t=md5(password+salt)``
with a fresh random salt per request) so the password itself never appears in
a URL, a proxy log or this process's own debug output.
"""

from __future__ import annotations

import hashlib
import logging
import secrets as _secrets
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterator
from urllib.parse import urlencode

from .config import Config
from .errors import (
    BadPathError,
    NavidromeAuthError,
    NotSubsonicError,
    UnexpectedResponseError,
)
from .http import HttpClient
from .models import LocalAlbum, LocalArtist, LocalTrack

log = logging.getLogger("dropwatch.navidrome")

#: Concurrent album-track fetches. Not user-configurable: Navidrome sits on
#: the local network and is never the bottleneck, so exposing a knob here
#: would promise a speed-up the rate-limited Deezer half cannot deliver.
TRACK_FETCH_WORKERS = 4

API_VERSION = "1.16.1"
CLIENT_NAME = "dropwatch"

# Subsonic error codes that mean "credentials", not "network".
_AUTH_ERROR_CODES = {40, 41, 42, 43, 44, 50}


class NavidromeClient:
    def __init__(self, config: Config, http: HttpClient) -> None:
        self.config = config
        self.http = http
        self._server_name = "unknown"

    # --- request plumbing --------------------------------------------------

    def _build_url(self, endpoint: str, params: dict[str, Any] | None = None) -> str:
        salt = _secrets.token_hex(8)
        token = hashlib.md5(
            (self.config.navidrome_password.reveal() + salt).encode("utf-8")
        ).hexdigest()
        query = {
            "u": self.config.navidrome_username,
            "t": token,
            "s": salt,
            "v": API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }
        query.update({k: v for k, v in (params or {}).items() if v is not None})
        return f"{self.config.rest_base}/{endpoint}?{urlencode(query)}"

    def _call(self, endpoint: str, params: dict[str, Any] | None = None) -> dict:
        response = self.http.get(self._build_url(endpoint, params))

        if response.status == 404:
            raise BadPathError(
                f"Navidrome returned 404 for {endpoint}.",
                hint=(
                    f"The server is reachable but {self.config.rest_base} is not the "
                    "Subsonic API path. Check the url setting — it should be the base "
                    "URL only, e.g. http://your-server:4533"
                ),
            )
        if response.status in (401, 403):
            raise NavidromeAuthError(
                "Navidrome rejected the credentials (HTTP "
                f"{response.status}).",
                hint="Check `dropwatch config`, or re-run `dropwatch setup`.",
            )
        if response.status >= 500:
            raise UnexpectedResponseError(
                f"Navidrome returned HTTP {response.status} for {endpoint}."
            )

        body_head = response.body[:200].lstrip().lower()
        if body_head.startswith((b"<!doctype", b"<html")):
            raise NotSubsonicError(
                f"{self.config.navidrome_url} returned an HTML page, not the Subsonic API.",
                hint=(
                    "This is usually the Navidrome web UI or a reverse proxy. Set "
                    "the url setting to the base URL Navidrome is served from."
                ),
            )

        try:
            payload = response.json()
        except ValueError:
            raise NotSubsonicError(
                f"{self.config.navidrome_url} did not return JSON.",
                hint="Confirm the URL points at a Navidrome or Subsonic-compatible server.",
            ) from None

        if not isinstance(payload, dict) or "subsonic-response" not in payload:
            raise NotSubsonicError(
                f"{self.config.navidrome_url} answered, but it is not a "
                "Subsonic-compatible API.",
                hint="Check that the url setting points at Navidrome, not another service.",
            )

        body = payload["subsonic-response"]
        if body.get("status") == "failed":
            error = body.get("error") or {}
            code = error.get("code")
            message = error.get("message", "unknown error")
            if code in _AUTH_ERROR_CODES:
                raise NavidromeAuthError(
                    f"Navidrome rejected the credentials: {message}",
                    hint="Check `dropwatch config`, or re-run `dropwatch setup`.",
                )
            raise UnexpectedResponseError(
                f"Navidrome returned an error for {endpoint}: {message} (code {code})"
            )
        return body

    # --- connectivity ------------------------------------------------------

    def ping(self) -> str:
        """Validate connectivity, credentials and protocol. Returns a label."""
        body = self._call("ping.view")
        server_type = body.get("type", "subsonic")
        version = body.get("serverVersion", body.get("version", "?"))
        self._server_name = f"{server_type} {version}"
        log.info("Connected to %s", self._server_name)
        return self._server_name

    # --- library reads -----------------------------------------------------

    def get_artists(self) -> list[LocalArtist]:
        """All album artists (ID3 view), flattened from the index buckets."""
        body = self._call("getArtists.view")
        container = body.get("artists") or {}
        artists: list[LocalArtist] = []
        for index in container.get("index", []) or []:
            for entry in index.get("artist", []) or []:
                artists.append(
                    LocalArtist(
                        id=str(entry.get("id", "")),
                        name=entry.get("name", "") or "",
                        album_count=int(entry.get("albumCount") or 0),
                    )
                )
        log.info("Navidrome reports %d album artists", len(artists))
        return artists

    def get_favorite_artists(self) -> list[LocalArtist]:
        """Artists you have starred, or who made something you have starred.

        Read rather than asked for: the library already records what matters to
        you, so a "favourites only" scan uses that rather than asking you to
        maintain a second copy of it.

        Starring an album or a song does not star its artist in Navidrome, but
        it does say the artist matters, so all three kinds contribute.

        Contributors come from the structured `artists` array, not the display
        name: that joins collaborators with a bullet — "Keith Urban • Michael
        McDonald" — which is nobody's artist. The array splits them into real
        entries with their own ids, so starring one collaboration favourites
        both artists.
        """
        body = self._call("getStarred2.view")
        container = body.get("starred2") or {}

        found: dict[str, LocalArtist] = {}
        counts: dict[str, int] = {}

        def remember(kind: str, artist_id: str, name: str) -> None:
            artist_id, name = str(artist_id or ""), (name or "").strip()
            if not artist_id and not name:
                return
            key = artist_id or name.casefold()
            if key not in found:
                found[key] = LocalArtist(id=artist_id, name=name)
                counts[kind] = counts.get(kind, 0) + 1

        for entry in container.get("artist") or []:
            remember("artist", entry.get("id"), entry.get("name"))

        for kind in ("album", "song"):
            for entry in container.get(kind) or []:
                contributors = entry.get("artists") or []
                if contributors:
                    for who in contributors:
                        remember(kind, who.get("id"), who.get("name"))
                else:  # older servers send only the flat pair
                    remember(kind, entry.get("artistId"), entry.get("artist"))

        log.info(
            "Navidrome favourites: %d artist(s) — %s",
            len(found),
            ", ".join(f"{n} via starred {k}s" for k, n in sorted(counts.items())) or "none",
        )
        return list(found.values())

    def get_artist_albums(self, artist_id: str) -> list[LocalAlbum]:
        body = self._call("getArtist.view", {"id": artist_id})
        artist = body.get("artist") or {}
        return [_album_from_json(a) for a in (artist.get("album") or [])]

    def get_artist_albums_many(
        self, artist_ids: list[str], workers: int = TRACK_FETCH_WORKERS
    ) -> dict[str, list[LocalAlbum]]:
        """`getArtist` for several artists at once, skipping the ones that fail.

        Asked per artist rather than for the library because this is the only
        endpoint that reports *participations* — albums the artist appears on
        without heading. One artist failing must not abort the run, so it is
        logged and left out.
        """
        found: dict[str, list[LocalAlbum]] = {}

        def fetch(artist_id: str) -> tuple[str, list[LocalAlbum] | None]:
            try:
                return artist_id, self.get_artist_albums(artist_id)
            except Exception as exc:  # noqa: BLE001 - one artist must not stop the scan
                log.warning("Could not read albums for artist %s: %s", artist_id, exc)
                return artist_id, None

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for artist_id, albums in pool.map(fetch, artist_ids):
                if albums is not None:
                    found[artist_id] = albums
        return found

    def get_all_albums(self, page_size: int = 500) -> list[LocalAlbum]:
        """Every album in the library, paginated.

        One request per 500 albums, rather than one per artist, which matters
        for a library with hundreds of artists on the far end of a tailnet.
        """
        albums: list[LocalAlbum] = []
        offset = 0
        while True:
            body = self._call(
                "getAlbumList2.view",
                {"type": "alphabeticalByName", "size": page_size, "offset": offset},
            )
            page = (body.get("albumList2") or {}).get("album") or []
            albums.extend(_album_from_json(a) for a in page)
            if len(page) < page_size:
                break
            offset += page_size
            if offset > 100_000:  # pragma: no cover - runaway guard
                log.warning("Stopping album pagination at %d albums", offset)
                break
        log.info("Navidrome reports %d albums", len(albums))
        return albums

    def get_album_tracks(self, album_id: str) -> list[LocalTrack]:
        body = self._call("getAlbum.view", {"id": album_id})
        album = body.get("album") or {}
        return [_track_from_json(s) for s in (album.get("song") or [])]

    def load_tracks(self, albums: list[LocalAlbum], workers: int = TRACK_FETCH_WORKERS) -> int:
        """Populate `tracks` for albums that lack them. Returns failure count.

        A failure on one album is logged and skipped so the rest of the scan
        continues; the caller reports the run as partial.
        """
        pending = [a for a in albums if not a.tracks_loaded]
        if not pending:
            return 0

        failures = 0

        def fetch(album: LocalAlbum) -> tuple[LocalAlbum, list[LocalTrack] | None]:
            try:
                return album, self.get_album_tracks(album.id)
            except Exception as exc:  # noqa: BLE001 - one album must not stop the scan
                log.warning("Could not read tracks for album %r: %s", album.name, exc)
                return album, None

        max_workers = max(1, workers)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for album, tracks in pool.map(fetch, pending):
                if tracks is None:
                    failures += 1
                    continue
                album.tracks = tracks
                album.tracks_loaded = True
        return failures


def _credit_names(*arrays: object) -> tuple[str, ...]:
    """Names from OpenSubsonic `artists`/`albumArtists` arrays, deduplicated.

    Servers predating the extension send neither, so an empty result means
    "not told", never "nobody" — callers keep the flat display string too.
    """
    names: list[str] = []
    for array in arrays:
        for entry in array or ():
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _album_from_json(data: dict) -> LocalAlbum:
    return LocalAlbum(
        id=str(data.get("id", "")),
        name=data.get("name") or data.get("album") or "",
        artist=data.get("artist") or "",
        artist_id=str(data.get("artistId", "") or ""),
        year=int(data["year"]) if str(data.get("year") or "").isdigit() else None,
        song_count=int(data.get("songCount") or 0),
        duration=int(data.get("duration") or 0) or None,
        artists=_credit_names(data.get("artists"), data.get("albumArtists")),
    )


def _track_from_json(data: dict) -> LocalTrack:
    return LocalTrack(
        title=data.get("title") or "",
        duration=int(data.get("duration") or 0) or None,
        track=int(data["track"]) if str(data.get("track") or "").isdigit() else None,
        disc=int(data["discNumber"]) if str(data.get("discNumber") or "").isdigit() else None,
        artist=data.get("artist") or "",
        year=int(data["year"]) if str(data.get("year") or "").isdigit() else None,
        artists=_credit_names(data.get("artists"), data.get("albumArtists")),
        album_artist=data.get("albumArtist") or "",
    )


def iter_all_albums(client: NavidromeClient, artists: list[LocalArtist]) -> Iterator[LocalAlbum]:
    for artist in artists:
        yield from artist.albums
