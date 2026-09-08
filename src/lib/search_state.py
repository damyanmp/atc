"""Search throttle — prevents hammering indexers by tracking recent searches.

State is persisted to <config_dir>/search_state.json.

Backoff tiers for per-item searches (individual episodes / movies via `media today`):
  attempt 1  →  wait 10 min before retrying
  attempt 2  →  wait 20 min
  attempt 3  →  wait 45 min
  attempt 4+ →  wait 60 min

Backoff tiers for broad searches (`search-missing` — hits all monitored items):
  attempt 1  →  wait 2 hours
  attempt 2  →  wait 8 hours
  attempt 3+ →  wait 24 hours

Attempt counts reset automatically after 48 hours of inactivity for a given key.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_ITEM_BACKOFF_MINUTES:  list[int] = [10, 20, 45, 60]
_BROAD_BACKOFF_MINUTES: list[int] = [120, 480, 1440]
_RESET_AFTER_HOURS = 48


@dataclass
class _Record:
    count: int = 0
    last_at: str = ""          # ISO datetime
    backoff_until: str = ""    # ISO datetime


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _fmt(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _human_delta(td: timedelta) -> str:
    mins = int(td.total_seconds() / 60)
    if mins >= 60:
        return f"{mins // 60}h {mins % 60:02d}m"
    return f"{mins}m"


class SearchState:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, _Record] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            for key, rec in raw.get("searches", {}).items():
                self._data[key] = _Record(
                    count=int(rec.get("count", 0)),
                    last_at=rec.get("last_at", ""),
                    backoff_until=rec.get("backoff_until", ""),
                )
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(
                {"searches": {
                    k: {"count": r.count, "last_at": r.last_at, "backoff_until": r.backoff_until}
                    for k, r in self._data.items()
                }},
                indent=2,
            ))
        except Exception:
            pass

    def _get(self, key: str) -> _Record:
        rec = self._data.get(key, _Record())
        last = _parse(rec.last_at)
        if last and (_now() - last) > timedelta(hours=_RESET_AFTER_HOURS):
            return _Record()
        return rec

    def can_search(self, key: str, broad: bool = False) -> tuple[bool, str]:
        """Return (allowed, reason).

        reason is empty when allowed; a human-readable explanation when blocked.
        """
        rec = self._get(key)
        if not rec.last_at:
            return True, ""
        until = _parse(rec.backoff_until)
        now = _now()
        if until and now < until:
            return False, f"retry in {_human_delta(until - now)} (searched {rec.count}x)"
        return True, ""

    def record_search(self, key: str, broad: bool = False) -> None:
        """Record that a search just fired and set the next backoff window."""
        rec = self._get(key)
        tiers = _BROAD_BACKOFF_MINUTES if broad else _ITEM_BACKOFF_MINUTES
        wait = tiers[min(rec.count, len(tiers) - 1)]
        now = _now()
        self._data[key] = _Record(
            count=rec.count + 1,
            last_at=_fmt(now),
            backoff_until=_fmt(now + timedelta(minutes=wait)),
        )
        self._save()


def load_state(config_path: Path) -> SearchState:
    """Load (or create) the search state file adjacent to config.yaml."""
    return SearchState(config_path.parent / "search_state.json")
