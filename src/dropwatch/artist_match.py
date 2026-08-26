"""Resolving local artist names to Deezer artist IDs.

The library has no stable external identifiers, so resolution rests on name
evidence corroborated by catalogue overlap: a candidate that shares album
titles with what is already on disk is almost certainly the right artist,
while a same-named candidate with nothing in common probably is not.

Artist names are never split on ``&``, ``and``, ``feat.``, ``vs.`` or commas.
Those substrings occur inside real names, and splitting them fabricates
artists that do not exist.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

from .deezer import DeezerProvider
from .errors import DropWatchError
from .models import DeezerArtist, DeezerRelease, LocalArtist
from .normalize import artist_key, artist_key_variants, fold, similarity
from .state import STATUS_BLOCKED, Store

log = logging.getLogger("dropwatch.artist_match")

MIN_NAME_SCORE = 0.85
MAX_CANDIDATES = 25
#: How many candidates are worth spending discography requests on.
MAX_DISAMBIGUATION_FETCHES = 3

# Local "artists" that are not artists.
_NON_ARTIST_NAMES = {
    "various artists",
    "various",
    "va",
    "unknown artist",
    "unknown",
    "soundtrack",
    "original soundtrack",
    "compilation",
}

# Candidate names that usually indicate a different, derivative act.
_DERIVATIVE_RE = re.compile(
    r"\b(karaoke|tribute|made\s+famous|in\s+the\s+style\s+of|cover\s+band"
    r"|performed\s+by|backing\s+track|instrumental\s+version)\b",
    re.IGNORECASE,
)


class Resolution(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    IGNORED = "ignored"
    ERROR = "error"


@dataclass
class Candidate:
    artist: DeezerArtist
    name_score: float
    overlap: int = 0
    discography: list[DeezerRelease] = field(default_factory=list)

    @property
    def total_score(self) -> float:
        # Overlap dominates; fan count is only a tie-breaker, because
        # popularity is weak supporting evidence — see docs/DESIGN.md.
        popularity = min(self.artist.nb_fan, 1_000_000) / 1_000_000 * 0.05
        return self.name_score + min(self.overlap, 5) * 0.1 + popularity


@dataclass
class ArtistResolution:
    local: LocalArtist
    status: Resolution
    artist: DeezerArtist | None = None
    confidence: float = 0.0
    reason: str = ""
    candidates: list[DeezerArtist] = field(default_factory=list)
    discography: list[DeezerRelease] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is Resolution.RESOLVED and self.artist is not None


def name_score(local_name: str, candidate_name: str) -> float:
    """0..1 score for how well two artist names correspond."""
    local_variants = artist_key_variants(local_name)
    candidate_variants = artist_key_variants(candidate_name)
    if not local_variants or not candidate_variants:
        return 0.0

    local_key_exact = artist_key(local_name)
    candidate_key_exact = artist_key(candidate_name)
    if local_key_exact == candidate_key_exact:
        return 1.0
    if local_variants & candidate_variants:
        return 0.92

    best = max(
        similarity(a, b) for a in local_variants for b in candidate_variants
    )
    # A candidate whose name merely *contains* the local name (e.g. "Björk &
    # Toffe" for "Björk") is a different act, so containment is not rewarded.
    return best


def album_overlap(local: LocalArtist, releases: list[DeezerRelease]) -> int:
    """Number of local album titles that also appear in the Deezer listing."""
    if not local.albums or not releases:
        return 0
    from .normalize import parse_title  # local import keeps module import cheap

    deezer_titles = {parse_title(r.title).base for r in releases}
    deezer_titles.discard("")
    count = 0
    for album in local.albums:
        base = parse_title(album.name).base
        if base and base in deezer_titles:
            count += 1
    return count


class ArtistResolver:
    def __init__(self, provider: DeezerProvider, store: Store) -> None:
        self.provider = provider
        self.store = store

    def resolve(self, local: LocalArtist) -> ArtistResolution:
        if fold(local.name) in _NON_ARTIST_NAMES:
            return ArtistResolution(
                local,
                Resolution.IGNORED,
                reason="not a single artist (compilation placeholder)",
            )

        mapping = self.store.get_mapping(local.name)
        if mapping is not None:
            if mapping.status == STATUS_BLOCKED:
                return ArtistResolution(local, Resolution.IGNORED, reason="blocked by you")
            if mapping.targets:
                return self._resolve_from_mapping(local, mapping)

        try:
            found = self.provider.search_artists(local.name, limit=MAX_CANDIDATES)
        except DropWatchError as exc:
            return ArtistResolution(local, Resolution.ERROR, reason=str(exc))

        return self._resolve_from_search(local, found)

    # --- internals ---------------------------------------------------------

    def _resolve_from_mapping(self, local: LocalArtist, mapping) -> ArtistResolution:
        """Merge the discographies of every Deezer id mapped to this artist.

        Deezer often splits one act across duplicate artist entries, each
        holding part of the catalogue, so a mapping may name several.
        """
        artists: list[DeezerArtist] = []
        releases: list[DeezerRelease] = []
        failures: list[str] = []

        for target in mapping.targets:
            try:
                artist = self.provider.get_artist(target.deezer_id)
            except DropWatchError as exc:
                failures.append(f"{target.deezer_id}: {exc}")
                continue
            if artist is None:
                failures.append(f"{target.deezer_id}: no such Deezer artist")
                continue
            artists.append(artist)
            try:
                releases.extend(self.provider.get_discography(target.deezer_id))
            except DropWatchError as exc:
                failures.append(f"{target.deezer_id}: {exc}")

        if not artists:
            detail = "; ".join(failures) or "no Deezer ids mapped"
            return ArtistResolution(
                local,
                Resolution.NOT_FOUND,
                reason=f"manual mapping is unusable ({detail})",
            )

        # Releases carried by more than one mapped id would otherwise appear
        # twice; identity is the release id regardless of which entry listed it.
        seen: set[str] = set()
        merged: list[DeezerRelease] = []
        for release in releases:
            if release.id in seen:
                continue
            seen.add(release.id)
            merged.append(release)

        reason = "manual mapping"
        if len(artists) > 1:
            reason = f"manual mapping ({len(artists)} Deezer artists merged)"
        if failures:
            reason += f"; {len(failures)} mapped id(s) failed"
            log.warning("Mapping for %r partially failed: %s", local.name, "; ".join(failures))

        return ArtistResolution(
            local,
            Resolution.RESOLVED,
            artist=artists[0],
            confidence=1.0,
            reason=reason,
            discography=merged,
        )

    def _resolve_from_search(
        self, local: LocalArtist, found: list[DeezerArtist]
    ) -> ArtistResolution:
        local_is_derivative = bool(_DERIVATIVE_RE.search(local.name))
        candidates: list[Candidate] = []
        for artist in found:
            score = name_score(local.name, artist.name)
            if score < MIN_NAME_SCORE:
                continue
            if _DERIVATIVE_RE.search(artist.name) and not local_is_derivative:
                continue
            candidates.append(Candidate(artist=artist, name_score=score))

        if not candidates:
            return ArtistResolution(
                local,
                Resolution.NOT_FOUND,
                reason="no Deezer artist with a sufficiently similar name",
                candidates=found[:5],
            )

        exact = [c for c in candidates if c.name_score >= 1.0]
        pool = exact or candidates
        pool.sort(key=lambda c: (-c.name_score, -c.artist.nb_fan))

        # Corroborate with catalogue overlap. For the eventual winner this is
        # not extra work: the discography is needed by the scan anyway.
        for candidate in pool[:MAX_DISAMBIGUATION_FETCHES]:
            try:
                candidate.discography = self.provider.get_discography(candidate.artist.id)
            except DropWatchError as exc:
                log.debug("Could not fetch discography for %s: %s", candidate.artist.name, exc)
                continue
            candidate.overlap = album_overlap(local, candidate.discography)

        pool.sort(key=lambda c: -c.total_score)
        best = pool[0]
        runner_up = pool[1] if len(pool) > 1 else None

        return self._decide(local, best, runner_up, pool, bool(exact))

    def _decide(
        self,
        local: LocalArtist,
        best: Candidate,
        runner_up: Candidate | None,
        pool: list[Candidate],
        had_exact: bool,
    ) -> ArtistResolution:
        others = [c.artist for c in pool[:5]]

        if best.overlap > 0:
            # Shared album titles: the strongest evidence available.
            if runner_up is not None and runner_up.overlap >= best.overlap and had_exact:
                return ArtistResolution(
                    local,
                    Resolution.AMBIGUOUS,
                    reason=(
                        f"{len(pool)} Deezer artists share this name with equal "
                        "catalogue overlap"
                    ),
                    candidates=others,
                )
            return ArtistResolution(
                local,
                Resolution.RESOLVED,
                artist=best.artist,
                confidence=0.95 if had_exact else 0.8,
                reason=f"{best.overlap} shared album title(s)",
                discography=best.discography,
            )

        # No overlap. Acceptable when there is nothing to overlap with.
        if not local.albums:
            return ArtistResolution(
                local,
                Resolution.RESOLVED if had_exact else Resolution.AMBIGUOUS,
                artist=best.artist if had_exact else None,
                confidence=0.75 if had_exact else 0.0,
                reason="exact name match, no local albums to corroborate",
                candidates=others,
                discography=best.discography if had_exact else [],
            )

        if not had_exact:
            return ArtistResolution(
                local,
                Resolution.AMBIGUOUS,
                reason="only approximate name matches, and no shared album titles",
                candidates=others,
            )

        if len([c for c in pool if c.name_score >= 1.0]) > 1:
            return ArtistResolution(
                local,
                Resolution.AMBIGUOUS,
                reason="several Deezer artists share this name and none matches the local albums",
                candidates=others,
            )

        # A lone exact name match with no shared titles: plausible for artists
        # whose catalogue is thin on Deezer, so it resolves, but at low
        # confidence and with a note the user can act on.
        return ArtistResolution(
            local,
            Resolution.RESOLVED,
            artist=best.artist,
            confidence=0.6,
            reason="exact name match but no shared album titles",
            discography=best.discography,
        )
