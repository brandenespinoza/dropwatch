"""Domain models shared by the Navidrome reader, Deezer provider and matcher."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from functools import total_ordering


class ReleaseType(str, Enum):
    ALBUM = "Album"
    EP = "EP"
    SINGLE = "Single"
    UNKNOWN = "Unknown"


class Ownership(str, Enum):
    OWNED = "owned"
    PROBABLY_OWNED = "probably_owned"
    MISSING = "missing"
    PROBABLY_MISSING = "probably_missing"
    AMBIGUOUS = "ambiguous"
    IGNORED = "ignored"


#: A decision the user recorded about a release.
#: OWNED and BLOCKED both suppress it; they are kept apart because "I have this"
#: and "I don't want to be told about this" are different statements, and only
#: the first is a claim about the library.
DECISION_OWNED = "owned"
DECISION_MISSING = "missing"
DECISION_BLOCKED = "blocked"

DECISIONS = (DECISION_OWNED, DECISION_MISSING, DECISION_BLOCKED)

#: Only these appear in the main terminal list.
REPORTABLE = (Ownership.MISSING, Ownership.PROBABLY_MISSING)


class DatePrecision(int, Enum):
    """Ordered so that higher precision sorts first within the same period."""

    UNKNOWN = 0
    YEAR = 1
    MONTH = 2
    DAY = 3


_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")


@total_ordering
@dataclass(frozen=True)
class ReleaseDate:
    """A release date that knows how precise it is.

    Deezer returns ``0000-00-00`` and ``2019-00-00`` style values, so precision
    has to be derived rather than assumed.
    """

    year: int | None = None
    month: int | None = None
    day: int | None = None

    @classmethod
    def parse(cls, raw: str | int | None) -> ReleaseDate:
        if raw is None:
            return cls()
        text = str(raw).strip()
        if not text:
            return cls()
        match = _DATE_RE.match(text)
        if not match:
            return cls()
        year = int(match.group(1))
        if not 1000 <= year <= 2999:
            return cls()
        month = int(match.group(2)) if match.group(2) else 0
        day = int(match.group(3)) if match.group(3) else 0
        if not 1 <= month <= 12:
            return cls(year=year)
        if not 1 <= day <= 31:
            return cls(year=year, month=month)
        return cls(year=year, month=month, day=day)

    @property
    def precision(self) -> DatePrecision:
        if self.year is None:
            return DatePrecision.UNKNOWN
        if self.month is None:
            return DatePrecision.YEAR
        if self.day is None:
            return DatePrecision.MONTH
        return DatePrecision.DAY

    @property
    def sort_key(self) -> tuple[int, int, int, int]:
        """Key for a descending sort.

        Unknown dates collapse to all-zero so they land at the very bottom.
        Within a period, a less precise date sorts *after* the fully dated
        releases it could overlap: 2024-06-15 > 2024-06 > 2024-05-02 > 2024.
        """
        if self.year is None:
            return (0, 0, 0, 0)
        return (self.year, self.month or 0, self.day or 0, int(self.precision))

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ReleaseDate):
            return NotImplemented
        return self.sort_key < other.sort_key

    def on_or_after(self, cutoff: ReleaseDate) -> bool:
        """Is this date at or after `cutoff`, given both may be imprecise?

        Comparison stops at whichever date is less precise, and an unknown
        date always passes. A release dated only "2024" is not excluded by a
        cutoff of 2024-06-01, because it may well fall after it — filtering it
        out would hide a release on a technicality.
        """
        if self.year is None or cutoff.year is None:
            return True
        if self.year != cutoff.year:
            return self.year > cutoff.year
        if self.month is None or cutoff.month is None:
            return True
        if self.month != cutoff.month:
            return self.month > cutoff.month
        if self.day is None or cutoff.day is None:
            return True
        return self.day >= cutoff.day

    def __str__(self) -> str:
        if self.year is None:
            return "unknown"
        if self.month is None:
            return f"{self.year:04d}"
        if self.day is None:
            return f"{self.year:04d}-{self.month:02d}"
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"


# --- Local library ---------------------------------------------------------


@dataclass
class LocalTrack:
    title: str
    duration: int | None = None
    track: int | None = None
    disc: int | None = None
    artist: str = ""
    year: int | None = None


@dataclass
class LocalAlbum:
    id: str
    name: str
    artist: str
    artist_id: str = ""
    year: int | None = None
    song_count: int = 0
    duration: int | None = None
    tracks: list[LocalTrack] = field(default_factory=list)
    tracks_loaded: bool = False

    @property
    def fingerprint(self) -> str:
        """Changes when the album's content changes, for cache invalidation."""
        return f"{self.song_count}:{self.duration or 0}"


@dataclass
class LocalArtist:
    id: str
    name: str
    album_count: int = 0
    albums: list[LocalAlbum] = field(default_factory=list)


# --- Deezer ----------------------------------------------------------------


@dataclass
class DeezerTrack:
    id: str
    title: str
    title_short: str = ""
    title_version: str = ""
    duration: int | None = None
    isrc: str | None = None
    disc: int | None = None
    position: int | None = None
    artist: str = ""


@dataclass
class DeezerRelease:
    id: str
    title: str
    artist_id: str
    artist_name: str
    record_type: str = ""
    release_date: ReleaseDate = field(default_factory=ReleaseDate)
    explicit: bool = False
    fans: int = 0
    link: str = ""
    # Populated only when album detail is fetched.
    nb_tracks: int | None = None
    duration: int | None = None
    upc: str | None = None
    label: str | None = None
    contributors: list[str] = field(default_factory=list)
    tracks: list[DeezerTrack] = field(default_factory=list)
    detail_loaded: bool = False


@dataclass
class DeezerArtist:
    id: str
    name: str
    nb_album: int = 0
    nb_fan: int = 0
    link: str = ""


# --- Results ---------------------------------------------------------------


@dataclass
class MissingRelease:
    """A Deezer release judged absent from the library, ready for printing."""

    release: DeezerRelease
    local_artist: str
    release_type: ReleaseType
    ownership: Ownership
    reason: str = ""
    traits: tuple[str, ...] = ()

    @property
    def date(self) -> ReleaseDate:
        return self.release.release_date

    @property
    def sort_key(self):
        # Descending by date; ties broken deterministically so runs are stable.
        return (
            self.date.sort_key,
            _reverse_str(self.local_artist.casefold()),
            _reverse_str(self.release.title.casefold()),
        )


class _ReverseStr:
    """Inverts string ordering so it can sit inside a reverse=True sort."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ReverseStr) and other.value == self.value

    def __lt__(self, other: _ReverseStr) -> bool:
        return self.value > other.value


def _reverse_str(value: str) -> _ReverseStr:
    return _ReverseStr(value)


@dataclass
class UnresolvedArtist:
    name: str
    reason: str
    candidates: list[DeezerArtist] = field(default_factory=list)


@dataclass
class ReviewItem:
    """An ambiguous release: shown separately, never in the main list."""

    release: DeezerRelease
    local_artist: str
    release_type: ReleaseType
    reason: str
