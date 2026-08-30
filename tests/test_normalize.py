"""Artist and title normalization."""

from __future__ import annotations

import pytest

from dropwatch.normalize import (
    credited_to,
    artist_key_variants,
    durations_match,
    fold,
    parse_title,
    title_similarity,
    track_key,
)


class TestFold:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("Björk", "Bjork"),
            ("BEYONCÉ", "beyonce"),
            ("Sigur Rós", "sigur ros"),
            ("Don't Stop", "Dont Stop"),
            ("Don’t Stop", "Don't Stop"),  # curly vs straight apostrophe
            ("Simon & Garfunkel", "Simon and Garfunkel"),
            ("Florence + the Machine", "Florence and the Machine"),
            ("A  B", "a b"),  # whitespace collapse
            ("Godspeed You! Black Emperor", "Godspeed You Black Emperor"),
            ("Nine Inch Nails ", "nine inch nails"),
        ],
    )
    def test_equivalent_forms_fold_together(self, a, b):
        assert fold(a) == fold(b)

    def test_distinct_names_stay_distinct(self):
        assert fold("The Beatles") != fold("The Beach Boys")

    def test_non_latin_script_is_preserved(self):
        assert fold("東京事変") == "東京事変"


class TestArtistKeyVariants:
    def test_leading_article_variant(self):
        assert "beatles" in artist_key_variants("The Beatles")
        assert "the beatles" in artist_key_variants("The Beatles")

    def test_compact_variant_matches_punctuated_name(self):
        assert artist_key_variants("AC/DC") & artist_key_variants("ACDC")

    def test_names_containing_conjunctions_are_never_split(self):
        # These must survive whole; splitting invents artists that don't exist.
        for name in [
            "Earth, Wind & Fire",
            "Florence + the Machine",
            "Nick Cave and the Bad Seeds",
            "Hall & Oates",
            "Emerson, Lake & Palmer",
        ]:
            variants = artist_key_variants(name)
            assert any(len(v.split()) >= 3 for v in variants), name


class TestParseTitleEditions:
    """Cosmetic markers must not change a release's identity."""

    @pytest.mark.parametrize(
        "decorated",
        [
            "Kid A (Deluxe Edition)",
            "Kid A (Super Deluxe)",
            "Kid A (Expanded Edition)",
            "Kid A (Remastered)",
            "Kid A (2011 Remaster)",
            "Kid A (Remastered 2011)",
            "Kid A - 2011 Remaster",
            "Kid A (Bonus Track Version)",
            "Kid A (Special Edition)",
            "Kid A (Explicit)",
            "Kid A (Clean)",
            "Kid A (20th Anniversary Edition)",
            "Kid A [Deluxe Edition]",
            "Kid A (Japanese Edition)",
            "Kid A (Reissue)",
        ],
    )
    def test_edition_markers_are_stripped(self, decorated):
        assert parse_title(decorated).base == parse_title("Kid A").base

    def test_edition_markers_are_recorded(self):
        parsed = parse_title("Kid A (Deluxe Edition)")
        assert parsed.editions
        assert not parsed.versions


class TestParseTitleVersions:
    """Meaningful markers must keep releases apart."""

    @pytest.mark.parametrize(
        "decorated",
        [
            "Kid A (Live)",
            "Kid A (Acoustic)",
            "Kid A (Remix)",
            "Kid A (Radio Edit)",
            "Kid A (Instrumental)",
            "Kid A (Demo)",
            "Kid A (Alternate Take)",
            "Kid A (Mono)",
            "Kid A (Stereo)",
            "Kid A (Spanish Version)",
            "Kid A - Live",
            "Kid A (Live at Wembley)",
        ],
    )
    def test_version_markers_are_not_stripped_into_equality(self, decorated):
        parsed = parse_title(decorated)
        plain = parse_title("Kid A")
        assert parsed.versions, f"{decorated} should carry a version marker"
        assert not parsed.same_recording_as(plain)

    def test_live_variants_collapse_to_one_version(self):
        a = parse_title("Kid A (Live at Wembley)")
        b = parse_title("Kid A (Live in Paris)")
        assert a.same_recording_as(b)

    def test_deluxe_live_album_keeps_live_but_drops_deluxe(self):
        parsed = parse_title("Kid A (Live) [Deluxe Edition]")
        assert parsed.versions == {"live"}
        assert parsed.base == parse_title("Kid A").base

    def test_unrecognised_suffix_stays_in_the_base_title(self):
        # Unknown suffixes must make titles differ rather than silently merge.
        assert parse_title("Kid A (Rarities)").base != parse_title("Kid A").base


class TestTraits:
    @pytest.mark.parametrize(
        "title,trait",
        [
            ("Live at Leeds", "live"),
            ("Greatest Hits", "compilation"),
            ("The Best Of", "compilation"),
            ("Remixes", "remix"),
            ("Trainspotting (Original Soundtrack)", "soundtrack"),
            ("Thriller (Deluxe)", "deluxe"),
            ("Purple Rain (Remastered)", "remaster"),
            ("Songs (Karaoke Version)", "karaoke"),
        ],
    )
    def test_secondary_traits_detected(self, title, trait):
        assert trait in parse_title(title).traits


class TestTrackKey:
    def test_version_in_separate_field_matches_version_in_title(self):
        assert track_key("Song", "Live") == track_key("Song (Live)")

    def test_plain_and_live_differ(self):
        assert track_key("Song") != track_key("Song (Live)")

    def test_remaster_suffix_is_cosmetic_for_tracks(self):
        assert track_key("Song (2011 Remaster)") == track_key("Song")


class TestCreditedTo:
    variants = {"ghost", "test artist"}

    def test_exact_credit(self):
        assert credited_to("Ghost", self.variants)

    def test_credit_inside_a_featured_string(self):
        assert credited_to("Ghost feat. Tobias Forge", self.variants)
        assert credited_to("Someone Else feat. Test Artist", self.variants)

    def test_a_longer_name_is_not_a_match(self):
        # The whole point of matching tokens rather than substrings.
        assert not credited_to("Ghostface Killah", self.variants)

    def test_punctuation_and_case_are_folded(self):
        assert credited_to("GHOST / Tobias", self.variants)

    def test_empty_inputs(self):
        assert not credited_to("", self.variants)
        assert not credited_to("Ghost", set())


class TestSimilarity:
    def test_identical_is_one(self):
        assert title_similarity("kid a", "kid a") == 1.0

    def test_unrelated_is_low(self):
        assert title_similarity("kid a", "nevermind") < 0.5


class TestDurations:
    def test_within_tolerance(self):
        assert durations_match(180, 183) is True

    def test_outside_tolerance(self):
        assert durations_match(180, 240) is False

    def test_unknown_returns_none(self):
        # None means "cannot tell", which callers treat differently from False.
        assert durations_match(180, None) is None
        assert durations_match(None, 180) is None
        assert durations_match(0, 180) is None
