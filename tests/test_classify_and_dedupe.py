"""Release classification and duplicate collapsing."""

from __future__ import annotations

from conftest import deezer_release

from dropwatch.classify import classify_release, resolve_type
from dropwatch.dedupe import deduplicate
from dropwatch.models import ReleaseType


def sized(record_type, count, seconds_each=210, **kwargs):
    tracks = [(f"Track {i}", seconds_each) for i in range(count)]
    return deezer_release("Some Release", record_type=record_type, tracks=tracks, **kwargs)


class TestClassification:
    def test_declared_types_are_trusted_when_shape_agrees(self):
        assert resolve_type(sized("album", 10))[0] is ReleaseType.ALBUM
        assert resolve_type(sized("ep", 5))[0] is ReleaseType.EP
        assert resolve_type(sized("single", 1))[0] is ReleaseType.SINGLE

    def test_compilation_maps_to_album_and_records_the_trait(self):
        release = sized("compilation", 20)
        assert resolve_type(release)[0] is ReleaseType.ALBUM
        assert "compilation" in classify_release(release).traits

    def test_single_with_album_length_is_corrected(self):
        # Deezer mislabels full albums as singles often enough to matter.
        release = sized("single", 12)
        assert resolve_type(release)[0] is ReleaseType.ALBUM
        assert classify_release(release).source == "corrected"

    def test_album_with_one_short_track_is_corrected_to_single(self):
        assert resolve_type(sized("album", 1, seconds_each=200))[0] is ReleaseType.SINGLE

    def test_album_with_five_tracks_is_corrected_to_ep(self):
        assert resolve_type(sized("album", 5, seconds_each=200))[0] is ReleaseType.EP

    def test_few_but_very_long_tracks_stay_an_album(self):
        # Three 20-minute pieces are a long-player, not a single.
        assert resolve_type(sized("album", 3, seconds_each=1200))[0] is ReleaseType.ALBUM

    def test_no_detail_falls_back_to_the_declared_type(self):
        release = deezer_release("X", record_type="ep")
        classification = classify_release(release)
        assert classification.type is ReleaseType.EP
        assert classification.source == "declared"

    def test_no_detail_and_no_declared_type_is_unknown(self):
        release = deezer_release("X", record_type="")
        assert resolve_type(release)[0] is ReleaseType.UNKNOWN

    def test_confidence_is_higher_when_signals_agree(self):
        agree = classify_release(sized("album", 10))
        corrected = classify_release(sized("single", 12))
        assert agree.confidence > corrected.confidence


class TestDeduplicate:
    def test_identical_upc_collapses(self):
        a = deezer_release("Album", release_id="1", upc="123", date="2020-01-01")
        b = deezer_release("Album (Explicit)", release_id="2", upc="123", date="2020-05-01")
        canonical, dupes = deduplicate([a, b])
        assert len(canonical) == 1
        assert dupes[canonical[0].id]

    def test_territorial_duplicates_collapse_to_the_earliest(self):
        a = deezer_release("Album", release_id="1", date="2020-01-01")
        b = deezer_release("Album", release_id="2", date="2020-03-01")
        c = deezer_release("Album", release_id="3", date="2021-08-01")
        canonical, _ = deduplicate([a, b, c])
        assert len(canonical) == 1
        assert canonical[0].id == "1"

    def test_explicit_and_clean_pair_collapses(self):
        canonical, _ = deduplicate(
            [
                deezer_release("Album", release_id="1", date="2020-01-01"),
                deezer_release("Album (Clean)", release_id="2", date="2020-01-01"),
            ]
        )
        assert len(canonical) == 1

    def test_remaster_collapses_into_the_original(self):
        canonical, _ = deduplicate(
            [
                deezer_release("Album", release_id="1", date="1995-01-01"),
                deezer_release("Album (2015 Remaster)", release_id="2", date="2015-01-01"),
            ]
        )
        assert len(canonical) == 1
        assert canonical[0].id == "1"

    def test_live_album_is_never_merged_into_the_studio_album(self):
        canonical, _ = deduplicate(
            [
                deezer_release("Album", release_id="1", date="2020-01-01"),
                deezer_release("Album (Live)", release_id="2", date="2021-01-01"),
            ]
        )
        assert len(canonical) == 2

    def test_single_is_not_merged_into_the_album_of_the_same_name(self):
        canonical, _ = deduplicate(
            [
                deezer_release("Hurt", release_id="1", date="2020-01-01", record_type="album"),
                deezer_release("Hurt", release_id="2", date="2020-01-01", record_type="single"),
            ]
        )
        assert len(canonical) == 2

    def test_different_artists_are_never_merged(self):
        canonical, _ = deduplicate(
            [
                deezer_release("Album", release_id="1", artist_id="100"),
                deezer_release("Album", release_id="2", artist_id="200"),
            ]
        )
        assert len(canonical) == 2

    def test_result_is_deterministic(self):
        releases = [
            deezer_release("Album", release_id=str(i), date="2020-01-01") for i in range(5)
        ]
        first, _ = deduplicate(releases)
        second, _ = deduplicate(list(reversed(releases)))
        assert [r.id for r in first] == [r.id for r in second]
