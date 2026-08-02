"""Deezer catalog provider.

Interface used: ``https://api.deezer.com`` — Deezer's **public, unauthenticated
REST API**. It is publicly documented by Deezer but not covered by any
stability guarantee, and it needs no credentials and no OAuth application.
See the README for the provenance classification.

Empirically verified behaviours this client works around:

* ``/album/{id}`` embeds at most 25 tracks and reports ``tracks.next: null``
  even when the album has more, so the track list is fetched from the
  dedicated ``/album/{id}/tracks`` endpoint, which paginates correctly and
  additionally returns ``isrc``, ``disk_number`` and ``track_position``.
* Application errors arrive with HTTP 200 and an ``error`` object in the body,
  so HTTP status alone is not a success signal.
* Roughly 50 requests per 5 seconds per IP are allowed; exceeding that returns
  ``{"error": {"code": 4, "message": "Quota limit exceeded"}}``.

Everything unofficial is confined to this module: the matcher and reporter
only see the models in `models.py`, so swapping this provider out would not
touch them.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable
from urllib.parse import quote

from .errors import DeezerError, DeezerQuotaError, DeezerUnavailableError, DropWatchError
from .http import HttpClient, RateLimiter
from .models import DeezerArtist, DeezerRelease, DeezerTrack, ReleaseDate

log = logging.getLogger("dropwatch.deezer")

API_BASE = "https://api.deezer.com"
PAGE_SIZE = 100

# Conservative against the observed ~50 req / 5 s ceiling.
DEFAULT_RATE = 8.0
DEFAULT_BURST = 16

MAX_RETRIES = 3
RETRYABLE_CODES = {4, 500, 700}
NOT_FOUND_CODES = {800}
AUTH_CODES = {200, 300}


def make_rate_limiter() -> RateLimiter:
    return RateLimiter(DEFAULT_RATE, DEFAULT_BURST)


class DeezerProvider:
    """Read-only catalog access. Never downloads, decrypts or writes anything."""

    def __init__(
        self,
        http: HttpClient,
        cache: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.http = http
        self.cache = cache
        self._sleep = sleep

    # --- transport ---------------------------------------------------------

    def _get(self, path: str, cache_key: str | None = None) -> dict:
        """GET a Deezer path with caching, bounded retries and error mapping."""
        if cache_key and self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        payload = self._get_uncached(path)

        if cache_key and self.cache is not None:
            self.cache.set(cache_key, payload)
        return payload

    def _get_uncached(self, path: str) -> dict:
        url = f"{API_BASE}{path}"
        delay = 1.0
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.http.get(url)
            except DropWatchError as exc:
                # Transport failure: retry, it may be a blip.
                last_error = DeezerUnavailableError(f"Deezer request failed: {exc}")
                log.debug("Deezer transport error (attempt %d/%d): %s", attempt, MAX_RETRIES, exc)
            else:
                if response.status >= 500:
                    last_error = DeezerUnavailableError(
                        f"Deezer returned HTTP {response.status}"
                    )
                elif response.status == 429:
                    last_error = DeezerQuotaError("Deezer rate limit hit (HTTP 429)")
                else:
                    try:
                        payload = response.json()
                    except ValueError:
                        last_error = DeezerUnavailableError(
                            "Deezer returned a non-JSON response"
                        )
                    else:
                        error = payload.get("error") if isinstance(payload, dict) else None
                        if not error:
                            return payload
                        code, message = _error_details(error)
                        if code in NOT_FOUND_CODES:
                            raise DeezerNotFound(message)
                        if code in AUTH_CODES:
                            # Never retried: re-sending rejected credentials
                            # cannot succeed and risks locking the account.
                            raise DeezerError(f"Deezer refused the request: {message}")
                        if code in RETRYABLE_CODES:
                            last_error = DeezerQuotaError(f"Deezer: {message}")
                        else:
                            raise DeezerError(f"Deezer error {code}: {message}")

            if attempt < MAX_RETRIES:
                # Jitter avoids re-synchronising after a shared quota window.
                self._sleep(delay + random.uniform(0, 0.4))
                delay *= 2

        raise last_error or DeezerUnavailableError("Deezer request failed")

    def _paginate(self, path: str, cache_key: str | None = None) -> list[dict]:
        """Follow Deezer's ``next`` links, guarding against runaway loops."""
        if cache_key and self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        items: list[dict] = []
        separator = "&" if "?" in path else "?"
        current = f"{path}{separator}limit={PAGE_SIZE}&index=0"
        seen_indexes: set[str] = set()

        while current:
            payload = self._get(current)
            data = payload.get("data")
            if not isinstance(data, list):
                raise DeezerError(f"Unexpected Deezer response shape for {path}")
            items.extend(data)

            next_url = payload.get("next")
            if not next_url:
                break
            current = next_url.replace(API_BASE, "", 1) if next_url.startswith(API_BASE) else next_url
            if current in seen_indexes:
                log.warning("Deezer pagination loop detected for %s", path)
                break
            seen_indexes.add(current)
            if len(items) > 5000:
                log.warning("Stopping pagination for %s after 5000 items", path)
                break

        if cache_key and self.cache is not None:
            self.cache.set(cache_key, items)
        return items

    # --- catalog -----------------------------------------------------------

    def search_artists(self, name: str, limit: int = 25) -> list[DeezerArtist]:
        query = quote(name, safe="")
        payload = self._get(
            f"/search/artist?q={query}&limit={limit}",
            cache_key=f"search:artist:{name.casefold()}:{limit}",
        )
        return [
            _artist_from_json(a) for a in payload.get("data", []) if a.get("id") is not None
        ]

    def get_artist(self, artist_id: str) -> DeezerArtist | None:
        try:
            payload = self._get(f"/artist/{artist_id}", cache_key=f"artist:{artist_id}")
        except DeezerNotFound:
            return None
        return _artist_from_json(payload)

    def get_discography(self, artist_id: str) -> list[DeezerRelease]:
        """Every release Deezer lists for the artist, across all pages."""
        rows = self._paginate(
            f"/artist/{artist_id}/albums", cache_key=f"discography:{artist_id}"
        )
        artist_name = ""
        releases = []
        for row in rows:
            # `not row["id"]` would discard a legitimate id of 0.
            if row.get("id") is None:
                continue
            releases.append(_release_from_json(row, artist_id, artist_name))
        return releases

    def album_summary(self, release_id: str) -> dict | None:
        """Album metadata for display only. Served from cache when present."""
        try:
            return self._get(f"/album/{release_id}", cache_key=f"album:{release_id}")
        except DeezerNotFound:
            return None

    def load_release_detail(self, release: DeezerRelease) -> DeezerRelease:
        """Fill in UPC, label, contributors and the complete track list.

        Two requests, because the album endpoint's embedded track list is both
        truncated at 25 and missing ISRCs.
        """
        if release.detail_loaded:
            return release

        try:
            detail = self._get(f"/album/{release.id}", cache_key=f"album:{release.id}")
        except DeezerNotFound:
            release.detail_loaded = True
            return release

        release.upc = detail.get("upc") or None
        release.label = detail.get("label") or None
        release.nb_tracks = _as_int(detail.get("nb_tracks"))
        release.duration = _as_int(detail.get("duration"))
        release.contributors = [
            c.get("name", "") for c in detail.get("contributors", []) if c.get("name")
        ]
        if detail.get("record_type"):
            release.record_type = detail["record_type"]
        if detail.get("release_date"):
            parsed = ReleaseDate.parse(detail["release_date"])
            if parsed.precision >= release.release_date.precision:
                release.release_date = parsed

        try:
            rows = self._paginate(
                f"/album/{release.id}/tracks", cache_key=f"album_tracks:{release.id}"
            )
        except DeezerNotFound:
            rows = []
        release.tracks = [_track_from_json(t) for t in rows if t.get("id") is not None]

        if release.tracks:
            # Prefer counts derived from the full list over the summary field,
            # which can disagree with what the catalogue actually exposes.
            release.nb_tracks = len(release.tracks)
            total = sum(t.duration or 0 for t in release.tracks)
            release.duration = total or release.duration

        release.detail_loaded = True
        return release


class DeezerNotFound(DeezerError):
    """Deezer has no data for the requested id (error code 800)."""


def _error_details(error: Any) -> tuple[int | None, str]:
    if isinstance(error, dict):
        raw = error.get("code")
        try:
            code = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            code = None
        return code, str(error.get("message") or error.get("type") or "unknown error")
    return None, str(error)


def _as_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result or None


def _artist_from_json(data: dict) -> DeezerArtist:
    return DeezerArtist(
        id=str(data.get("id", "")),
        name=data.get("name", "") or "",
        nb_album=int(data.get("nb_album") or 0),
        nb_fan=int(data.get("nb_fan") or 0),
        link=data.get("link", "") or "",
    )


def _release_from_json(data: dict, artist_id: str, artist_name: str) -> DeezerRelease:
    return DeezerRelease(
        id=str(data.get("id", "")),
        title=data.get("title", "") or "",
        artist_id=str(data.get("artist", {}).get("id") or artist_id),
        artist_name=data.get("artist", {}).get("name") or artist_name,
        record_type=(data.get("record_type") or "").lower(),
        release_date=ReleaseDate.parse(data.get("release_date")),
        explicit=bool(data.get("explicit_lyrics")),
        fans=int(data.get("fans") or 0),
        link=data.get("link", "") or "",
        nb_tracks=_as_int(data.get("nb_tracks")),
    )


def _track_from_json(data: dict) -> DeezerTrack:
    return DeezerTrack(
        id=str(data.get("id", "")),
        title=data.get("title", "") or "",
        title_short=data.get("title_short", "") or "",
        title_version=data.get("title_version", "") or "",
        duration=_as_int(data.get("duration")),
        isrc=(data.get("isrc") or None),
        disc=_as_int(data.get("disk_number")),
        position=_as_int(data.get("track_position")),
        artist=(data.get("artist") or {}).get("name", "") or "",
    )
