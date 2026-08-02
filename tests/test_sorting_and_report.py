"""Sorting, date precision and terminal formatting."""

from __future__ import annotations

import io

from conftest import deezer_release

from dropwatch.models import (
    DatePrecision,
    MissingRelease,
    Ownership,
    ReleaseDate,
    ReleaseType,
    ReviewItem,
    UnresolvedArtist,
)
from dropwatch.report import (
    GROUP_LABELS,
    build_summary,
    display_width,
    print_results,
    print_review,
    print_summary,
    print_unresolved,
    render_table,
    sort_releases,
)


_ids = iter(range(1, 10_000))


def missing(title, date, artist="Artist", release_type=ReleaseType.ALBUM):
    return MissingRelease(
        release=deezer_release(title, release_id=str(next(_ids)), date=date),
        local_artist=artist,
        release_type=release_type,
        ownership=Ownership.MISSING,
    )


class TestReleaseDate:
    def test_full_date(self):
        d = ReleaseDate.parse("2024-06-15")
        assert (d.year, d.month, d.day) == (2024, 6, 15)
        assert d.precision is DatePrecision.DAY
        assert str(d) == "2024-06-15"

    def test_zero_month_and_day_degrade_to_year(self):
        d = ReleaseDate.parse("2019-00-00")
        assert d.precision is DatePrecision.YEAR
        assert str(d) == "2019"

    def test_zero_day_degrades_to_month(self):
        d = ReleaseDate.parse("2019-07-00")
        assert d.precision is DatePrecision.MONTH
        assert str(d) == "2019-07"

    def test_empty_and_garbage_are_unknown(self):
        for raw in ["", None, "0000-00-00", "not a date", "12"]:
            assert ReleaseDate.parse(raw).precision is DatePrecision.UNKNOWN
            assert str(ReleaseDate.parse(raw)) == "unknown"


class TestSorting:
    def test_strict_reverse_chronological_order(self):
        items = [
            missing("Oldest", "2020-01-01"),
            missing("Newest", "2026-07-24"),
            missing("Middle", "2023-05-05"),
        ]
        assert [i.release.title for i in sort_releases(items)] == [
            "Newest",
            "Middle",
            "Oldest",
        ]

    def test_matches_the_prd_example_ordering(self):
        items = [
            missing("Album Title", "2026-06-02"),
            missing("Release Title", "2026-07-24", release_type=ReleaseType.SINGLE),
            missing("Another Release", "2026-07-18", release_type=ReleaseType.EP),
        ]
        assert [str(i.date) for i in sort_releases(items)] == [
            "2026-07-24",
            "2026-07-18",
            "2026-06-02",
        ]

    def test_less_precise_dates_sort_below_full_dates_in_the_same_period(self):
        items = [
            missing("YearOnly", "2024-00-00"),
            missing("JanFirst", "2024-01-01"),
            missing("MonthOnly", "2024-06-00"),
            missing("JuneFifteen", "2024-06-15"),
        ]
        assert [i.release.title for i in sort_releases(items)] == [
            "JuneFifteen",  # 2024-06-15
            "MonthOnly",  # 2024-06, sits below the fully dated June release
            "JanFirst",  # 2024-01-01
            "YearOnly",  # 2024, below every dated 2024 release
        ]

    def test_unknown_dates_sink_to_the_bottom(self):
        items = [
            missing("NoDate", ""),
            missing("Ancient", "1970-01-01"),
            missing("Recent", "2025-01-01"),
        ]
        assert [i.release.title for i in sort_releases(items)][-1] == "NoDate"

    def test_ties_break_alphabetically_and_are_stable(self):
        items = [
            missing("B Title", "2024-01-01", artist="Zed"),
            missing("A Title", "2024-01-01", artist="Alpha"),
            missing("C Title", "2024-01-01", artist="Alpha"),
        ]
        ordered = sort_releases(items)
        assert [(i.local_artist, i.release.title) for i in ordered] == [
            ("Alpha", "A Title"),
            ("Alpha", "C Title"),
            ("Zed", "B Title"),
        ]
        assert sort_releases(list(reversed(items))) == ordered


class TestTable:
    """Column mechanics are exercised flat; grouping is covered separately."""

    def test_header_and_columns(self):
        lines = render_table([missing("Release Title", "2026-07-24")], width=140, grouped=False)
        for label in ("RELEASE DATE", "ARTIST", "TYPE", "TITLE", "URL"):
            assert label in lines[0]
        assert lines[1].startswith("2026-07-24")
        assert "Album" in lines[1]

    def test_deezer_url_is_printed_in_full(self):
        item = missing("Release Title", "2026-07-24")
        line = render_table([item], width=140, grouped=False)[1]
        assert line.endswith(item.release.link)
        assert item.release.link.startswith("https://www.deezer.com/album/")

    def test_truncated_title_keeps_a_gap_before_the_url(self):
        # Truncating to the padded width eats the separator, gluing the
        # ellipsis onto "https://".
        item = missing("A very long release title " * 6, "2026-07-24")
        line = render_table([item], width=90, grouped=False)[1]
        assert "…https://" not in line
        assert "  " + item.release.link in line

    def test_url_is_never_truncated(self):
        # A truncated URL is useless, so it wins over the title for space.
        item = missing("A very long release title " * 6, "2026-07-24")
        line = render_table([item], width=60, grouped=False)[1]
        assert line.endswith(item.release.link)
        assert "\u2026" in line, "the title should absorb the truncation instead"

    def test_header_columns_do_not_run_together(self):
        # "RELEASE DATE" exactly fills a 12-cell column, so a too-narrow date
        # column silently produces "RELEASE DATEARTIST".
        header = render_table([missing("T", "2024-01-01")], width=140, grouped=False)[0]
        assert "RELEASE DATE  " in header
        assert "DATEARTIST" not in header
        for label in ("ARTIST", "TYPE", "TITLE", "URL"):
            assert f" {label}" in header

    def test_columns_do_not_collide_with_the_widest_values(self):
        item = missing("Title", "2024-01-01", artist="A" * 40)
        line = render_table([item], width=160, grouped=False)[1]
        assert "  " in line[14:], "artist column must keep a gap before TYPE"

    def test_resembles_the_prd_example(self):
        items = sort_releases(
            [
                missing("Release Title", "2026-07-24", "Artist Name", ReleaseType.SINGLE),
                missing("Another Release", "2026-07-18", "Another Artist", ReleaseType.EP),
                missing("Album Title", "2026-06-02", "Artist Name", ReleaseType.ALBUM),
            ]
        )
        lines = render_table(items, width=140, grouped=False)
        assert lines[1].split()[:5] == ["2026-07-24", "Artist", "Name", "Single", "Release"]
        assert lines[2].split()[:5] == ["2026-07-18", "Another", "Artist", "EP", "Another"]
        assert lines[3].split()[:5] == ["2026-06-02", "Artist", "Name", "Album", "Album"]

    def test_columns_align_with_wide_characters(self):
        items = [
            missing("A", "2024-01-01", artist="\u6771\u4eac\u4e8b\u5909"),
            missing("B", "2024-01-02", artist="Radiohead"),
        ]
        lines = render_table(items, width=140, grouped=False)[1:]
        starts = {display_width(line[: line.index("Album")]) for line in lines}
        assert len(starts) == 1, "TYPE column must start at the same display offset"

    def test_long_titles_are_truncated_not_wrapped(self):
        item = missing("X" * 300, "2024-01-01")
        lines = render_table([item], width=80, grouped=False)
        assert len(lines) == 2, "one row, never wrapped onto a second line"
        assert "\u2026" in lines[1]

    def test_empty_result_set_renders_nothing(self):
        assert render_table([]) == []


class TestGrouping:
    def _items(self):
        return sort_releases(
            [
                missing("New Single", "2026-07-24", "A", ReleaseType.SINGLE),
                missing("An EP", "2026-07-18", "B", ReleaseType.EP),
                missing("Old Album", "2020-01-01", "C", ReleaseType.ALBUM),
                missing("New Album", "2026-06-02", "D", ReleaseType.ALBUM),
                missing("Mystery", "", "E", ReleaseType.UNKNOWN),
            ]
        )

    def test_groups_appear_in_order_with_counts(self):
        lines = render_table(self._items(), width=140)
        labels = set(GROUP_LABELS.values())
        headings = [ln for ln in lines if ln.split(" (")[0] in labels]
        assert headings == ["Albums (2)", "EPs (1)", "Singles (1)", "Unclassified (1)"]

    def test_dates_stay_newest_first_inside_each_group(self):
        lines = render_table(self._items(), width=140)
        album_rows = lines[lines.index("Albums (2)") + 1 :][:2]
        assert album_rows[0].startswith("2026-06-02")
        assert album_rows[1].startswith("2020-01-01")

    def test_empty_groups_are_omitted(self):
        items = [missing("Only An Album", "2024-01-01")]
        text = "\n".join(render_table(items, width=140))
        assert "Albums (1)" in text
        assert "Singles" not in text and "EPs" not in text

    def test_type_column_is_kept_so_rows_stay_self_describing(self):
        # Grouped output still has to survive being piped somewhere.
        lines = render_table(self._items(), width=140)
        rows = [ln for ln in lines if ln[:2].isdigit() or ln.startswith("unknown")]
        assert all(any(t in r for t in ("Album", "EP", "Single", "Unknown")) for r in rows)

    def test_flat_mode_has_no_group_headings_or_blank_lines(self):
        lines = render_table(self._items(), width=140, grouped=False)
        assert "" not in lines
        assert not any(ln.startswith("Albums") for ln in lines)
        assert len(lines) == 6  # header + five rows


class TestSummary:
    def test_compact_summary_format(self):
        items = (
            [missing(f"a{i}", "2024-01-01", release_type=ReleaseType.ALBUM) for i in range(8)]
            + [missing(f"e{i}", "2024-01-01", release_type=ReleaseType.EP) for i in range(6)]
            + [missing(f"s{i}", "2024-01-01", release_type=ReleaseType.SINGLE) for i in range(28)]
        )
        unresolved = [UnresolvedArtist(f"artist{i}", "no match") for i in range(5)]
        review = [
            ReviewItem(deezer_release(f"r{i}"), "Artist", ReleaseType.ALBUM, "unclear")
            for i in range(3)
        ]
        summary = build_summary(items, unresolved, review, artists_scanned=50, partial_reasons=[])
        out = io.StringIO()
        print_summary(summary, out)
        assert out.getvalue() == (
            "42 missing releases: 8 albums, 6 EPs, 28 singles\n"
            "5 artists could not be resolved\n"
            "3 releases require review\n"
        )

    def test_singular_wording(self):
        summary = build_summary(
            [missing("one", "2024-01-01")],
            [UnresolvedArtist("x", "y")],
            [ReviewItem(deezer_release("r"), "Artist", ReleaseType.ALBUM, "unclear")],
            1,
            [],
        )
        out = io.StringIO()
        print_summary(summary, out)
        text = out.getvalue()
        assert "1 missing release: 1 album\n" in text
        assert "1 artist could not be resolved" in text
        # Subject and verb must agree in both directions.
        assert "1 release requires review" in text

    def test_zero_results(self):
        out = io.StringIO()
        print_summary(build_summary([], [], [], 10, []), out)
        assert out.getvalue() == "0 missing releases\n"

    def test_partial_runs_are_flagged(self):
        out = io.StringIO()
        print_summary(build_summary([], [], [], 10, ["Deezer timed out"]), out)
        assert "output is partial: Deezer timed out" in out.getvalue()


class TestStreams:
    def test_results_go_to_stdout_only(self):
        out = io.StringIO()
        print_results([missing("T", "2024-01-01")], out)
        assert "T" in out.getvalue()

    def test_review_and_unresolved_go_to_stderr(self):
        err = io.StringIO()
        print_review(
            [ReviewItem(deezer_release("R"), "Artist", ReleaseType.ALBUM, "conflicting evidence")],
            err,
        )
        print_unresolved([UnresolvedArtist("Ghost", "no match")], err)
        text = err.getvalue()
        assert "Needs review (1)" in text
        assert "conflicting evidence" in text
        assert "Unresolved artists (1)" in text
        assert "dropwatch map" in text


class TestSinceCutoff:
    """--since accepts a year, a month or a full date."""

    def on(self, date, cutoff):
        return ReleaseDate.parse(date).on_or_after(ReleaseDate.parse(cutoff))

    def test_year_cutoff(self):
        assert self.on("2024-06-15", "2024") is True
        assert self.on("2023-12-31", "2024") is False

    def test_month_cutoff(self):
        assert self.on("2024-07-01", "2024-06") is True
        assert self.on("2024-05-31", "2024-06") is False

    def test_full_date_cutoff(self):
        assert self.on("2024-06-15", "2024-06-01") is True
        assert self.on("2024-06-01", "2024-06-01") is True, "the cutoff day is included"
        assert self.on("2024-05-31", "2024-06-01") is False

    def test_less_precise_release_is_kept(self):
        # "2024" might fall after 2024-06-01, so excluding it would hide a
        # release on a technicality.
        assert self.on("2024", "2024-06-01") is True
        assert self.on("2024-06", "2024-06-15") is True

    def test_a_clearly_older_imprecise_date_is_still_excluded(self):
        assert self.on("2019", "2024-06-01") is False

    def test_unknown_dates_always_pass(self):
        assert self.on("", "2024-06-01") is True


class TestSinceArgument:
    def test_accepted_forms(self):
        from dropwatch.cli import _parse_since

        assert str(_parse_since("2024")) == "2024"
        assert str(_parse_since("2024-06")) == "2024-06"
        assert str(_parse_since("2024-06-15")) == "2024-06-15"

    def test_rejected_forms(self):
        import argparse

        import pytest

        from dropwatch.cli import _parse_since

        for bad in ("yesterday", "June", "24", ""):
            with pytest.raises(argparse.ArgumentTypeError):
                _parse_since(bad)
