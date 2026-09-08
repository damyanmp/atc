"""Tests for arr.py data structures and derived logic.

Covers: ArrQueueItem.progress_pct, CalendarEpisode/CalendarMovie fields,
ArrImportRecord defaults, and ArrMovieRecord/ArrSeriesRecord structures.
No HTTP calls are made — all tests exercise in-memory dataclass behaviour.
"""
import pytest

from lib.arr import (
    ArrImportRecord,
    ArrQueueItem,
    ArrStatus,
    CalendarEpisode,
    CalendarMovie,
)

# ── ArrQueueItem.progress_pct ────────────────────────────────────────────────

class TestArrQueueItemProgress:
    def test_fully_downloaded(self):
        item = ArrQueueItem(queue_id=1, title="Movie", size_bytes=1000, size_left=0)
        assert item.progress_pct == 100.0

    def test_half_downloaded(self):
        item = ArrQueueItem(queue_id=1, title="Movie", size_bytes=1000, size_left=500)
        assert item.progress_pct == 50.0

    def test_not_started(self):
        item = ArrQueueItem(queue_id=1, title="Movie", size_bytes=1000, size_left=1000)
        assert item.progress_pct == 0.0

    def test_zero_size_returns_zero(self):
        item = ArrQueueItem(queue_id=1, title="Movie", size_bytes=0, size_left=0)
        assert item.progress_pct == 0.0

    def test_partial_progress(self):
        item = ArrQueueItem(queue_id=1, title="Movie", size_bytes=200, size_left=50)
        assert item.progress_pct == pytest.approx(75.0)


# ── CalendarEpisode ───────────────────────────────────────────────────────────

class TestCalendarEpisode:
    def test_episode_id_field_stored(self):
        ep = CalendarEpisode(
            series_title="The Show", season=2, episode=5,
            title="Chapter Five", air_date="2026-03-23",
            has_file=False, monitored=True,
            series_id=10, episode_id=999,
        )
        assert ep.episode_id == 999

    def test_episode_id_defaults_to_zero(self):
        ep = CalendarEpisode(
            series_title="The Show", season=1, episode=1,
            title="Pilot", air_date="2026-01-01",
            has_file=False, monitored=True,
        )
        assert ep.episode_id == 0

    def test_series_id_defaults_to_zero(self):
        ep = CalendarEpisode(
            series_title="S", season=1, episode=1,
            title="T", air_date="2026-01-01",
            has_file=False, monitored=True,
        )
        assert ep.series_id == 0

    def test_has_file_and_monitored_flags(self):
        ep = CalendarEpisode(
            series_title="S", season=1, episode=1,
            title="T", air_date="2026-01-01",
            has_file=True, monitored=False,
        )
        assert ep.has_file is True
        assert ep.monitored is False


# ── CalendarMovie ─────────────────────────────────────────────────────────────

class TestCalendarMovie:
    def test_basic_fields(self):
        m = CalendarMovie(
            title="Dune: Part Three", year=2026,
            release_date="2026-03-23", release_type="cinema",
            has_file=False, monitored=True,
            arr_id=42, tmdb_id=1234,
        )
        assert m.title == "Dune: Part Three"
        assert m.arr_id == 42
        assert m.tmdb_id == 1234

    def test_arr_id_defaults_to_zero(self):
        m = CalendarMovie(
            title="Film", year=2025,
            release_date="2025-01-01", release_type="digital",
            has_file=False, monitored=True,
        )
        assert m.arr_id == 0
        assert m.tmdb_id == 0

    def test_release_types(self):
        for rtype in ("digital", "physical", "cinema"):
            m = CalendarMovie(
                title="F", year=2025,
                release_date="2025-01-01", release_type=rtype,
                has_file=False, monitored=True,
            )
            assert m.release_type == rtype


# ── ArrImportRecord ───────────────────────────────────────────────────────────

class TestArrImportRecord:
    def test_movie_defaults(self):
        rec = ArrImportRecord(title="Inception", media_type="movie")
        assert rec.season == 0
        assert rec.episode == 0
        assert rec.episode_title == ""
        assert rec.quality == ""
        assert rec.size_bytes == 0

    def test_episode_fields(self):
        rec = ArrImportRecord(
            title="Breaking Bad", media_type="episode",
            season=3, episode=10, episode_title="Fly",
            quality="WEB-1080p", size_bytes=2_500_000_000,
        )
        assert rec.season == 3
        assert rec.episode == 10
        assert rec.episode_title == "Fly"
        assert rec.quality == "WEB-1080p"
        assert rec.size_bytes == 2_500_000_000


# ── ArrStatus ─────────────────────────────────────────────────────────────────

class TestArrStatus:
    def test_online_defaults(self):
        s = ArrStatus(online=True, version="5.0.0", app_name="Radarr")
        assert s.online is True
        assert s.error == ""

    def test_offline(self):
        s = ArrStatus(online=False, error="Connection refused")
        assert s.online is False
        assert "refused" in s.error
