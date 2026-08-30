"""Release classification: Album / EP / Single / Unknown.

Deezer's ``record_type`` is the primary signal but it is not consistent — the
catalogue contains three-track "albums" and eight-track "singles". So the
declared type is cross-checked against track count and total duration, and the
structural evidence is allowed to override it only when the disagreement is
large. When neither signal is trustworthy the release is reported as Unknown
rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import DeezerRelease, ReleaseType
from .normalize import parse_title, track_key

# Structural boundaries, in tracks and seconds.
ALBUM_MIN_TRACKS = 7
ALBUM_MIN_SECONDS = 1800  # 30 minutes
EP_MIN_TRACKS = 4
EP_MIN_SECONDS = 540  # 9 minutes
SINGLE_MAX_TRACKS = 3

UNKNOWN_CONFIDENCE_THRESHOLD = 0.4

_RECORD_TYPE_MAP = {
    "album": ReleaseType.ALBUM,
    "ep": ReleaseType.EP,
    "single": ReleaseType.SINGLE,
    "compilation": ReleaseType.ALBUM,
}


@dataclass(frozen=True)
class Classification:
    type: ReleaseType
    confidence: float
    traits: frozenset[str]
    source: str  # "declared", "structural", "corrected" or "unknown"


def _structural_type(nb_tracks: int | None, duration: int | None) -> ReleaseType | None:
    """Infer type from shape alone. None when the shape is not decisive."""
    if nb_tracks is None:
        return None

    if nb_tracks >= ALBUM_MIN_TRACKS:
        return ReleaseType.ALBUM
    if duration is not None and duration >= ALBUM_MIN_SECONDS:
        # Few but very long tracks: a long-player regardless of track count.
        return ReleaseType.ALBUM
    if nb_tracks >= EP_MIN_TRACKS:
        return ReleaseType.EP
    if nb_tracks <= SINGLE_MAX_TRACKS:
        if duration is not None and duration >= EP_MIN_SECONDS and nb_tracks == SINGLE_MAX_TRACKS:
            # Three substantial tracks sit on the single/EP boundary.
            return ReleaseType.EP
        return ReleaseType.SINGLE
    return None


def local_release_type(song_count: int, duration: int | None) -> ReleaseType | None:
    """Type of a *local* album, from its shape alone.

    Navidrome does not record whether an album is an album, an EP or a single,
    so the same structural thresholds Deezer's inconsistent `record_type` is
    cross-checked against are the only ruler available. None when the shape
    says nothing — an album whose song count was never read.
    """
    return _structural_type(song_count or None, duration)


def shared_track_versions(release: DeezerRelease) -> set[str]:
    """Version markers that every track on the release carries.

    Deezer routinely leaves the marker off the release title and puts it only
    on the tracks: the single titled plainly "Castaway" whose sole track is
    "Castaway (Sunset Tsunami & MO2 Remix)" printed as a bare duplicate of a
    song the user already owned, with nothing on the line to explain itself.

    Every track, not any track: a release is a remix release when all of it is
    remixes, while one remix among twelve album tracks says nothing about the
    album. Venue and year detail is already collapsed by `track_key`, so a live
    record whose tracks each name a different city still reads as one "live".
    """
    if not release.tracks:
        return set()
    shared: set[str] | None = None
    for track in release.tracks:
        _, versions = track_key(track.title_short or track.title, track.title_version)
        shared = set(versions) if shared is None else shared & versions
        if not shared:
            return set()
    return shared or set()


def classify_release(release: DeezerRelease) -> Classification:
    """Decide the primary type and collect secondary characteristics."""
    parsed = parse_title(release.title)
    traits = set(parsed.traits) | shared_track_versions(release)

    declared_raw = (release.record_type or "").strip().lower()
    declared = _RECORD_TYPE_MAP.get(declared_raw)
    if declared_raw == "compilation":
        traits.add("compilation")

    structural = _structural_type(release.nb_tracks, release.duration)

    # No detail fetched: the declared type is all we have.
    if structural is None:
        if declared is None:
            return Classification(
                ReleaseType.UNKNOWN, 0.0, frozenset(traits), "unknown"
            )
        return Classification(declared, 0.6, frozenset(traits), "declared")

    if declared is None:
        return Classification(structural, 0.6, frozenset(traits), "structural")

    if declared == structural:
        return Classification(declared, 0.95, frozenset(traits), "declared")

    # Disagreement. Trust structure when the mismatch is stark, otherwise keep
    # the declared type and lower confidence.
    severity = _disagreement_severity(declared, structural)
    if severity >= 2:
        return Classification(structural, 0.7, frozenset(traits), "corrected")
    if severity == 1:
        return Classification(structural, 0.55, frozenset(traits), "corrected")
    return Classification(declared, 0.5, frozenset(traits), "declared")


_ORDER = {ReleaseType.SINGLE: 0, ReleaseType.EP: 1, ReleaseType.ALBUM: 2}


def _disagreement_severity(declared: ReleaseType, structural: ReleaseType) -> int:
    """How far apart the two signals are, on the single -> EP -> album scale."""
    a, b = _ORDER.get(declared), _ORDER.get(structural)
    if a is None or b is None:
        return 0
    return abs(a - b)


def resolve_type(release: DeezerRelease) -> tuple[ReleaseType, Classification]:
    """Classification with the low-confidence cases collapsed to Unknown."""
    classification = classify_release(release)
    if classification.confidence < UNKNOWN_CONFIDENCE_THRESHOLD:
        return ReleaseType.UNKNOWN, classification
    return classification.type, classification
