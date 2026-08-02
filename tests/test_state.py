"""Local state: cache expiry, invalidation and mappings."""

from __future__ import annotations

import time

from dropwatch.state import MappingTarget, STATUS_BLOCKED, Store


class TestCache:
    def test_roundtrip(self, store):
        store.set("k", {"a": 1})
        assert store.get("k") == {"a": 1}

    def test_missing_key_is_none(self, store):
        assert store.get("nope") is None

    def test_expired_entries_are_ignored(self, tmp_path):
        with Store(tmp_path / "s.sqlite3", max_age_hours=1.0) as store:
            store.set("k", {"a": 1})
            # Backdate the entry past the expiry window.
            store._conn.execute(
                "UPDATE cache SET fetched_at = ?", (time.time() - 7200,)
            )
            store._conn.commit()
            assert store.get("k") is None

    def test_fresh_entries_within_the_window_are_served(self, tmp_path):
        with Store(tmp_path / "s.sqlite3", max_age_hours=2.0) as store:
            store.set("k", {"a": 1})
            store._conn.execute("UPDATE cache SET fetched_at = ?", (time.time() - 3000,))
            store._conn.commit()
            assert store.get("k") == {"a": 1}

    def test_zero_max_age_disables_expiry(self, tmp_path):
        with Store(tmp_path / "s.sqlite3", max_age_hours=0) as store:
            store.set("k", 1)
            store._conn.execute("UPDATE cache SET fetched_at = ?", (0,))
            store._conn.commit()
            assert store.get("k") == 1

    def test_clear_cache_keeps_mappings(self, store):
        store.set("k", 1)
        store.set_mapping("Artist", [MappingTarget("1", "Artist")])
        store.clear_cache()
        assert store.get("k") is None
        assert store.get_mapping("Artist") is not None

    def test_cache_age_is_reported(self, store):
        assert store.cache_age_hours() is None
        store.set("k", 1)
        assert 0 <= store.cache_age_hours() < 1

    def test_overwrites_refresh_the_timestamp(self, store):
        store.set("k", 1)
        store._conn.execute("UPDATE cache SET fetched_at = ?", (0,))
        store._conn.commit()
        store.set("k", 2)
        assert store.get("k") == 2


class TestLocalTrackCache:
    def test_fingerprint_mismatch_invalidates(self, store):
        store.set_local_tracks("a1", "10:2700", [{"title": "T"}])
        assert store.get_local_tracks("a1", "10:2700") == [{"title": "T"}]
        # The album gained a track, so the cached list must not be reused.
        assert store.get_local_tracks("a1", "11:2900") is None

    def test_cleared_with_the_cache(self, store):
        store.set_local_tracks("a1", "fp", [{"title": "T"}])
        store.clear_cache()
        assert store.get_local_tracks("a1", "fp") is None


class TestMappings:
    def test_set_and_get(self, store):
        store.set_mapping("Ghost", [MappingTarget("42", "Ghost")])
        mapping = store.get_mapping("Ghost")
        assert mapping.deezer_ids == ["42"]
        assert mapping.status == "confirmed"

    def test_block(self, store):
        store.block_artist("Skip")
        assert store.get_mapping("Skip").status == STATUS_BLOCKED
        assert store.get_mapping("Skip").is_blocked

    def test_update_replaces(self, store):
        store.set_mapping("Ghost", [MappingTarget("1", "Ghost A")])
        store.set_mapping("Ghost", [MappingTarget("2", "Ghost B")])
        assert store.get_mapping("Ghost").deezer_ids == ["2"]
        assert len(store.list_mappings()) == 1

    def test_reset_removes_all(self, store):
        store.set_mapping("A", [MappingTarget("1", "A")])
        store.set_mapping("B", [MappingTarget("2", "B")])
        assert store.reset_mappings() == 2
        assert store.list_mappings() == []

    def test_delete_missing_returns_false(self, store):
        assert store.delete_mapping("Nobody") is False


class TestReleaseDecisions:
    """The review section has to be actionable, or it repeats forever."""

    def test_owned_decision_roundtrip(self, store):
        from dropwatch.models import DECISION_OWNED

        store.set_release_decision("123", DECISION_OWNED, "have it")
        assert store.release_decisions() == {"123": DECISION_OWNED}

    def test_missing_decision_roundtrip(self, store):
        from dropwatch.models import DECISION_MISSING

        store.set_release_decision("456", DECISION_MISSING)
        assert store.release_decisions() == {"456": DECISION_MISSING}

    def test_decision_can_be_changed(self, store):
        from dropwatch.models import DECISION_MISSING, DECISION_OWNED

        store.set_release_decision("1", DECISION_OWNED)
        store.set_release_decision("1", DECISION_MISSING)
        assert store.release_decisions() == {"1": DECISION_MISSING}

    def test_decision_can_be_cleared(self, store):
        from dropwatch.models import DECISION_OWNED

        store.set_release_decision("1", DECISION_OWNED)
        assert store.clear_release_decision("1") is True
        assert store.release_decisions() == {}

    def test_unknown_decision_is_rejected(self, store):
        import pytest

        with pytest.raises(ValueError):
            store.set_release_decision("1", "maybe")

    def test_reset(self, store):
        from dropwatch.models import DECISION_OWNED

        store.set_release_decision("1", DECISION_OWNED)
        store.set_release_decision("2", DECISION_OWNED)
        assert store.reset_release_decisions() == 2
        assert store.count_release_decisions() == 0


class TestPersistence:
    def test_state_survives_reopening(self, tmp_path):
        path = tmp_path / "s.sqlite3"
        with Store(path) as store:
            store.set_mapping("Ghost", [MappingTarget("42", "Ghost")])
        with Store(path) as store:
            assert store.get_mapping("Ghost").deezer_ids == ["42"]

    def test_state_file_is_private(self, tmp_path):
        path = tmp_path / "s.sqlite3"
        with Store(path):
            pass
        assert path.stat().st_mode & 0o077 == 0

    def test_unresolved_report_ignores_expiry(self, tmp_path):
        with Store(tmp_path / "s.sqlite3", max_age_hours=1.0) as store:
            store.save_unresolved([{"name": "X", "reason": "no match", "candidates": []}])
            store._conn.execute("UPDATE cache SET fetched_at = ?", (0,))
            store._conn.commit()
            # The unresolved list is a report, not a cache entry.
            assert store.load_unresolved()[0]["name"] == "X"


class TestExpiryClasses:
    """Album detail is immutable; discography listings exist to change."""

    def _age(self, store, key, hours):
        store._conn.execute(
            "UPDATE cache SET fetched_at = ? WHERE key = ?",
            (time.time() - hours * 3600, key),
        )
        store._conn.commit()

    def test_album_detail_survives_the_normal_ttl(self, tmp_path):
        with Store(tmp_path / "s.sqlite3", max_age_hours=24.0) as store:
            store.set("album:302127", {"title": "Discovery"})
            store.set("album_tracks:302127", [{"title": "One More Time"}])
            self._age(store, "album:302127", 72)
            self._age(store, "album_tracks:302127", 72)
            assert store.get("album:302127") is not None
            assert store.get("album_tracks:302127") is not None

    def test_discography_still_expires_on_the_normal_ttl(self, tmp_path):
        with Store(tmp_path / "s.sqlite3", max_age_hours=24.0) as store:
            store.set("discography:27", [{"id": 1}])
            store.set("search:artist:daft punk:25", {"data": []})
            self._age(store, "discography:27", 72)
            self._age(store, "search:artist:daft punk:25", 72)
            assert store.get("discography:27") is None
            assert store.get("search:artist:daft punk:25") is None

    def test_stable_entries_are_bounded_not_immortal(self, tmp_path):
        # Catalogue corrections should still land eventually.
        with Store(tmp_path / "s.sqlite3", max_age_hours=24.0) as store:
            store.set("album:1", {"x": 1})
            self._age(store, "album:1", 24 * 40)
            assert store.get("album:1") is None

    def test_disabled_expiry_stays_disabled_for_every_class(self, tmp_path):
        with Store(tmp_path / "s.sqlite3", max_age_hours=0) as store:
            store.set("album:1", {"x": 1})
            store.set("discography:1", [{"x": 1}])
            self._age(store, "album:1", 24 * 400)
            self._age(store, "discography:1", 24 * 400)
            assert store.get("album:1") is not None
            assert store.get("discography:1") is not None

    def test_a_longer_configured_ttl_is_respected(self, tmp_path):
        with Store(tmp_path / "s.sqlite3", max_age_hours=24 * 90) as store:
            store.set("album:1", {"x": 1})
            self._age(store, "album:1", 24 * 60)
            assert store.get("album:1") is not None, "config must not shorten it"

    def test_stats_split_by_class(self, tmp_path):
        with Store(tmp_path / "s.sqlite3") as store:
            store.set("album:1", {})
            store.set("album_tracks:1", [])
            store.set("discography:1", [])
            stats = store.cache_stats()
            assert stats == {"total": 3, "stable": 2, "volatile": 1}


class TestMultipleTargets:
    """One local artist may map to several Deezer ids."""

    def test_order_is_preserved(self, store):
        store.set_mapping(
            "Ghost",
            [MappingTarget("1160651", "Ghost"), MappingTarget("4859761", "Ghost")],
        )
        assert store.get_mapping("Ghost").deezer_ids == ["1160651", "4859761"]

    def test_replacing_drops_the_old_targets(self, store):
        store.set_mapping("Ghost", [MappingTarget("1", "A"), MappingTarget("2", "B")])
        store.set_mapping("Ghost", [MappingTarget("3", "C")])
        assert store.get_mapping("Ghost").deezer_ids == ["3"]

    def test_clear_returns_a_multi_mapped_artist_to_unresolved(self, store):
        store.set_mapping("Ghost", [MappingTarget("1", "A"), MappingTarget("2", "B")])
        assert store.clear_mapping("Ghost") is True
        assert store.get_mapping("Ghost") is None

    def test_clear_also_lifts_a_block(self, store):
        store.block_artist("Skip Me")
        assert store.get_mapping("Skip Me").is_blocked
        assert store.clear_mapping("Skip Me") is True
        assert store.get_mapping("Skip Me") is None

    def test_blocking_keeps_any_existing_targets(self, store):
        # The status is what suppresses the artist; keeping the ids means a
        # Deezer id still names it, so `unblock <same id>` finds it again.
        store.set_mapping("Ghost", [MappingTarget("1", "A")])
        store.block_artist("Ghost")
        mapping = store.get_mapping("Ghost")
        assert mapping.is_blocked
        assert mapping.deezer_ids == ["1"]
        assert mapping.describe() == "(blocked)"

    def test_blocking_an_unknown_artist_needs_no_targets(self, store):
        store.block_artist("Karaoke Hits Vol 3")
        mapping = store.get_mapping("Karaoke Hits Vol 3")
        assert mapping.is_blocked
        assert mapping.deezer_ids == []

    def test_mappings_for_deezer_id_is_the_reverse_lookup(self, store):
        store.set_mapping("Ghost", [MappingTarget("1", "A")])
        store.set_mapping("Ghost B.C.", [MappingTarget("1", "A")])
        store.set_mapping("Other", [MappingTarget("2", "B")])
        assert [m.local_name for m in store.mappings_for_deezer_id("1")] == [
            "Ghost",
            "Ghost B.C.",
        ]
        assert store.mappings_for_deezer_id("999") == []

    def test_reset_clears_targets_too(self, store):
        store.set_mapping("A", [MappingTarget("1", "A"), MappingTarget("2", "A2")])
        store.reset_mappings()
        with store._lock:
            rows = store._conn.execute("SELECT COUNT(*) c FROM artist_mapping_target").fetchone()
        assert rows["c"] == 0

    def test_describe_lists_every_target(self, store):
        store.set_mapping("Ghost", [MappingTarget("1", "Ghost"), MappingTarget("2", "Ghost")])
        text = store.get_mapping("Ghost").describe()
        assert "[1]" in text and "[2]" in text


class TestSchemaMigration:
    def _v1_database(self, path):
        import sqlite3

        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                fetched_at REAL NOT NULL);
            CREATE TABLE artist_mapping (local_key TEXT PRIMARY KEY,
                local_name TEXT NOT NULL, deezer_id TEXT, deezer_name TEXT,
                status TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE ignored_release (deezer_id TEXT PRIMARY KEY, note TEXT,
                created_at REAL NOT NULL);
            CREATE TABLE local_tracks (album_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL, fetched_at REAL NOT NULL);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        conn.execute(
            "INSERT INTO artist_mapping VALUES('bjork','Björk','630','Björk','confirmed',0)"
        )
        conn.execute(
            "INSERT INTO artist_mapping VALUES('skip me','Skip Me',NULL,NULL,'ignored',0)"
        )
        conn.commit()
        conn.close()

    def test_single_id_mappings_are_carried_forward(self, tmp_path):
        path = tmp_path / "old.sqlite3"
        self._v1_database(path)
        with Store(path) as store:
            assert store.get_mapping("Björk").deezer_ids == ["630"]

    def test_ignored_entries_survive(self, tmp_path):
        path = tmp_path / "old.sqlite3"
        self._v1_database(path)
        with Store(path) as store:
            assert store.get_mapping("Skip Me").is_blocked

    def test_migration_is_idempotent(self, tmp_path):
        path = tmp_path / "old.sqlite3"
        self._v1_database(path)
        with Store(path):
            pass
        with Store(path) as store:
            assert store.get_mapping("Björk").deezer_ids == ["630"]

    def test_version_is_recorded(self, tmp_path):
        from dropwatch.state import SCHEMA_VERSION

        path = tmp_path / "old.sqlite3"
        self._v1_database(path)
        with Store(path) as store:
            row = store._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        assert int(row["value"]) == SCHEMA_VERSION
