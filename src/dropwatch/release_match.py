"""Deciding whether a Deezer release is already in the local library.

Two independent lines of evidence are used:

*title identity* — the normalised base title, with cosmetic edition markers
removed but meaningful version markers preserved; and

*recording coverage* — whether each individual recording on the Deezer release
already exists somewhere in the artist's local catalogue, matched on track
title, version marker and duration.

Coverage is what makes singles behave sensibly: an advance single whose only
track is already on an owned album is not reported, while a single carrying an
exclusive B-side or a different mix is. Where the evidence cannot settle the
question the release is marked ambiguous and shown in a review section rather
than being asserted as missing.

For singles the duration half of that test is dropped when the same song, with
the same version markers, sits on an owned album or EP credited to the artist.
A single and its parent album routinely differ by a few seconds of lead-in or
fade, and a genuinely different edit almost always says so in a version marker
("Radio Edit", "Single Version") that identity already keeps apart — so the
length difference on its own was reporting songs the user demonstrably owns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .classify import local_release_type
from .models import (
    DECISION_BLOCKED,
    DECISION_MISSING,
    DECISION_OWNED,
    DeezerRelease,
    LocalAlbum,
    LocalArtist,
    LocalTrack,
    Ownership,
    ReleaseType,
)
from .normalize import (
    ParsedTitle,
    artist_key_variants,
    credited_to,
    durations_match,
    parse_title,
    title_similarity,
    track_key,
)

#: Track coverage above this counts as "essentially all of it".
HIGH_COVERAGE = 0.85
#: Title similarity above this, without an exact base match, is suspicious.
NEAR_TITLE_MATCH = 0.90
#: Local and Deezer years may legitimately differ by this much.
YEAR_TOLERANCE = 1


@dataclass
class Verdict:
    ownership: Ownership
    reason: str = ""

    @property
    def reportable(self) -> bool:
        return self.ownership in (Ownership.MISSING, Ownership.PROBABLY_MISSING)


@dataclass
class Coverage:
    total: int = 0
    same: int = 0
    different: int = 0
    unknown: int = 0
    absent: int = 0

    @property
    def present(self) -> int:
        return self.same + self.unknown

    @property
    def ratio(self) -> float:
        return (self.present / self.total) if self.total else 0.0


class LocalIndex:
    """Everything known locally about one artist, indexed for lookup."""

    def __init__(self, artist: LocalArtist) -> None:
        self.artist = artist
        self.albums: list[LocalAlbum] = list(artist.albums)
        self.parsed: dict[str, ParsedTitle] = {}
        self._variants = artist_key_variants(artist.name)
        self._by_base: dict[str, list[LocalAlbum]] = {}
        self._tracks_exact: dict[tuple[str, frozenset[str]], list[LocalTrack]] = {}
        self._tracks_by_base: dict[str, list[LocalTrack]] = {}
        self._on_a_product: set[tuple[str, frozenset[str]]] = set()

        for album in self.albums:
            parsed = parse_title(album.name)
            self.parsed[album.id] = parsed
            if parsed.base:
                self._by_base.setdefault(parsed.base, []).append(album)
            # Falls back to the loaded tracks because `songCount` is a listing
            # field, and an album that arrived without one still has a shape.
            product = local_release_type(
                album.song_count or len(album.tracks), album.duration
            ) in (ReleaseType.ALBUM, ReleaseType.EP)
            for track in album.tracks:
                base, versions = track_key(track.title)
                if not base:
                    continue
                self._tracks_exact.setdefault((base, versions), []).append(track)
                self._tracks_by_base.setdefault(base, []).append(track)
                if product and self._credits_artist(track, album):
                    self._on_a_product.add((base, versions))

    def _credits_artist(self, track: LocalTrack, album: LocalAlbum) -> bool:
        """Is this recording credited to the artist being scanned?

        Asked because an album reaching this index is not proof that its every
        track is by this artist: a compilation credits dozens of acts, and
        suppressing a single because someone *else*'s song shares its title
        would be exactly the false claim the review section exists to avoid.
        """
        if not self._variants:
            return False
        return any(
            credited_to(name, self._variants)
            for name in (*track.credits, *album.credits)
        )

    @property
    def has_tracks(self) -> bool:
        return bool(self._tracks_by_base)

    def albums_with_base(self, base: str) -> list[LocalAlbum]:
        return self._by_base.get(base, [])

    def find_recording(
        self, base: str, versions: frozenset[str]
    ) -> tuple[list[LocalTrack], list[LocalTrack]]:
        """Return (exact version matches, same-song-different-version matches)."""
        return self._tracks_exact.get((base, versions), []), self._tracks_by_base.get(base, [])

    def on_owned_product(self, base: str, versions: frozenset[str]) -> bool:
        """Is this exact recording a track on an owned album or EP by the artist?"""
        return (base, versions) in self._on_a_product

    def best_title_similarity(self, base: str) -> tuple[float, LocalAlbum | None]:
        best_score, best_album = 0.0, None
        for album in self.albums:
            score = title_similarity(base, self.parsed[album.id].base)
            if score > best_score:
                best_score, best_album = score, album
        return best_score, best_album


def compute_coverage(
    release: DeezerRelease, index: LocalIndex, relax_duration: bool = False
) -> Coverage:
    """How much of the release's material already exists locally.

    With `relax_duration`, a recording whose title and version markers match a
    track on an owned album or EP counts as owned however long it runs. Only
    singles ask for this: a single and its parent album routinely differ by a
    few seconds of lead-in or fade, and treating that as a different edit put
    songs the user demonstrably owns back in the queue.
    """
    coverage = Coverage()
    for track in release.tracks:
        coverage.total += 1
        base, versions = track_key(track.title_short or track.title, track.title_version)
        if not base:
            coverage.unknown += 1
            continue

        exact, same_song = index.find_recording(base, versions)
        if not exact:
            # The song exists locally but only in a different version.
            coverage.different += 1 if same_song else 0
            coverage.absent += 0 if same_song else 1
            continue

        if relax_duration and index.on_owned_product(base, versions):
            coverage.same += 1
            continue

        verdicts = [durations_match(track.duration, t.duration) for t in exact]
        if any(v is True for v in verdicts):
            coverage.same += 1
        elif all(v is False for v in verdicts):
            # Same title and version, materially different length.
            coverage.different += 1
        else:
            coverage.unknown += 1
    return coverage


def determine_ownership(
    release: DeezerRelease,
    index: LocalIndex,
    release_type: ReleaseType,
    decisions: dict[str, str] | None = None,
) -> Verdict:
    """Classify one Deezer release against the local library.

    A decision the user recorded in the review workflow wins outright: the
    point of reviewing something is not to be asked about it again.
    """
    decision = (decisions or {}).get(release.id)
    if decision == DECISION_OWNED:
        return Verdict(Ownership.IGNORED, "you marked this as already owned")
    if decision == DECISION_BLOCKED:
        return Verdict(Ownership.IGNORED, "you blocked this release")
    if decision == DECISION_MISSING:
        return Verdict(Ownership.MISSING, "you marked this as missing")

    parsed = parse_title(release.title)
    if not parsed.base:
        return Verdict(Ownership.AMBIGUOUS, "release has no usable title")

    # A single named after a song the library already holds on an album or EP,
    # credited to this artist, is the advance-single case stated as a title
    # rather than inferred from a tracklist. It settles the release outright
    # only when there is no tracklist to check; otherwise coverage still runs,
    # so a single carrying an exclusive B-side is reported as before.
    if release_type is ReleaseType.SINGLE and index.on_owned_product(
        parsed.base, parsed.versions
    ):
        if not release.tracks:
            return Verdict(
                Ownership.OWNED, "this song is already on an owned album or EP"
            )

    title_matches = index.albums_with_base(parsed.base)
    same_version = [
        a for a in title_matches if index.parsed[a.id].versions == parsed.versions
    ]

    if same_version:
        return _judge_title_match(release, parsed, same_version, index)

    if title_matches:
        # Same album name, different version marker: a live or acoustic take on
        # something owned is still a release we do not have.
        return _judge_coverage(
            release,
            index,
            release_type,
            note=f"different version ({', '.join(sorted(parsed.versions)) or 'unmarked'})",
        )

    score, near = index.best_title_similarity(parsed.base)
    if near is not None and score >= NEAR_TITLE_MATCH:
        return Verdict(
            Ownership.AMBIGUOUS,
            f"title closely resembles local album {near.name!r} but is not identical",
        )

    return _judge_coverage(release, index, release_type)


def _judge_title_match(
    release: DeezerRelease,
    parsed: ParsedTitle,
    matches: list[LocalAlbum],
    index: LocalIndex,
) -> Verdict:
    """A local album shares this release's title and version markers."""
    local = max(matches, key=lambda a: a.song_count)

    # A large year gap on an identical title suggests a re-recording rather
    # than the same product, unless the release is flagged as a reissue.
    if local.year and release.release_date.year:
        gap = abs(local.year - release.release_date.year)
        # Any edition marker ("Deluxe", "2011 Remaster", "20th Anniversary")
        # explains a later date for the same album, so the gap is expected.
        reissue_like = bool(parsed.editions) or bool(
            parsed.traits & {"remaster", "reissue", "anniversary", "compilation", "deluxe", "expanded"}
        )
        if gap > YEAR_TOLERANCE and not reissue_like:
            return Verdict(
                Ownership.AMBIGUOUS,
                f"title matches a local album, but yours is dated {local.year} "
                f"and Deezer says {release.release_date.year}",
            )

    if release.nb_tracks is None:
        return Verdict(Ownership.PROBABLY_OWNED, "title matches a local album")

    if release.nb_tracks <= local.song_count:
        return Verdict(Ownership.OWNED, "title and track count match a local album")

    # Deezer lists more tracks: typically a deluxe or expanded edition.
    if not index.has_tracks or not release.tracks:
        return Verdict(
            Ownership.AMBIGUOUS,
            f"Deezer's edition has {release.nb_tracks} tracks, yours has "
            f"{local.song_count}",
        )

    coverage = compute_coverage(release, index)
    if coverage.absent == 0 and coverage.different == 0:
        return Verdict(Ownership.OWNED, "extra tracks on this edition are already owned")
    if coverage.ratio >= HIGH_COVERAGE and coverage.different == 0:
        return Verdict(Ownership.PROBABLY_OWNED, "nearly all tracks already owned")

    extra = coverage.absent + coverage.different
    return Verdict(
        Ownership.PROBABLY_MISSING,
        f"edition adds {extra} track(s) not in the library",
    )


def _judge_coverage(
    release: DeezerRelease,
    index: LocalIndex,
    release_type: ReleaseType,
    note: str = "",
) -> Verdict:
    """No local album by this title: decide on recording coverage instead."""
    prefix = f"{note}; " if note else ""

    if not release.tracks:
        # Detail was unavailable. Being wrong about a single is easy, so
        # singles go to review while larger releases are reported as missing.
        if release_type is ReleaseType.SINGLE:
            return Verdict(
                Ownership.AMBIGUOUS,
                prefix + "could not read the track list to check for owned recordings",
            )
        return Verdict(Ownership.MISSING, prefix + "no local album with this title")

    if not index.has_tracks:
        return Verdict(
            Ownership.PROBABLY_MISSING,
            prefix + "no local album with this title (local track list unavailable)",
        )

    coverage = compute_coverage(
        release, index, relax_duration=release_type is ReleaseType.SINGLE
    )

    if coverage.total == 0:
        return Verdict(Ownership.AMBIGUOUS, prefix + "release lists no tracks")

    if coverage.absent == 0 and coverage.different == 0:
        if coverage.unknown == 0:
            return Verdict(
                Ownership.OWNED, prefix + "every recording is already in the library"
            )
        return Verdict(
            Ownership.PROBABLY_OWNED,
            prefix + "all recordings appear to be owned, some durations unconfirmed",
        )

    if coverage.absent == 0 and coverage.different > 0:
        # Titles line up but the recordings differ: alternate takes or mixes.
        return Verdict(
            Ownership.PROBABLY_MISSING,
            prefix
            + f"{coverage.different} recording(s) differ from the owned versions",
        )

    if coverage.present == 0:
        return Verdict(Ownership.MISSING, prefix + "none of these recordings are owned")

    exclusive = coverage.absent + coverage.different
    if release_type is ReleaseType.SINGLE:
        return Verdict(
            Ownership.PROBABLY_MISSING,
            prefix + f"{exclusive} exclusive track(s) not owned",
        )
    return Verdict(
        Ownership.MISSING,
        prefix + f"{exclusive} of {coverage.total} tracks not owned",
    )


# --- ISRC refinement -------------------------------------------------------


@dataclass
class IsrcIndex:
    """ISRCs from Deezer releases already established as owned."""

    codes: set[str] = field(default_factory=set)

    def add_release(self, release: DeezerRelease, local: LocalIndex) -> None:
        """Index the ISRCs of recordings the library actually holds.

        Deezer's tracklist is the album *as published*. Artists increasingly
        drip-release an album a song at a time, each new song landing as a
        single and on the album at once, so that tracklist routinely names
        recordings that are not out yet and therefore cannot be owned. Taking
        the whole tracklist on trust let a part-owned album vouch for the very
        advance singles the user was waiting for, so a track vouches only once
        the library holds that recording.
        """
        for track in release.tracks:
            if not track.isrc:
                continue
            base, versions = track_key(track.title_short or track.title, track.title_version)
            if not base:
                continue
            exact, _ = local.find_recording(base, versions)
            if any(durations_match(track.duration, t.duration) is True for t in exact):
                self.codes.add(track.isrc.upper())

    def covers(self, release: DeezerRelease) -> bool | None:
        """True when every track's ISRC is already owned; None if unknowable."""
        isrcs = [t.isrc.upper() for t in release.tracks if t.isrc]
        if not isrcs or len(isrcs) != len(release.tracks):
            return None
        if not self.codes:
            return None
        return all(code in self.codes for code in isrcs)


def refine_with_isrc(verdict: Verdict, release: DeezerRelease, owned: IsrcIndex) -> Verdict:
    """Suppress a single whose exact recordings sit on an owned album.

    This is the precise form of the "advance single" case: identical ISRCs mean
    identical recordings, which title and duration matching can only approximate.
    """
    if not verdict.reportable:
        return verdict
    if owned.covers(release) is True:
        return Verdict(
            Ownership.OWNED,
            "same recordings (by ISRC) already owned on another release",
        )
    return verdict
