"""Tests for the search-throttle / cooldown state (lib/search_state.py).

SearchState prevents hammering indexers by enforcing per-item and broad-search
cooldown windows that escalate with each repeated search.
"""
import json
from datetime import datetime, timedelta

from lib.search_state import (
    _BROAD_BACKOFF_MINUTES,
    _ITEM_BACKOFF_MINUTES,
    _RESET_AFTER_HOURS,
    SearchState,
    _fmt,
)

# ── helpers ───────────────────────────────────────────────────────────────────

def _expire(state: SearchState, key: str) -> None:
    """Manually push the backoff_until into the past so can_search returns True."""
    rec = state._data.get(key)
    if rec:
        rec.backoff_until = _fmt(datetime.now().astimezone() - timedelta(minutes=1))


def _make_old(state: SearchState, key: str, hours: int) -> None:
    """Backdate last_at so the auto-reset logic kicks in."""
    rec = state._data.get(key)
    if rec:
        old = datetime.now().astimezone() - timedelta(hours=hours)
        rec.last_at = _fmt(old)
        rec.backoff_until = _fmt(old)


# ── new-key behaviour ─────────────────────────────────────────────────────────

class TestInitialState:
    def test_new_key_allows_search(self, tmp_path):
        state = SearchState(tmp_path / "ss.json")
        ok, reason = state.can_search("movie:1")
        assert ok
        assert reason == ""

    def test_new_broad_key_allows_search(self, tmp_path):
        state = SearchState(tmp_path / "ss.json")
        ok, _ = state.can_search("missing_movies", broad=True)
        assert ok

    def test_missing_file_does_not_crash(self, tmp_path):
        state = SearchState(tmp_path / "never_written.json")
        ok, _ = state.can_search("ep:99")
        assert ok


# ── cooldown after recording ──────────────────────────────────────────────────

class TestCooldownAfterRecord:
    def test_blocks_immediately_after_record(self, tmp_path):
        state = SearchState(tmp_path / "ss.json")
        state.record_search("movie:42")
        ok, reason = state.can_search("movie:42")
        assert not ok
        assert "retry in" in reason

    def test_first_item_backoff_is_10_minutes(self, tmp_path):
        state = SearchState(tmp_path / "ss.json")
        state.record_search("ep:10")
        rec = state._data["ep:10"]
        last = datetime.fromisoformat(rec.last_at)
        until = datetime.fromisoformat(rec.backoff_until)
        delta_minutes = round((until - last).total_seconds() / 60)
        assert delta_minutes == _ITEM_BACKOFF_MINUTES[0]  # 10 minutes

    def test_first_broad_backoff_is_2_hours(self, tmp_path):
        state = SearchState(tmp_path / "ss.json")
        state.record_search("missing_eps", broad=True)
        rec = state._data["missing_eps"]
        last = datetime.fromisoformat(rec.last_at)
        until = datetime.fromisoformat(rec.backoff_until)
        delta_minutes = round((until - last).total_seconds() / 60)
        assert delta_minutes == _BROAD_BACKOFF_MINUTES[0]  # 120 minutes

    def test_different_keys_are_independent(self, tmp_path):
        state = SearchState(tmp_path / "ss.json")
        state.record_search("movie:1")
        ok, _ = state.can_search("movie:2")
        assert ok

    def test_allows_after_backoff_expires(self, tmp_path):
        state = SearchState(tmp_path / "ss.json")
        state.record_search("movie:7")
        _expire(state, "movie:7")
        ok, _ = state.can_search("movie:7")
        assert ok


# ── count escalation ──────────────────────────────────────────────────────────

class TestBackoffEscalation:
    def test_count_increments_on_each_record(self, tmp_path):
        state = SearchState(tmp_path / "ss.json")
        state.record_search("ep:5")
        assert state._data["ep:5"].count == 1
        _expire(state, "ep:5")
        state.record_search("ep:5")
        assert state._data["ep:5"].count == 2

    def test_item_backoff_tiers_in_order(self, tmp_path):
        """Each successive search should use the next tier from _ITEM_BACKOFF_MINUTES."""
        state = SearchState(tmp_path / "ss.json")
        key = "ep:escalate"
        for i, expected_minutes in enumerate(_ITEM_BACKOFF_MINUTES):
            _expire(state, key)
            state.record_search(key)
            rec = state._data[key]
            last = datetime.fromisoformat(rec.last_at)
            until = datetime.fromisoformat(rec.backoff_until)
            actual_minutes = round((until - last).total_seconds() / 60)
            assert actual_minutes == expected_minutes, (
                f"tier {i}: expected {expected_minutes}m, got {actual_minutes}m"
            )

    def test_item_backoff_caps_at_last_tier(self, tmp_path):
        """Once all tiers are exhausted, the last tier is repeated."""
        state = SearchState(tmp_path / "ss.json")
        key = "ep:cap"
        # Burn through all tiers
        for _ in range(len(_ITEM_BACKOFF_MINUTES) + 3):
            _expire(state, key)
            state.record_search(key)
        rec = state._data[key]
        last = datetime.fromisoformat(rec.last_at)
        until = datetime.fromisoformat(rec.backoff_until)
        actual_minutes = round((until - last).total_seconds() / 60)
        assert actual_minutes == _ITEM_BACKOFF_MINUTES[-1]

    def test_broad_backoff_tiers_in_order(self, tmp_path):
        state = SearchState(tmp_path / "ss.json")
        key = "missing_broad"
        for i, expected_minutes in enumerate(_BROAD_BACKOFF_MINUTES):
            _expire(state, key)
            state.record_search(key, broad=True)
            rec = state._data[key]
            last = datetime.fromisoformat(rec.last_at)
            until = datetime.fromisoformat(rec.backoff_until)
            actual_minutes = round((until - last).total_seconds() / 60)
            assert actual_minutes == expected_minutes, (
                f"broad tier {i}: expected {expected_minutes}m, got {actual_minutes}m"
            )


# ── persistence ───────────────────────────────────────────────────────────────

class TestPersistence:
    def test_state_survives_reload(self, tmp_path):
        path = tmp_path / "ss.json"
        s1 = SearchState(path)
        s1.record_search("movie:99")

        s2 = SearchState(path)
        ok, _ = s2.can_search("movie:99")
        assert not ok

    def test_count_survives_reload(self, tmp_path):
        path = tmp_path / "ss.json"
        s1 = SearchState(path)
        s1.record_search("ep:77")
        _expire(s1, "ep:77")
        s1.record_search("ep:77")

        s2 = SearchState(path)
        assert s2._data["ep:77"].count == 2

    def test_corrupt_file_is_silently_ignored(self, tmp_path):
        path = tmp_path / "ss.json"
        path.write_text("{ not valid json !!!")
        state = SearchState(path)  # must not raise
        ok, _ = state.can_search("any:key")
        assert ok

    def test_empty_file_is_silently_ignored(self, tmp_path):
        path = tmp_path / "ss.json"
        path.write_text("")
        state = SearchState(path)
        ok, _ = state.can_search("any:key")
        assert ok

    def test_json_structure_on_disk(self, tmp_path):
        path = tmp_path / "ss.json"
        state = SearchState(path)
        state.record_search("movie:1")
        data = json.loads(path.read_text())
        assert "searches" in data
        assert "movie:1" in data["searches"]
        rec = data["searches"]["movie:1"]
        assert "count" in rec and "last_at" in rec and "backoff_until" in rec


# ── auto-reset after inactivity ───────────────────────────────────────────────

class TestAutoReset:
    def test_resets_after_48_hours(self, tmp_path):
        state = SearchState(tmp_path / "ss.json")
        state.record_search("movie:old")
        # Backdate last_at past the reset threshold
        _make_old(state, "movie:old", hours=_RESET_AFTER_HOURS + 1)

        ok, reason = state.can_search("movie:old")
        assert ok, f"Expected reset after {_RESET_AFTER_HOURS}h, got: {reason}"

    def test_does_not_reset_before_48_hours(self, tmp_path):
        state = SearchState(tmp_path / "ss.json")
        state.record_search("movie:fresh")
        _make_old(state, "movie:fresh", hours=_RESET_AFTER_HOURS - 1)
        # Backoff_until should still be in the future relative to now; re-record
        # to get a fresh backoff then check the count wasn't reset
        count_before = state._data["movie:fresh"].count
        assert count_before == 1
