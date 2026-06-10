"""Tautulli API client — active streams, play history, and stream management.

Tautulli exposes a single JSON API endpoint:
    http://<host>:<port>/api/v2?apikey=<key>&cmd=<command>

All responses follow the shape:
    {"response": {"result": "success"|"error", "data": {...}}}
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx

from config import TautulliConfig


@dataclass
class TautulliStatus:
    online: bool
    stream_count: int = 0
    error: str = ""


@dataclass
class TautulliStream:
    session_key: str
    user: str
    title: str                # episode title (or movie title)
    media_type: str           # "movie", "episode", "track"
    year: int
    grandparent_title: str    # TV show name for episodes; "" for movies
    season: int               # 0 for movies
    episode: int              # 0 for movies
    player: str               # client device name
    transcode_decision: str   # "direct play", "transcode", "copy"
    bandwidth: int            # kbps (0 = unknown)
    progress_pct: float       # 0–100
    state: str                # "playing", "paused", "buffering"
    ip_address: str = ""
    quality_profile: str = ""


@dataclass
class TautulliHistoryRecord:
    user: str
    title: str                # episode title (or movie title)
    media_type: str           # "movie", "episode", "track"
    grandparent_title: str    # TV show name for episodes
    year: int
    played_at: str            # formatted YYYY-MM-DD HH:MM
    duration_secs: int
    percent_complete: int
    player: str
    transcode_decision: str
    quality_profile: str = ""


def _client(cfg: TautulliConfig) -> httpx.Client:
    return httpx.Client(verify=cfg.verify_ssl, timeout=15)


def _api(cfg: TautulliConfig, cmd: str, **params) -> dict:
    """Call the Tautulli API. Returns the ``data`` payload or raises RuntimeError."""
    full_params = {"apikey": cfg.api_key, "cmd": cmd, **params}
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/v2", params=full_params)
    if not r.is_success:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    resp = r.json().get("response") or {}
    if resp.get("result") != "success":
        msg = resp.get("message") or "unknown error"
        raise RuntimeError(f"Tautulli API error: {msg}")
    return resp.get("data") or {}


def get_status(cfg: TautulliConfig) -> TautulliStatus:
    """Check Tautulli connectivity and return active stream count."""
    try:
        data = _api(cfg, "get_activity")
        count = int(data.get("stream_count") or 0)
        return TautulliStatus(online=True, stream_count=count)
    except Exception as e:
        return TautulliStatus(online=False, error=str(e))


def get_activity(cfg: TautulliConfig) -> list[TautulliStream]:
    """Return currently active streams."""
    try:
        data = _api(cfg, "get_activity")
    except Exception:
        return []
    sessions = data.get("sessions") or []
    streams = []
    for s in sessions:
        view_offset = int(s.get("view_offset") or 0)
        duration = int(s.get("duration") or 1)
        pct = round((view_offset / duration * 100) if duration else 0.0, 1)
        streams.append(TautulliStream(
            session_key=str(s.get("session_key", "")),
            user=s.get("friendly_name") or s.get("user", ""),
            title=s.get("title", ""),
            media_type=s.get("media_type", ""),
            year=int(s.get("year") or 0),
            grandparent_title=s.get("grandparent_title", ""),
            season=int(s.get("parent_media_index") or 0),
            episode=int(s.get("media_index") or 0),
            player=s.get("player", ""),
            transcode_decision=s.get("transcode_decision", ""),
            bandwidth=int(s.get("bandwidth") or 0),
            progress_pct=pct,
            state=s.get("state", ""),
            ip_address=s.get("ip_address_public") or s.get("ip_address", ""),
            quality_profile=s.get("quality_profile", ""),
        ))
    return streams


def get_history(
    cfg: TautulliConfig,
    length: int = 20,
    user: str = "",
) -> list[TautulliHistoryRecord]:
    """Return recent play history, newest first."""
    params: dict = {"length": length, "order_column": "date", "order_dir": "desc"}
    if user:
        params["search"] = user
    try:
        data = _api(cfg, "get_history", **params)
    except Exception:
        return []
    # get_history nests records under a second "data" key
    records = data.get("data") or []
    result = []
    for rec in records:
        ts = rec.get("date") or rec.get("started") or 0
        try:
            played_at = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            played_at = str(ts)
        result.append(TautulliHistoryRecord(
            user=rec.get("friendly_name") or rec.get("user", ""),
            title=rec.get("title", ""),
            media_type=rec.get("media_type", ""),
            grandparent_title=rec.get("grandparent_title", ""),
            year=int(rec.get("year") or 0),
            played_at=played_at,
            duration_secs=int(rec.get("duration") or 0),
            percent_complete=int(rec.get("percent_complete") or 0),
            player=rec.get("player", ""),
            transcode_decision=rec.get("transcode_decision") or "",
            quality_profile=rec.get("quality_profile") or "",
        ))
    return result


def terminate_session(cfg: TautulliConfig, session_key: str, message: str = "") -> None:
    """Terminate an active stream. Raises RuntimeError on failure."""
    params: dict = {"session_key": session_key}
    if message:
        params["message"] = message
    _api(cfg, "terminate_session", **params)
