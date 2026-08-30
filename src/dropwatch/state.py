"""Local state: response cache, artist mappings and block lists.

SQLite rather than JSON because the cache is written from several threads and
a partially written JSON file loses everything, while SQLite gives atomic
commits for free from the standard library.

No credentials are ever written here.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import DECISIONS, DECISION_OWNED, DatePrecision, ReleaseDate
from .normalize import artist_key

log = logging.getLogger("dropwatch.state")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS artist_mapping (
    local_key   TEXT PRIMARY KEY,
    local_name  TEXT NOT NULL,
    deezer_id   TEXT,
    deezer_name TEXT,
    status      TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
-- One local artist may map to several Deezer IDs: the catalogue routinely
-- splits an act across duplicate artist entries with partial discographies.
CREATE TABLE IF NOT EXISTS artist_mapping_target (
    local_key   TEXT NOT NULL,
    deezer_id   TEXT NOT NULL,
    deezer_name TEXT,
    position    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (local_key, deezer_id)
);
-- A decision the user made about one ambiguous release, so the review
-- section does not ask the same question on every run.
CREATE TABLE IF NOT EXISTS release_decision (
    deezer_id  TEXT PRIMARY KEY,
    decision   TEXT NOT NULL,
    note       TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS local_tracks (
    album_id    TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    payload     TEXT NOT NULL,
    fetched_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: Payloads whose content is fixed once published. A released album's metadata
#: and track list do not change, so expiring them on the same clock as a
#: discography listing — which exists precisely to change — re-fetches
#: thousands of immutable records for nothing. Bounded rather than infinite so
#: catalogue corrections land eventually; `--refresh` forces it sooner.
STABLE_KEY_PREFIXES = ("album:", "album_tracks:")
STABLE_MAX_AGE_HOURS = 24 * 30

#: Meta key holding the scope the last scan ran with, so `rip` can replay it.
#: Lives here rather than in `scan` so the walk can read it without importing
#: the scanner, which would drag in both API clients.
LAST_SCAN_SCOPE = "last_scan_scope"

#: Meta keys the schema owns. `set_meta` refuses them: clearing
#: `schema_version` would silently reset the store to v1 and re-run every
#: migration on the next open, which no caller could plausibly intend.
RESERVED_META_KEYS = frozenset({"schema_version"})

STATUS_CONFIRMED = "confirmed"
#: The stored value stays "ignored": it predates the command being named
#: `block`, and rewriting it would invalidate every existing state file.
STATUS_BLOCKED = "ignored"


SCHEMA_VERSION = 4


@dataclass(frozen=True)
class ScanScope:
    """What the last scan actually covered, so `rip` can replay it.

    One record rather than a setting per axis, because the rule is one rule:
    the walk offers what the last scan reported. A scan writes the whole record
    every time, so a filter the user dropped is cleared by the same write that
    stores the ones they kept — "no cutoff" is a state the walk reads, never
    the mere absence of one.

    Three of the four axes are predicates over a stored entry's own fields and
    need nothing else recorded here. Favourite-ness is the exception: it is not
    derivable from an entry, and `rip` must never contact Navidrome to ask, so
    the scan records the favourite artists it resolved to and the walk replays
    that list — the same mechanism `artists` already uses.

    An earlier design stamped a boolean on each stored entry instead. It went
    stale: once an artist stopped being a favourite no favourites scan covered
    them again, so nothing could ever purge the entry, and the walk kept
    offering releases the scan no longer reported. A name list cannot go stale
    because the whole record is rewritten on every scan.
    """

    favorites: bool = False
    since: ReleaseDate | None = None
    types: frozenset[str] = frozenset()
    artists: tuple[str, ...] = ()
    #: The favourite artists as the scan resolved them, captured before
    #: `--artist` and `--limit` narrow the run: this describes who was a
    #: favourite, not how much work the scan chose to do. `None` means no scan
    #: has recorded them yet, which is not the same as a scan that found none
    #: — the first narrows nothing, the second narrows to nothing.
    favorite_artists: tuple[str, ...] | None = None

    def to_json(self) -> dict:
        return {
            "favorites": self.favorites,
            "since": str(self.since) if self.since is not None else None,
            "types": sorted(self.types),
            "artists": list(self.artists),
            "favorite_artists": (
                None if self.favorite_artists is None else list(self.favorite_artists)
            ),
        }

    @classmethod
    def from_json(cls, raw: object) -> ScanScope:
        """Rebuild a stored scope, ignoring anything unreadable.

        Every field falls back to "not narrowing". Walking a shorter list than
        the user asked for is worse than ignoring a corrupt record: the first
        silently hides releases, the second only offers a few extra.
        """
        if not isinstance(raw, dict):
            return cls()
        since = ReleaseDate.parse(raw.get("since"))
        types = raw.get("types")
        artists = raw.get("artists")
        favorite_artists = raw.get("favorite_artists")
        return cls(
            favorites=bool(raw.get("favorites")),
            since=None if since.precision is DatePrecision.UNKNOWN else since,
            types=(
                frozenset(str(t) for t in types) if isinstance(types, list) else frozenset()
            ),
            artists=tuple(str(a) for a in artists) if isinstance(artists, list) else (),
            # A record written before the list existed has no names to replay,
            # and stays None so it narrows nothing: that self-corrects on the
            # next scan and errs toward offering a few extra releases rather
            # than hiding them. An empty list is different — a scan that
            # resolved to no favourites at all — and narrows to nothing.
            favorite_artists=(
                tuple(str(a) for a in favorite_artists)
                if isinstance(favorite_artists, list)
                else None
            ),
        )


@dataclass(frozen=True)
class MappingTarget:
    deezer_id: str
    deezer_name: str | None = None


@dataclass
class ArtistMapping:
    local_key: str
    local_name: str
    targets: list[MappingTarget]
    status: str
    updated_at: float

    @property
    def is_blocked(self) -> bool:
        return self.status == STATUS_BLOCKED

    @property
    def deezer_ids(self) -> list[str]:
        return [t.deezer_id for t in self.targets]

    def describe(self) -> str:
        if self.is_blocked:
            return "(blocked)"
        if not self.targets:
            return "(no Deezer artist)"
        return ", ".join(
            f"{t.deezer_name or '?'} [{t.deezer_id}]" for t in self.targets
        )


class Store:
    """Thread-safe wrapper around the SQLite state file."""

    def __init__(self, path: Path, max_age_hours: float = 24.0) -> None:
        self.path = path
        self.max_age_seconds = max_age_hours * 3600.0
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._migrate()
        if new_file:
            # No secrets live here, but the file does describe the library.
            try:
                os.chmod(path, 0o600)
            except OSError:  # pragma: no cover - platform dependent
                pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- generic cache -----------------------------------------------------

    def max_age_for(self, key: str) -> float:
        """Expiry for one key, in seconds. 0 disables expiry entirely."""
        if self.max_age_seconds <= 0:
            return 0.0
        if key.startswith(STABLE_KEY_PREFIXES):
            return max(self.max_age_seconds, STABLE_MAX_AGE_HOURS * 3600.0)
        return self.max_age_seconds

    def get(self, key: str) -> Any | None:
        """Return a cached payload, or None when absent or stale."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, fetched_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        max_age = self.max_age_for(key)
        if max_age > 0 and time.time() - row["fetched_at"] > max_age:
            return None
        try:
            return json.loads(row["payload"])
        except ValueError:
            return None

    def set(self, key: str, payload: Any) -> None:
        encoded = json.dumps(payload, separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                "INSERT INTO cache(key, payload, fetched_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
                "fetched_at=excluded.fetched_at",
                (key, encoded, time.time()),
            )
            self._conn.commit()

    def clear_cache(self) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM cache")
            self._conn.execute("DELETE FROM local_tracks")
            self._conn.commit()
            return cursor.rowcount

    def cache_stats(self) -> dict[str, int]:
        """Entry counts split by expiry class, for the `cache` command."""
        with self._lock:
            rows = self._conn.execute("SELECT key FROM cache").fetchall()
        stable = sum(1 for r in rows if r["key"].startswith(STABLE_KEY_PREFIXES))
        return {"total": len(rows), "stable": stable, "volatile": len(rows) - stable}

    def cache_age_hours(self) -> float | None:
        with self._lock:
            row = self._conn.execute("SELECT MIN(fetched_at) AS oldest FROM cache").fetchone()
        if not row or row["oldest"] is None:
            return None
        return (time.time() - row["oldest"]) / 3600.0

    # --- local track cache -------------------------------------------------

    def get_local_tracks(self, album_id: str, fingerprint: str) -> list[dict] | None:
        """Cached tracks, invalidated when the album's fingerprint changes."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, fingerprint FROM local_tracks WHERE album_id = ?",
                (album_id,),
            ).fetchone()
        if row is None or row["fingerprint"] != fingerprint:
            return None
        try:
            return json.loads(row["payload"])
        except ValueError:
            return None

    def set_local_tracks(self, album_id: str, fingerprint: str, tracks: list[dict]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO local_tracks(album_id, fingerprint, payload, fetched_at) "
                "VALUES(?,?,?,?) ON CONFLICT(album_id) DO UPDATE SET "
                "fingerprint=excluded.fingerprint, payload=excluded.payload, "
                "fetched_at=excluded.fetched_at",
                (album_id, fingerprint, json.dumps(tracks, separators=(",", ":")), time.time()),
            )
            self._conn.commit()

    # --- schema migration --------------------------------------------------

    def _migrate(self) -> None:
        """Bring an older state file up to the current schema.

        v1 stored a single Deezer id per artist directly on `artist_mapping`.
        Those rows are copied into the target table; the old columns are left
        in place so a downgrade does not lose data.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(row["value"]) if row else 1

            if current < 4:
                # v3 cached local tracks without their credit fields, and the
                # cache key is the album fingerprint, which a metadata-only
                # change does not move. Dropping the cache is free — it is
                # rebuilt from Navidrome on the next scan — and is the only way
                # those rows ever gain the fields the singles rule reads.
                self._conn.execute("DELETE FROM local_tracks")

            if current < 3:
                # v2 had `ignored_release`, which could only express "owned".
                have = self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='ignored_release'"
                ).fetchone()
                if have:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO release_decision"
                        "(deezer_id, decision, note, created_at) "
                        "SELECT deezer_id, ?, note, created_at FROM ignored_release",
                        (DECISION_OWNED,),
                    )

            if current < 2:
                self._conn.execute(
                    "INSERT OR IGNORE INTO artist_mapping_target"
                    "(local_key, deezer_id, deezer_name, position) "
                    "SELECT local_key, deezer_id, deezer_name, 0 FROM artist_mapping "
                    "WHERE deezer_id IS NOT NULL AND deezer_id != ''"
                )
                log.debug("Migrated artist mappings to the multi-target schema")

            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    # --- scan metadata -----------------------------------------------------

    def set_meta(self, key: str, value: str | None) -> None:
        """Record one fact about the last scan. `None` clears it.

        The `meta` table also holds the schema version, so the keys the schema
        owns are refused outright rather than merely left alone by convention:
        a stray clear there would reset the store to v1 and re-run every
        migration on the next open.
        """
        if key in RESERVED_META_KEYS:
            raise ValueError(f"{key!r} belongs to the schema and cannot be set here")
        with self._lock:
            if value is None:
                self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def save_scan_scope(self, scope: ScanScope) -> None:
        """Record what the scan covered. Written whole, so nothing lingers."""
        self.set_meta(LAST_SCAN_SCOPE, json.dumps(scope.to_json()))

    def load_scan_scope(self) -> ScanScope:
        """The last scan's scope, or an unnarrowed one if there is none."""
        raw = self.get_meta(LAST_SCAN_SCOPE)
        if not raw:
            return ScanScope()
        try:
            return ScanScope.from_json(json.loads(raw))
        except ValueError:
            return ScanScope()

    # --- artist mappings ---------------------------------------------------

    def _targets_for(self, local_key: str) -> list[MappingTarget]:
        rows = self._conn.execute(
            "SELECT deezer_id, deezer_name FROM artist_mapping_target "
            "WHERE local_key = ? ORDER BY position, deezer_id",
            (local_key,),
        ).fetchall()
        return [MappingTarget(r["deezer_id"], r["deezer_name"]) for r in rows]

    def get_mapping(self, local_name: str) -> ArtistMapping | None:
        key = artist_key(local_name)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artist_mapping WHERE local_key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            targets = self._targets_for(key)
        return ArtistMapping(
            local_key=row["local_key"],
            local_name=row["local_name"],
            targets=targets,
            status=row["status"],
            updated_at=row["updated_at"],
        )

    def set_mapping(
        self,
        local_name: str,
        targets: list[MappingTarget] | None = None,
        status: str = STATUS_CONFIRMED,
    ) -> None:
        """Replace an artist's mapping outright.

        Passing an empty target list with the confirmed status is meaningless,
        so callers wanting to forget an artist should use `clear_mapping`.
        """
        key = artist_key(local_name)
        targets = list(targets or [])
        with self._lock:
            self._conn.execute(
                "INSERT INTO artist_mapping"
                "(local_key, local_name, deezer_id, deezer_name, status, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(local_key) DO UPDATE SET "
                "local_name=excluded.local_name, deezer_id=excluded.deezer_id, "
                "deezer_name=excluded.deezer_name, status=excluded.status, "
                "updated_at=excluded.updated_at",
                (
                    key,
                    local_name,
                    targets[0].deezer_id if targets else None,
                    targets[0].deezer_name if targets else None,
                    status,
                    time.time(),
                ),
            )
            self._conn.execute(
                "DELETE FROM artist_mapping_target WHERE local_key = ?", (key,)
            )
            for position, target in enumerate(targets):
                self._conn.execute(
                    "INSERT INTO artist_mapping_target"
                    "(local_key, deezer_id, deezer_name, position) VALUES(?,?,?,?)",
                    (key, target.deezer_id, target.deezer_name, position),
                )
            self._conn.commit()

    def block_artist(self, local_name: str) -> None:
        """Suppress an artist, keeping any Deezer ids already mapped to it.

        The scan checks the status before it looks at targets, so keeping them
        changes nothing there — but it means a Deezer id still names this
        artist afterwards, so `block` and `unblock` round-trip on the same id.

        The stored status stays the string "ignored": it predates the command
        being named `block`, and rewriting it would invalidate existing state
        files to no purpose.
        """
        existing = self.get_mapping(local_name)
        targets = existing.targets if existing else []
        self.set_mapping(local_name, targets, status=STATUS_BLOCKED)

    def clear_mapping(self, local_name: str) -> bool:
        """Forget everything about an artist, returning it to unresolved.

        Clears every mapped Deezer id and any block flag, so the next scan
        resolves the artist from scratch.
        """
        key = artist_key(local_name)
        with self._lock:
            self._conn.execute(
                "DELETE FROM artist_mapping_target WHERE local_key = ?", (key,)
            )
            cursor = self._conn.execute(
                "DELETE FROM artist_mapping WHERE local_key = ?", (key,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # Retained under the old name; `unmap` on the command line calls this.
    delete_mapping = clear_mapping

    def list_mappings(self) -> list[ArtistMapping]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM artist_mapping ORDER BY local_name COLLATE NOCASE"
            ).fetchall()
            return [
                ArtistMapping(
                    local_key=r["local_key"],
                    local_name=r["local_name"],
                    targets=self._targets_for(r["local_key"]),
                    status=r["status"],
                    updated_at=r["updated_at"],
                )
                for r in rows
            ]

    def mappings_for_deezer_id(self, deezer_id: str) -> list[ArtistMapping]:
        """Local artists mapped to this Deezer artist.

        The reverse of the usual lookup, so a Deezer URL copied out of the
        results can name an artist the same way a local name does. More than
        one local artist may point at the same Deezer artist.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.* FROM artist_mapping m "
                "JOIN artist_mapping_target t ON t.local_key = m.local_key "
                "WHERE t.deezer_id = ? ORDER BY m.local_name COLLATE NOCASE",
                (str(deezer_id),),
            ).fetchall()
            return [
                ArtistMapping(
                    local_key=r["local_key"],
                    local_name=r["local_name"],
                    targets=self._targets_for(r["local_key"]),
                    status=r["status"],
                    updated_at=r["updated_at"],
                )
                for r in rows
            ]

    def reset_mappings(self) -> int:
        with self._lock:
            self._conn.execute("DELETE FROM artist_mapping_target")
            cursor = self._conn.execute("DELETE FROM artist_mapping")
            self._conn.commit()
            return cursor.rowcount

    # --- ignored releases --------------------------------------------------

    def set_release_decision(self, deezer_id: str, decision: str, note: str = "") -> None:
        """Record what the user said about an ambiguous release."""
        if decision not in DECISIONS:
            raise ValueError(f"unknown decision {decision!r}")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO release_decision"
                "(deezer_id, decision, note, created_at) VALUES(?,?,?,?)",
                (str(deezer_id), decision, note, time.time()),
            )
            self._conn.commit()

    def clear_release_decision(self, deezer_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM release_decision WHERE deezer_id = ?", (str(deezer_id),)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def release_decisions(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT deezer_id, decision FROM release_decision"
            ).fetchall()
        return {r["deezer_id"]: r["decision"] for r in rows}

    def count_release_decisions(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) c FROM release_decision").fetchone()
        return int(row["c"])

    def reset_release_decisions(self) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM release_decision")
            self._conn.commit()
            return cursor.rowcount

    # --- review queue ------------------------------------------------------

    def save_review(self, entries: list[dict]) -> None:
        self.set("__review__", entries)

    def load_review(self) -> list[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM cache WHERE key = ?", ("__review__",)
            ).fetchone()
        if row is None:
            return []
        try:
            return json.loads(row["payload"])
        except ValueError:
            return []

    # --- missing releases (persisted for the `rip` command) ----------------

    def save_missing(self, entries: list[dict]) -> None:
        self.set("__missing__", entries)

    def load_missing(self) -> list[dict]:
        # Like the other report queues, this is what the last scan concluded
        # rather than a cache entry, so it does not expire: ripping the day
        # after a scan still works.
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM cache WHERE key = ?", ("__missing__",)
            ).fetchone()
        if row is None:
            return []
        try:
            return json.loads(row["payload"])
        except ValueError:
            return []

    # --- unresolved artists (persisted for the `artists` command) ----------

    def save_unresolved(self, entries: list[dict]) -> None:
        self.set("__unresolved__", entries)

    def load_unresolved(self) -> list[dict]:
        # Deliberately bypasses expiry: this is a report, not a cache entry.
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM cache WHERE key = ?", ("__unresolved__",)
            ).fetchone()
        if row is None:
            return []
        try:
            return json.loads(row["payload"])
        except ValueError:
            return []
