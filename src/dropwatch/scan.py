"""Scan orchestration: library in, missing releases out.

Request volume is the main design constraint, so work is staged: cheap
listing data first, and expensive per-release detail only for releases that
still look missing after the cheap pass. Everything fetched is cached, so a
second run costs almost nothing.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field

from .artist_match import ArtistResolver, Resolution
from .classify import resolve_type
from .config import Config
from .deezer import DeezerProvider
from .dedupe import dedupe_results, deduplicate
from .errors import ConfigError, DropWatchError
from .models import (
    DeezerRelease,
    LocalAlbum,
    LocalArtist,
    LocalTrack,
    MissingRelease,
    Ownership,
    ReleaseDate,
    ReleaseType,
    ReviewItem,
    UnresolvedArtist,
)
from .navidrome import NavidromeClient
from .normalize import fold
from .release_match import (
    IsrcIndex,
    LocalIndex,
    Verdict,
    determine_ownership,
    refine_with_isrc,
)
from .state import ScanScope, Store

log = logging.getLogger("dropwatch.scan")

#: Owned Deezer albums consulted when checking whether a single is an advance
#: single. Bounded because each one costs two extra requests.
MAX_ISRC_REFERENCE_ALBUMS = 4
ISRC_REFERENCE_YEAR_WINDOW = 2


@dataclass
class ScanOptions:
    artist_filters: list[str] = field(default_factory=list)
    limit: int | None = None
    since: ReleaseDate | None = None
    types: set[ReleaseType] | None = None
    #: Restrict to artists starred in Navidrome.
    favorites: bool = False
    progress: bool = True


def _merge_queue(
    existing: list[dict], fresh: list[dict], scanned: set[str], key: str
) -> list[dict]:
    """Refresh only the entries belonging to artists this run actually covered.

    Replacing the queue outright meant `--artist "X"` discarded every other
    artist's pending questions. A full scan covers everyone, so this is
    equivalent to a replacement in that case.
    """
    folded = {fold(name) for name in scanned}
    kept = [entry for entry in existing if fold(str(entry.get(key, ""))) not in folded]
    return kept + fresh


@dataclass
class Judged:
    """One release plus the verdict reached about it.

    Carrying the verdict alongside the release is what lets a single pass over
    the discography serve both the report and the ISRC cross-check.
    """

    release: DeezerRelease
    release_type: ReleaseType
    verdict: Verdict
    traits: tuple[str, ...]

    @classmethod
    def evaluate(
        cls, release: DeezerRelease, index: LocalIndex, decisions: dict[str, str]
    ) -> Judged:
        release_type, classification = resolve_type(release)
        return cls(
            release=release,
            release_type=release_type,
            verdict=determine_ownership(release, index, release_type, decisions),
            traits=tuple(sorted(classification.traits)),
        )

    def rejudge(self, index: LocalIndex, decisions: dict[str, str]) -> None:
        """Re-evaluate after release detail has been fetched."""
        self.release_type, classification = resolve_type(self.release)
        self.verdict = determine_ownership(
            self.release, index, self.release_type, decisions
        )
        self.traits = tuple(sorted(classification.traits))

    @property
    def is_owned(self) -> bool:
        return self.verdict.ownership in (
            Ownership.OWNED,
            Ownership.PROBABLY_OWNED,
            Ownership.IGNORED,
        )

    @property
    def is_single(self) -> bool:
        return self.release_type is ReleaseType.SINGLE


@dataclass
class ArtistOutcome:
    """What one artist contributed, for the live progress line."""

    name: str
    missing: list[MissingRelease] = field(default_factory=list)
    review: int = 0
    unresolved: UnresolvedArtist | None = None
    failed: bool = False

    @property
    def worth_reporting(self) -> bool:
        """Artists with nothing to say stay off the scroll, to keep it dense."""
        return bool(self.missing) or self.review or self.unresolved or self.failed

    def describe(self) -> str:
        if self.failed:
            return "failed"
        if self.unresolved is not None:
            count = len(self.unresolved.candidates)
            return f"unresolved{f', {count} candidates' if count else ''}"
        parts: list[str] = []
        if self.missing:
            counts: dict[ReleaseType, int] = {}
            for item in self.missing:
                counts[item.release_type] = counts.get(item.release_type, 0) + 1
            breakdown = ", ".join(
                f"{n} {_LABELS[t][0] if n == 1 else _LABELS[t][1]}"
                for t, n in sorted(counts.items(), key=lambda kv: kv[0].value)
            )
            parts.append(f"{len(self.missing)} missing ({breakdown})")
        if self.review:
            parts.append(f"{self.review} to review")
        return ", ".join(parts) or "nothing new"


_LABELS = {
    ReleaseType.ALBUM: ("album", "albums"),
    ReleaseType.EP: ("EP", "EPs"),
    ReleaseType.SINGLE: ("single", "singles"),
    ReleaseType.UNKNOWN: ("unclassified", "unclassified"),
}


@dataclass
class ScanResult:
    missing: list[MissingRelease] = field(default_factory=list)
    review: list[ReviewItem] = field(default_factory=list)
    unresolved: list[UnresolvedArtist] = field(default_factory=list)
    artists_scanned: int = 0
    partial_reasons: list[str] = field(default_factory=list)
    #: Names actually processed this run, so a filtered scan only refreshes
    #: its own share of the stored queues.
    scanned_names: set[str] = field(default_factory=set)


class Scanner:
    def __init__(
        self,
        client: NavidromeClient,
        provider: DeezerProvider,
        store: Store,
        config: Config,
    ) -> None:
        self.client = client
        self.provider = provider
        self.store = store
        self.config = config
        self.resolver = ArtistResolver(provider, store)
        #: The favourites the last `read_library` resolved to, for the scope
        #: record. Held here rather than returned because `read_library`'s
        #: list is the run's work queue, which `--limit` truncates. Stays None
        #: for a scan that never asked who the favourites were, which is not
        #: the same as a favourites scan that found none.
        self._favorite_artists: tuple[str, ...] | None = None

    # --- library -----------------------------------------------------------

    def read_library(self, options: ScanOptions) -> list[LocalArtist]:
        self._favorite_artists = None
        artists = self.client.get_artists()
        albums = self.client.get_all_albums()

        by_id: dict[str, LocalArtist] = {a.id: a for a in artists}
        by_name: dict[str, LocalArtist] = {fold(a.name): a for a in artists}

        orphans = 0
        shared = 0
        for album in albums:
            # An album belongs to every artist credited on it. Attaching it to
            # the one the display string leads with left a collaboration —
            # "Keith Urban • Michael McDonald" — owned by neither, so its songs
            # could not vouch for either artist's singles, and often matched no
            # album-artist entry at all and was dropped as an orphan.
            owners: dict[int, LocalArtist] = {}
            primary = by_id.get(album.artist_id) or by_name.get(fold(album.artist))
            if primary is not None:
                owners[id(primary)] = primary
            for name in album.artists:
                credited = by_name.get(fold(name))
                if credited is not None:
                    owners[id(credited)] = credited
            if not owners:
                orphans += 1
                continue
            if len(owners) > 1:
                shared += 1
            for artist in owners.values():
                artist.albums.append(album)
        if orphans:
            log.info("%d album(s) had no matching album artist entry", orphans)
        if shared:
            log.info("%d album(s) are credited to more than one library artist", shared)

        if options.favorites:
            artists = self._only_favorites(artists)

        if options.artist_filters:
            wanted = {fold(name) for name in options.artist_filters}
            artists = [a for a in artists if fold(a.name) in wanted]
            missing = wanted - {fold(a.name) for a in artists}
            for name in sorted(missing):
                log.warning("No artist named %r in the library", name)

        artists.sort(key=lambda a: fold(a.name))
        if options.limit:
            artists = artists[: options.limit]
        return artists

    def _only_favorites(self, artists: list[LocalArtist]) -> list[LocalArtist]:
        """Keep the album artists among your Navidrome favourites.

        Matched on id, falling back to a folded name. A favourite can have no
        album-artist entry at all — a guest on one starred track, say — and
        those have nothing here to scan.
        """
        starred = self.client.get_favorite_artists()
        if not starred:
            raise ConfigError(
                "Nothing is starred in Navidrome.",
                hint=(
                    "Favourite an artist, album or song in Navidrome, or drop "
                    "--favorites to scan the whole library."
                ),
            )

        ids = {a.id for a in starred if a.id}
        names = {fold(a.name) for a in starred if a.name}
        kept = [a for a in artists if a.id in ids or fold(a.name) in names]

        skipped = len(starred) - len(kept)
        if skipped > 0:
            log.info(
                "%d favourite(s) are not album artists and have nothing to scan",
                skipped,
            )
        log.info("Scanning %d of %d favourite artists", len(kept), len(starred))
        # Recorded here, before `--artist` and `--limit` are applied, because
        # this answers "who was a favourite when the scan ran" rather than
        # "how many of them did this run get through".
        self._favorite_artists = tuple(a.name for a in kept)
        return kept

    def load_local_tracks(self, artist: LocalArtist) -> int:
        """Fill in track lists for the artist's albums, using the cache."""
        pending: list[LocalAlbum] = []
        for album in artist.albums:
            cached = self.store.get_local_tracks(album.id, album.fingerprint)
            if cached is not None:
                album.tracks = [LocalTrack(**row) for row in cached]
                album.tracks_loaded = True
            else:
                pending.append(album)

        if not pending:
            return 0

        failures = self.client.load_tracks(pending)
        for album in pending:
            if album.tracks_loaded:
                self.store.set_local_tracks(
                    album.id,
                    album.fingerprint,
                    [vars(t) for t in album.tracks],
                )
        return failures

    # --- scan --------------------------------------------------------------

    def run(self, options: ScanOptions) -> ScanResult:
        result = ScanResult()
        artists = self.read_library(options)
        total = len(artists)

        for position, artist in enumerate(artists, start=1):
            self._progress(options, position, total, artist.name)
            before = (len(result.missing), len(result.review), len(result.unresolved))
            outcome = ArtistOutcome(name=artist.name)
            try:
                self._scan_artist(artist, options, result)
            except DropWatchError as exc:
                log.warning("Skipping %r: %s", artist.name, exc)
                result.partial_reasons.append(f"{artist.name} failed")
                outcome.failed = True
            except Exception as exc:  # noqa: BLE001 - one artist must not abort the run
                log.warning("Unexpected failure for %r: %s", artist.name, exc)
                result.partial_reasons.append(f"{artist.name} failed")
                outcome.failed = True
            result.artists_scanned += 1
            result.scanned_names.add(artist.name)

            outcome.missing = result.missing[before[0] :]
            outcome.review = len(result.review) - before[1]
            if len(result.unresolved) > before[2]:
                outcome.unresolved = result.unresolved[-1]
            self._report_artist(options, position, total, outcome)

        self._clear_progress(options)

        result.missing = dedupe_results(result.missing, lambda item: item.release_type)
        # No favourite-ness is stamped here. It used to be, and it went stale:
        # an entry outlives the artist's time as a favourite, and no later
        # favourites scan covers that artist to correct it. The scope record
        # carries the names instead, and is rewritten whole on every scan.
        fresh_missing = [
            {
                "id": item.release.id,
                "artist": item.local_artist,
                "title": item.release.title,
                "type": item.release_type.value,
                "date": str(item.release.release_date),
                "ownership": item.ownership.value,
                "url": item.release.link,
                "notes": ", ".join(item.traits),
            }
            for item in result.missing
        ]
        fresh_review = [
            {
                "id": item.release.id,
                "artist": item.local_artist,
                "title": item.release.title,
                "type": item.release_type.value,
                "date": str(item.release.release_date),
                "reason": item.reason,
                "url": item.release.link,
            }
            for item in result.review
        ]
        fresh_unresolved = [
            {
                "name": u.name,
                "reason": u.reason,
                "candidates": [
                    {"id": c.id, "name": c.name, "fans": c.nb_fan}
                    for c in u.candidates
                ],
            }
            for u in result.unresolved
        ]
        self.store.save_missing(
            _merge_queue(
                self.store.load_missing(), fresh_missing, result.scanned_names, "artist"
            )
        )
        self.store.save_review(
            _merge_queue(self.store.load_review(), fresh_review, result.scanned_names, "artist")
        )
        self.store.save_unresolved(
            _merge_queue(
                self.store.load_unresolved(), fresh_unresolved, result.scanned_names, "name"
            )
        )
        # The scope as resolved, not as typed: `rip` replays what this scan
        # actually covered, so a flag and the setting it overrode are the same
        # fact by the time they reach here. `limit` is deliberately absent — it
        # truncates how much work the scan does rather than describing which
        # releases belong in the results, so there is nothing to replay.
        self.store.save_scan_scope(
            ScanScope(
                favorites=options.favorites,
                since=options.since,
                types=frozenset(t.value for t in options.types or ()),
                artists=tuple(options.artist_filters),
                favorite_artists=self._favorite_artists,
            )
        )
        return result

    def _scan_artist(
        self, artist: LocalArtist, options: ScanOptions, result: ScanResult
    ) -> None:
        resolution = self.resolver.resolve(artist)

        if resolution.status is Resolution.IGNORED:
            return
        if resolution.status is Resolution.ERROR:
            result.partial_reasons.append(f"{artist.name} lookup failed")
            result.unresolved.append(
                UnresolvedArtist(artist.name, resolution.reason, resolution.candidates)
            )
            return
        if not resolution.ok:
            result.unresolved.append(
                UnresolvedArtist(artist.name, resolution.reason, resolution.candidates)
            )
            return

        if resolution.confidence < 0.7:
            log.info(
                "Low-confidence match: %r -> %r (%s)",
                artist.name,
                resolution.artist.name,
                resolution.reason,
            )

        releases = resolution.discography
        if not releases:
            releases = self.provider.get_discography(resolution.artist.id)

        if options.since is not None:
            releases = [r for r in releases if r.release_date.on_or_after(options.since)]

        canonical, _ = deduplicate(releases, artist_key=fold(artist.name))
        if not canonical:
            return

        decisions = self.store.release_decisions()

        # Pass one, on listing data alone. Cheap, and it settles anything that
        # clearly matches an owned album. The verdicts are kept rather than
        # discarded, so the ISRC step below can reuse them instead of
        # recomputing ownership for the whole discography a second time.
        # LocalIndex here holds album titles only; tracks are not loaded yet.
        settled = self._judge_all(canonical, LocalIndex(artist), decisions)
        candidates = [j for j in settled if not j.is_owned]
        if not candidates:
            return

        # Local tracks are only worth fetching once something looks missing.
        track_failures = self.load_local_tracks(artist)
        if track_failures:
            result.partial_reasons.append(
                f"{track_failures} album(s) of {artist.name} could not be read"
            )

        # Rebuilt deliberately: the index above had no track data, and
        # recording-level comparison needs it.
        index = LocalIndex(artist)

        for judged in candidates:
            try:
                self.provider.load_release_detail(judged.release)
            except DropWatchError as exc:
                log.debug("No detail for %r: %s", judged.release.title, exc)
            judged.rejudge(index, decisions)

        self._apply_isrc_evidence(candidates, settled, index)

        for judged in candidates:
            if options.types and judged.release_type not in options.types:
                continue
            self._record(judged, artist.name, result)

    def _judge_all(
        self, releases: list[DeezerRelease], index: LocalIndex, decisions: dict[str, str]
    ) -> list[Judged]:
        return [Judged.evaluate(r, index, decisions) for r in releases]

    def _apply_isrc_evidence(
        self, candidates: list[Judged], settled: list[Judged], index: LocalIndex
    ) -> None:
        """Let owned albums vouch for singles that look like advance releases.

        Only recordings the library actually holds vouch: an album settled as
        owned on its title alone may be part-owned, and its Deezer tracklist
        may name songs that are not even released yet.
        """
        if not any(j.verdict.reportable and j.is_single for j in candidates):
            return

        owned_albums = [j for j in settled if j.is_owned and not j.is_single]
        isrc_index = IsrcIndex()
        for judged in owned_albums[:MAX_ISRC_REFERENCE_ALBUMS]:
            try:
                self.provider.load_release_detail(judged.release)
            except DropWatchError:
                continue
            isrc_index.add_release(judged.release, index)

        if not isrc_index.codes:
            return
        for judged in candidates:
            if judged.is_single:
                judged.verdict = refine_with_isrc(
                    judged.verdict, judged.release, isrc_index
                )

    def _record(self, judged: Judged, artist_name: str, result: ScanResult) -> None:
        if judged.verdict.ownership is Ownership.AMBIGUOUS:
            result.review.append(
                ReviewItem(
                    judged.release, artist_name, judged.release_type, judged.verdict.reason
                )
            )
        elif judged.verdict.reportable:
            result.missing.append(
                MissingRelease(
                    release=judged.release,
                    local_artist=artist_name,
                    release_type=judged.release_type,
                    ownership=judged.verdict.ownership,
                    reason=judged.verdict.reason,
                    traits=judged.traits,
                )
            )

    # --- progress ----------------------------------------------------------

    def _show_progress(self, options: ScanOptions) -> bool:
        # Only on a terminal: piping stderr somewhere should stay clean, and
        # the transient line depends on ANSI erase.
        return options.progress and sys.stderr.isatty()

    def _progress(self, options: ScanOptions, position: int, total: int, name: str) -> None:
        """Transient line for the artist currently being scanned."""
        if not self._show_progress(options):
            return
        label = name if len(name) <= 40 else name[:39] + "…"
        sys.stderr.write(f"\r\033[2K[{position}/{total}] {label}")
        sys.stderr.flush()

    def _report_artist(
        self, options: ScanOptions, position: int, total: int, outcome: ArtistOutcome
    ) -> None:
        """Permanent line, written only when the artist yielded something.

        Findings are summarised rather than listed: printing the rows here
        would duplicate the final table, and the point of this line is to show
        the scan is alive and productive, not to be read as the report.
        """
        if not self._show_progress(options) or not outcome.worth_reporting:
            return
        label = outcome.name if len(outcome.name) <= 40 else outcome.name[:39] + "…"
        sys.stderr.write(
            f"\r\033[2K[{position}/{total}] {label} — {outcome.describe()}\n"
        )
        sys.stderr.flush()

    def _clear_progress(self, options: ScanOptions) -> None:
        if self._show_progress(options):
            sys.stderr.write("\r\033[2K")
            sys.stderr.flush()
