"""Ownership determination, including the singles rules."""

from __future__ import annotations

from conftest import deezer_release, local_album, local_artist

from dropwatch.models import Ownership, ReleaseType
from dropwatch.release_match import (
    IsrcIndex,
    LocalIndex,
    compute_coverage,
    determine_ownership,
    refine_with_isrc,
)

ALBUM_TRACKS = [
    ("Everything In Its Right Place", 251),
    ("Kid A", 267),
    ("The National Anthem", 351),
    ("How to Disappear Completely", 356),
    ("Treefingers", 222),
    ("Optimistic", 315),
    ("In Limbo", 191),
    ("Idioteque", 289),
]


def index_with(*albums):
    return LocalIndex(local_artist("Radiohead", list(albums)))


class TestTitleMatching:
    def test_exact_title_and_track_count_is_owned(self):
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        release = deezer_release("Kid A", date="2000-10-02", tracks=ALBUM_TRACKS)
        verdict = determine_ownership(release, index, ReleaseType.ALBUM)
        assert verdict.ownership is Ownership.OWNED

    def test_remaster_of_an_owned_album_is_owned(self):
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        release = deezer_release(
            "Kid A (2011 Remaster)", date="2011-05-01", tracks=ALBUM_TRACKS
        )
        assert determine_ownership(release, index, ReleaseType.ALBUM).ownership is Ownership.OWNED

    def test_deluxe_edition_with_only_owned_extras_is_owned(self):
        # Every bonus track already exists elsewhere in the library.
        extras = [("Fog", 200), ("Cuttooth", 300)]
        index = index_with(
            local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS),
            local_album("Amnesiac B-Sides", "Radiohead", 2001, extras),
        )
        release = deezer_release(
            "Kid A (Deluxe Edition)", date="2009-01-01", tracks=ALBUM_TRACKS + extras
        )
        verdict = determine_ownership(release, index, ReleaseType.ALBUM)
        assert verdict.ownership is Ownership.OWNED

    def test_deluxe_edition_with_unowned_extras_is_reported(self):
        extras = [("Untitled Bonus", 200), ("Another Bonus", 300)]
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        release = deezer_release(
            "Kid A (Deluxe Edition)", date="2009-01-01", tracks=ALBUM_TRACKS + extras
        )
        verdict = determine_ownership(release, index, ReleaseType.ALBUM)
        assert verdict.ownership is Ownership.PROBABLY_MISSING
        assert "2 track(s)" in verdict.reason

    def test_live_album_is_not_matched_to_the_studio_album(self):
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        live_tracks = [(t, d + 12) for t, d in ALBUM_TRACKS]
        release = deezer_release(
            "Kid A (Live)", date="2021-01-01", tracks=live_tracks, versions=["Live"] * 8
        )
        verdict = determine_ownership(release, index, ReleaseType.ALBUM)
        assert verdict.ownership in (Ownership.MISSING, Ownership.PROBABLY_MISSING)

    def test_same_title_very_different_year_is_ambiguous(self):
        # Could be a re-recording rather than the album already owned.
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        release = deezer_release("Kid A", date="2019-01-01", tracks=ALBUM_TRACKS)
        assert determine_ownership(release, index, ReleaseType.ALBUM).ownership is Ownership.AMBIGUOUS

    def test_near_miss_title_is_ambiguous_not_missing(self):
        index = index_with(local_album("In Rainbows", "Radiohead", 2007, ALBUM_TRACKS))
        release = deezer_release("In Rainbow", date="2007-10-10", tracks=ALBUM_TRACKS)
        verdict = determine_ownership(release, index, ReleaseType.ALBUM)
        assert verdict.ownership is Ownership.AMBIGUOUS

    def test_unrelated_album_is_missing(self):
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        release = deezer_release(
            "Hail to the Thief", date="2003-06-09", tracks=[("2+2=5", 200)]
        )
        assert determine_ownership(release, index, ReleaseType.ALBUM).ownership is Ownership.MISSING


class TestSingles:
    def test_advance_single_already_on_an_owned_album_is_not_reported(self):
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        single = deezer_release(
            "Optimistic", release_id="s1", date="2000-08-01", record_type="single",
            tracks=[("Optimistic", 315)],
        )
        verdict = determine_ownership(single, index, ReleaseType.SINGLE)
        assert verdict.ownership is Ownership.OWNED
        assert not verdict.reportable

    def test_single_with_an_exclusive_b_side_is_reported(self):
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        single = deezer_release(
            "Optimistic", release_id="s2", date="2000-08-01", record_type="single",
            tracks=[("Optimistic", 315), ("Cuttooth", 320)],
        )
        verdict = determine_ownership(single, index, ReleaseType.SINGLE)
        assert verdict.ownership is Ownership.PROBABLY_MISSING
        assert "exclusive" in verdict.reason

    def test_genuinely_new_standalone_single_is_missing(self):
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        single = deezer_release(
            "Brand New Song", release_id="s3", date="2026-01-01", record_type="single",
            tracks=[("Brand New Song", 200)],
        )
        assert determine_ownership(single, index, ReleaseType.SINGLE).ownership is Ownership.MISSING

    def test_alternate_version_of_an_owned_track_is_reported(self):
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        single = deezer_release(
            "Optimistic (Acoustic)", release_id="s4", date="2001-01-01",
            record_type="single", tracks=[("Optimistic", 280)], versions=["Acoustic"],
        )
        verdict = determine_ownership(single, index, ReleaseType.SINGLE)
        assert verdict.reportable

    def test_same_title_and_version_but_different_length_is_reported(self):
        # A materially different edit of a track that is otherwise owned.
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        single = deezer_release(
            "Idioteque", release_id="s5", date="2000-11-01", record_type="single",
            tracks=[("Idioteque", 420)],
        )
        verdict = determine_ownership(single, index, ReleaseType.SINGLE)
        assert verdict.ownership is Ownership.PROBABLY_MISSING
        assert "differ" in verdict.reason

    def test_single_without_track_detail_goes_to_review(self):
        # Recording identity cannot be established, so it must not be asserted.
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        single = deezer_release(
            "Mystery Single", release_id="s6", date="2005-01-01", record_type="single"
        )
        verdict = determine_ownership(single, index, ReleaseType.SINGLE)
        assert verdict.ownership is Ownership.AMBIGUOUS

    def test_remix_package_of_owned_material_is_reported(self):
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        remixes = deezer_release(
            "Idioteque (Remixes)", release_id="s7", date="2001-03-01",
            record_type="single",
            tracks=[("Idioteque", 400), ("Idioteque", 380)],
            versions=["Remix", "Remix"],
        )
        assert determine_ownership(remixes, index, ReleaseType.SINGLE).reportable


class TestCoverage:
    def test_full_coverage_with_durations(self):
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        release = deezer_release("Anything", tracks=ALBUM_TRACKS)
        coverage = compute_coverage(release, index)
        assert coverage.total == 8
        assert coverage.same == 8
        assert coverage.absent == 0

    def test_partial_coverage_counts_absent_tracks(self):
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS[:4]))
        release = deezer_release("Anything", tracks=ALBUM_TRACKS)
        coverage = compute_coverage(release, index)
        assert coverage.same == 4
        assert coverage.absent == 4

    def test_compilation_of_owned_tracks_is_not_reported(self):
        index = index_with(local_album("Kid A", "Radiohead", 2000, ALBUM_TRACKS))
        best_of = deezer_release(
            "Greatest Hits", release_id="c1", date="2015-01-01",
            record_type="compilation", tracks=ALBUM_TRACKS[:5],
        )
        verdict = determine_ownership(best_of, index, ReleaseType.ALBUM)
        assert verdict.ownership is Ownership.OWNED


class TestIsrcRefinement:
    def test_matching_isrcs_suppress_an_advance_single(self):
        owned = IsrcIndex()
        owned.add_release(
            deezer_release(
                "Album", release_id="a1", tracks=[("Track One", 200)],
                isrcs=["GBAAA0000001"],
            )
        )
        single = deezer_release(
            "Track One", release_id="s1", record_type="single",
            tracks=[("Track One", 200)], isrcs=["GBAAA0000001"],
        )
        from dropwatch.release_match import Verdict

        refined = refine_with_isrc(Verdict(Ownership.MISSING, "x"), single, owned)
        assert refined.ownership is Ownership.OWNED
        assert "ISRC" in refined.reason

    def test_different_isrc_leaves_the_verdict_alone(self):
        owned = IsrcIndex()
        owned.add_release(
            deezer_release("Album", release_id="a1", tracks=[("T", 200)], isrcs=["GBAAA0000001"])
        )
        single = deezer_release(
            "Other", release_id="s1", record_type="single",
            tracks=[("Other", 200)], isrcs=["GBAAA0000099"],
        )
        from dropwatch.release_match import Verdict

        refined = refine_with_isrc(Verdict(Ownership.MISSING, "x"), single, owned)
        assert refined.ownership is Ownership.MISSING

    def test_missing_isrcs_are_inconclusive(self):
        owned = IsrcIndex()
        owned.add_release(
            deezer_release("Album", release_id="a1", tracks=[("T", 200)], isrcs=["GBAAA0000001"])
        )
        single = deezer_release(
            "Other", release_id="s1", record_type="single", tracks=[("Other", 200)]
        )
        assert owned.covers(single) is None
