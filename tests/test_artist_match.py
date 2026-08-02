"""Artist resolution: ambiguity, manual mappings and misleading search results."""

from __future__ import annotations

from conftest import deezer_release, local_album, local_artist

from dropwatch.artist_match import ArtistResolver, Resolution, name_score
from dropwatch.errors import DeezerUnavailableError
from dropwatch.models import DeezerArtist
from dropwatch.state import MappingTarget


class StubProvider:
    """Minimal DeezerProvider stand-in driven by in-memory tables."""

    def __init__(self, search=None, discographies=None, fail=False):
        self.search_results = search or {}
        self.discographies = discographies or {}
        self.fail = fail
        self.discography_calls: list[str] = []

    def search_artists(self, name, limit=25):
        if self.fail:
            raise DeezerUnavailableError("Deezer is down")
        return list(self.search_results.get(name, []))

    def get_artist(self, artist_id):
        for results in self.search_results.values():
            for artist in results:
                if artist.id == artist_id:
                    return artist
        return None

    def get_discography(self, artist_id):
        self.discography_calls.append(artist_id)
        return list(self.discographies.get(artist_id, []))


def artist(id_, name, fans=1000):
    return DeezerArtist(id=id_, name=name, nb_fan=fans)


class TestNameScore:
    def test_exact_match_scores_one(self):
        assert name_score("Björk", "Bjork") == 1.0

    def test_article_variant_scores_high(self):
        assert name_score("The Beatles", "Beatles") >= 0.9

    def test_superset_name_is_not_treated_as_the_same_artist(self):
        # This is the real Deezer failure mode: searching "Björk" ranks
        # "Björk & Toffe" first.
        assert name_score("Björk", "Björk & Toffe") < 1.0

    def test_unrelated_names_score_low(self):
        assert name_score("Radiohead", "Coldplay") < 0.85


class TestResolution:
    def test_first_search_result_is_not_blindly_accepted(self, store):
        local = local_artist("Björk", [local_album("Homogenic", "Björk", 1997)])
        provider = StubProvider(
            search={"Björk": [artist("804103", "Björk & Toffe", 31), artist("630", "Björk", 900000)]},
            discographies={"630": [deezer_release("Homogenic", artist_id="630")]},
        )
        resolution = ArtistResolver(provider, store).resolve(local)
        assert resolution.ok
        assert resolution.artist.id == "630"

    def test_shared_album_titles_disambiguate_identical_names(self, store):
        local = local_artist("Nirvana", [local_album("Nevermind", "Nirvana", 1991)])
        provider = StubProvider(
            search={"Nirvana": [artist("1", "Nirvana", 50), artist("2", "Nirvana", 900000)]},
            discographies={
                "1": [deezer_release("Nevermind", artist_id="1")],
                "2": [deezer_release("Something Else", artist_id="2")],
            },
        )
        resolution = ArtistResolver(provider, store).resolve(local)
        assert resolution.ok
        # The less popular artist wins because the catalogue actually matches.
        assert resolution.artist.id == "1"

    def test_same_name_no_overlap_is_ambiguous(self, store):
        local = local_artist("Ghost", [local_album("Unknown Album", "Ghost", 2015)])
        provider = StubProvider(
            search={"Ghost": [artist("1", "Ghost"), artist("2", "Ghost")]},
            discographies={"1": [deezer_release("A", artist_id="1")], "2": []},
        )
        resolution = ArtistResolver(provider, store).resolve(local)
        assert resolution.status is Resolution.AMBIGUOUS
        assert len(resolution.candidates) >= 2

    def test_no_similar_name_is_not_found(self, store):
        local = local_artist("Obscure Local Band")
        provider = StubProvider(search={"Obscure Local Band": [artist("9", "Something Else")]})
        resolution = ArtistResolver(provider, store).resolve(local)
        assert resolution.status is Resolution.NOT_FOUND

    def test_karaoke_and_tribute_acts_are_rejected(self, store):
        local = local_artist("Adele", [local_album("25", "Adele", 2015)])
        provider = StubProvider(
            search={
                "Adele": [
                    artist("1", "Adele Karaoke", 10),
                    artist("2", "Tribute to Adele", 5),
                ]
            }
        )
        resolution = ArtistResolver(provider, store).resolve(local)
        assert resolution.status is Resolution.NOT_FOUND

    def test_various_artists_is_skipped(self, store):
        resolution = ArtistResolver(StubProvider(), store).resolve(local_artist("Various Artists"))
        assert resolution.status is Resolution.IGNORED

    def test_lone_exact_match_without_overlap_resolves_at_low_confidence(self, store):
        local = local_artist("Tiny Band", [local_album("Rare Album", "Tiny Band", 2019)])
        provider = StubProvider(
            search={"Tiny Band": [artist("7", "Tiny Band")]},
            discographies={"7": [deezer_release("Different Album", artist_id="7")]},
        )
        resolution = ArtistResolver(provider, store).resolve(local)
        assert resolution.ok
        assert resolution.confidence < 0.7

    def test_deezer_failure_reports_error_not_missing_artist(self, store):
        resolution = ArtistResolver(StubProvider(fail=True), store).resolve(
            local_artist("Anyone")
        )
        assert resolution.status is Resolution.ERROR


class TestManualMappings:
    def test_mapping_overrides_search_entirely(self, store):
        store.set_mapping("Ghost", [MappingTarget("42", "Ghost (Sweden)")])
        provider = StubProvider(
            search={"Ghost": [artist("1", "Ghost"), artist("2", "Ghost")]},
            discographies={"42": [deezer_release("Meliora", artist_id="42")]},
        )
        provider.search_results["Ghost"].append(artist("42", "Ghost (Sweden)"))
        resolution = ArtistResolver(provider, store).resolve(local_artist("Ghost"))
        assert resolution.ok
        assert resolution.artist.id == "42"
        assert resolution.confidence == 1.0
        assert resolution.reason == "manual mapping"

    def test_ignored_artist_is_skipped(self, store):
        store.block_artist("Skip Me")
        resolution = ArtistResolver(StubProvider(), store).resolve(local_artist("Skip Me"))
        assert resolution.status is Resolution.IGNORED

    def test_mapping_lookup_is_case_and_accent_insensitive(self, store):
        store.set_mapping("Björk", [MappingTarget("630", "Björk")])
        assert store.get_mapping("bjork") is not None
        assert store.get_mapping("BJÖRK") is not None

    def test_reset_removes_the_mapping(self, store):
        store.set_mapping("Ghost", [MappingTarget("42", "Ghost")])
        assert store.delete_mapping("Ghost") is True
        assert store.get_mapping("Ghost") is None

    def test_unresolved_artists_do_not_block_others(self, store):
        provider = StubProvider(
            search={
                "Good": [artist("1", "Good")],
                "Bad": [],
            },
            discographies={"1": [deezer_release("Album", artist_id="1")]},
        )
        resolver = ArtistResolver(provider, store)
        good = resolver.resolve(local_artist("Good", [local_album("Album", "Good")]))
        bad = resolver.resolve(local_artist("Bad"))
        assert good.ok
        assert not bad.ok
