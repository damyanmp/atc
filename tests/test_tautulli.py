"""Tests for lib/tautulli.py — dataclasses and utility behaviour."""
from __future__ import annotations

from lib.tautulli import TautulliHistoryRecord, TautulliStatus, TautulliStream


# ── TautulliStatus ────────────────────────────────────────────────────────────

class TestTautulliStatus:
    def test_online_defaults(self):
        s = TautulliStatus(online=True, stream_count=3)
        assert s.online
        assert s.stream_count == 3
        assert s.error == ""

    def test_offline(self):
        s = TautulliStatus(online=False, error="Connection refused")
        assert not s.online
        assert s.error == "Connection refused"
        assert s.stream_count == 0

    def test_online_zero_streams(self):
        s = TautulliStatus(online=True, stream_count=0)
        assert s.online
        assert s.stream_count == 0


# ── TautulliStream ────────────────────────────────────────────────────────────

class TestTautulliStream:
    def _movie_stream(self, **kwargs) -> TautulliStream:
        defaults = dict(
            session_key="s1", session_id="sess-1", user="alice", title="Inception",
            media_type="movie", year=2010, grandparent_title="",
            season=0, episode=0, player="Web", transcode_decision="transcode",
            bandwidth=5000, progress_pct=45.5, state="playing",
        )
        defaults.update(kwargs)
        return TautulliStream(**defaults)

    def _episode_stream(self, **kwargs) -> TautulliStream:
        defaults = dict(
            session_key="s2", session_id="sess-2", user="bob", title="Pilot",
            media_type="episode", year=2008, grandparent_title="Breaking Bad",
            season=1, episode=1, player="AppleTV", transcode_decision="direct play",
            bandwidth=20000, progress_pct=12.3, state="paused",
        )
        defaults.update(kwargs)
        return TautulliStream(**defaults)

    def test_movie_optional_defaults(self):
        s = self._movie_stream()
        assert s.ip_address == ""
        assert s.quality_profile == ""

    def test_movie_fields(self):
        s = self._movie_stream(bandwidth=8000, progress_pct=77.0)
        assert s.bandwidth == 8000
        assert s.progress_pct == 77.0
        assert s.grandparent_title == ""
        assert s.season == 0
        assert s.episode == 0

    def test_episode_fields(self):
        s = self._episode_stream()
        assert s.grandparent_title == "Breaking Bad"
        assert s.season == 1
        assert s.episode == 1
        assert s.media_type == "episode"

    def test_state_values(self):
        for state in ("playing", "paused", "buffering"):
            s = self._movie_stream(state=state)
            assert s.state == state

    def test_ip_and_quality(self):
        s = self._movie_stream(ip_address="10.0.0.5", quality_profile="4K (H.265)")
        assert s.ip_address == "10.0.0.5"
        assert s.quality_profile == "4K (H.265)"

    def test_progress_pct_zero(self):
        s = self._movie_stream(progress_pct=0.0)
        assert s.progress_pct == 0.0

    def test_progress_pct_full(self):
        s = self._movie_stream(progress_pct=100.0)
        assert s.progress_pct == 100.0


# ── TautulliHistoryRecord ─────────────────────────────────────────────────────

class TestTautulliHistoryRecord:
    def _movie_record(self, **kwargs) -> TautulliHistoryRecord:
        defaults = dict(
            user="alice", title="Inception", media_type="movie",
            grandparent_title="", year=2010,
            played_at="2026-03-24 21:15", duration_secs=8880,
            percent_complete=100, player="Web", transcode_decision="transcode",
        )
        defaults.update(kwargs)
        return TautulliHistoryRecord(**defaults)

    def _episode_record(self, **kwargs) -> TautulliHistoryRecord:
        defaults = dict(
            user="bob", title="Pilot", media_type="episode",
            grandparent_title="Breaking Bad", year=2008,
            played_at="2026-03-24 20:00", duration_secs=2700,
            percent_complete=75, player="AppleTV", transcode_decision="direct play",
        )
        defaults.update(kwargs)
        return TautulliHistoryRecord(**defaults)

    def test_movie_optional_defaults(self):
        r = self._movie_record()
        assert r.quality_profile == ""

    def test_movie_fields(self):
        r = self._movie_record()
        assert r.user == "alice"
        assert r.percent_complete == 100
        assert r.duration_secs == 8880

    def test_episode_fields(self):
        r = self._episode_record()
        assert r.grandparent_title == "Breaking Bad"
        assert r.media_type == "episode"
        assert r.duration_secs == 2700

    def test_percent_complete_range(self):
        for pct in (0, 50, 90, 100):
            r = self._movie_record(percent_complete=pct)
            assert r.percent_complete == pct

    def test_quality_profile(self):
        r = self._movie_record(quality_profile="1080p (H.264)")
        assert r.quality_profile == "1080p (H.264)"

    def test_transcode_decision_variants(self):
        for td in ("direct play", "transcode", "copy"):
            r = self._movie_record(transcode_decision=td)
            assert r.transcode_decision == td
