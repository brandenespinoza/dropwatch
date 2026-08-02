"""Live per-artist progress on stderr."""

from __future__ import annotations

from conftest import deezer_release

from dropwatch.models import MissingRelease, Ownership, ReleaseType, UnresolvedArtist
from dropwatch.scan import ArtistOutcome


def item(release_type):
    return MissingRelease(
        release=deezer_release("T"),
        local_artist="A",
        release_type=release_type,
        ownership=Ownership.MISSING,
    )


class TestOutcomeText:
    def test_counts_are_broken_down_by_type(self):
        outcome = ArtistOutcome(
            "Fleetwood Mac",
            missing=[item(ReleaseType.ALBUM), item(ReleaseType.ALBUM), item(ReleaseType.SINGLE)],
        )
        assert outcome.describe() == "3 missing (2 albums, 1 single)"

    def test_singular_wording(self):
        assert ArtistOutcome("X", missing=[item(ReleaseType.EP)]).describe() == "1 missing (1 EP)"

    def test_review_only(self):
        assert ArtistOutcome("X", review=2).describe() == "2 to review"

    def test_missing_and_review_together(self):
        outcome = ArtistOutcome("X", missing=[item(ReleaseType.ALBUM)], review=1)
        assert outcome.describe() == "1 missing (1 album), 1 to review"

    def test_unresolved_reports_candidate_count(self):
        from dropwatch.models import DeezerArtist

        outcome = ArtistOutcome(
            "Ghost",
            unresolved=UnresolvedArtist(
                "Ghost", "ambiguous", [DeezerArtist(id="1", name="Ghost"), DeezerArtist(id="2", name="Ghost")]
            ),
        )
        assert outcome.describe() == "unresolved, 2 candidates"

    def test_unresolved_without_candidates(self):
        outcome = ArtistOutcome("Ghost", unresolved=UnresolvedArtist("Ghost", "no match", []))
        assert outcome.describe() == "unresolved"

    def test_failure(self):
        assert ArtistOutcome("X", failed=True).describe() == "failed"

    def test_nothing_found(self):
        assert ArtistOutcome("X").describe() == "nothing new"


class TestWhatGetsPrinted:
    def test_quiet_artists_stay_off_the_scroll(self):
        # 196 lines of "nothing new" would drown the signal.
        assert ArtistOutcome("X").worth_reporting is False

    def test_findings_are_reported(self):
        assert ArtistOutcome("X", missing=[item(ReleaseType.ALBUM)]).worth_reporting
        assert ArtistOutcome("X", review=1).worth_reporting
        assert ArtistOutcome("X", failed=True).worth_reporting
        assert ArtistOutcome(
            "X", unresolved=UnresolvedArtist("X", "y", [])
        ).worth_reporting


class TestStreamSeparation:
    def test_progress_never_touches_stdout(self, capsys, monkeypatch, config, store):
        """stdout must stay exactly the report, so redirecting it stays clean."""
        import sys

        from dropwatch.scan import ScanOptions, Scanner

        monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
        scanner = Scanner.__new__(Scanner)
        options = ScanOptions(progress=True)
        scanner._progress(options, 1, 10, "Some Artist")
        scanner._report_artist(
            options, 1, 10, ArtistOutcome("Some Artist", missing=[item(ReleaseType.ALBUM)])
        )
        scanner._clear_progress(options)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Some Artist" in captured.err
        assert "1 missing (1 album)" in captured.err

    def test_nothing_is_written_when_stderr_is_not_a_terminal(self, capsys, monkeypatch):
        import sys

        from dropwatch.scan import ScanOptions, Scanner

        monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
        scanner = Scanner.__new__(Scanner)
        options = ScanOptions(progress=True)
        scanner._progress(options, 1, 10, "Some Artist")
        scanner._report_artist(
            options, 1, 10, ArtistOutcome("Some Artist", missing=[item(ReleaseType.ALBUM)])
        )
        assert capsys.readouterr().err == ""

    def test_no_progress_flag_silences_it(self, capsys, monkeypatch):
        import sys

        from dropwatch.scan import ScanOptions, Scanner

        monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
        scanner = Scanner.__new__(Scanner)
        options = ScanOptions(progress=False)
        scanner._progress(options, 1, 10, "X")
        scanner._report_artist(options, 1, 10, ArtistOutcome("X", review=1))
        assert capsys.readouterr().err == ""
