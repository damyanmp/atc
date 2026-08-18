"""Loki log-query client.

Minimal wrapper around Loki's HTTP query API — no auth, no pagination
machinery needed (unlike unifi.py); query_range()/count_over_window() cover it.

The filter vocabulary (NETWORK_LOG_TERMS / PIHOLE_LOG_TERMS) lives here rather
than in blackbox.py because both `atc blackbox` (historical dump, hours-scale
window) and `atc acars` (live alert scan, minutes-scale window) need the exact
same filter — tuned live against a real incident (see notes/changes/2026-08-18-
blackbox-loki-syslog-pihole.md): bare "timeout" and "gravity" are too broad
(routine WiFi-roaming retries / routine ad-blocking), and Ubiquiti's own
"(not an error)" self-annotation is excluded to avoid a substring false-positive
on "error".
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import httpx

from config import LokiConfig

NETWORK_LOG_TERMS = (
    r"error|critical|crit|fatal|panic|exception|denied|dropped"
    r"|reboot|rebooting|going down|shutting down"
    r"|link down|link up|no address|carrier"
    r"|upgrad(?:e|ing)|firmware|flashing"
    r"|moving from \w+ to (?:Blocking|Disabled)"
    r"|no route to host|network is unreachable|connection refused"
    r"|dhcp.*(?:fail|nak|decline)"
)
NETWORK_LOG_EXCLUDE_TERM = r"not an error"
PIHOLE_LOG_TERMS = r"error|err|critical|crit|fatal|panic|exception|warn|ftl"

_NETWORK_LOG_PATTERN = re.compile(rf"\b(?:{NETWORK_LOG_TERMS})\b", re.IGNORECASE)
_NETWORK_LOG_EXCLUDE = re.compile(NETWORK_LOG_EXCLUDE_TERM, re.IGNORECASE)
_PIHOLE_LOG_PATTERN = re.compile(rf"\b(?:{PIHOLE_LOG_TERMS})\b", re.IGNORECASE)


def query_range(
    cfg: LokiConfig, query: str, window_minutes: int, limit: int = 2000
) -> list[tuple[str, str, str]]:
    """Return (iso_timestamp, host_label, line) tuples for `query` over the last
    window_minutes, oldest first.

    Fetches the most recent `limit` lines (direction=backward) so a noisy
    window is capped at the API level rather than truncated after the fact —
    combined with a query that already has the interesting-lines filter
    pushed into it (see fetch_network_events/fetch_pihole_events), the cap
    applies to matches, not raw chatter.
    """
    now_ns = int(time.time() * 1e9)
    start_ns = now_ns - (window_minutes * 60 * 10**9)

    url = f"{cfg.base_url}/loki/api/v1/query_range"
    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(now_ns),
        "limit": str(limit),
        "direction": "backward",
    }
    with httpx.Client(verify=cfg.verify_ssl, timeout=15) as client:
        r = client.get(url, params=params)
        if not r.is_success:
            raise RuntimeError(f"Loki query_range HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()

    rows: list[tuple[str, str, str]] = []
    for stream in data.get("data", {}).get("result", []):
        host = stream.get("stream", {}).get("host", "")
        for ts_ns, line in stream.get("values", []):
            iso = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc).isoformat()[:19]
            rows.append((iso, host, line))

    rows.sort(key=lambda row: row[0])
    return rows


def count_over_window(cfg: LokiConfig, stream_selector: str, window_minutes: int) -> int:
    """Cheap total line count for `stream_selector` (e.g. `{job="syslog"}`) over the
    window, via an instant count_over_time query — same aggregation the Grafana
    dashboards use. Used only for the "N lines scanned" context stat.
    """
    url = f"{cfg.base_url}/loki/api/v1/query"
    params = {"query": f"sum(count_over_time({stream_selector}[{window_minutes}m]))"}
    with httpx.Client(verify=cfg.verify_ssl, timeout=15) as client:
        r = client.get(url, params=params)
        if not r.is_success:
            raise RuntimeError(f"Loki query HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()

    result = data.get("data", {}).get("result", [])
    if not result:
        return 0
    return int(float(result[0]["value"][1]))


def fetch_network_events(
    cfg: LokiConfig, window_minutes: int, limit: int = 2000
) -> list[tuple[str, str, str]]:
    """(iso_ts, host, line) tuples for flagged `job="syslog"` lines, oldest first.

    The filter is pushed into the LogQL query itself (RE2, same syntax as the
    compiled Python pattern) so the volume cap applies to matching lines, not
    raw chatter — a busy multi-host stream can otherwise exhaust the cap on
    routine noise long before reaching the line that actually matters.
    """
    query = f'{{job="syslog"}} |~ `(?i)(?:{NETWORK_LOG_TERMS})` !~ `(?i){NETWORK_LOG_EXCLUDE_TERM}`'
    rows = query_range(cfg, query, window_minutes, limit)
    # Re-check client-side: RE2 vs Python re can diverge at the edges.
    return [
        (ts, host, line) for ts, host, line in rows
        if _NETWORK_LOG_PATTERN.search(line) and not _NETWORK_LOG_EXCLUDE.search(line)
    ]


def fetch_pihole_events(
    cfg: LokiConfig, window_minutes: int, limit: int = 2000
) -> list[tuple[str, str, str]]:
    """(iso_ts, host, line) tuples for flagged `job="pihole"` lines, oldest first."""
    query = f'{{job="pihole"}} |~ `(?i)(?:{PIHOLE_LOG_TERMS})`'
    rows = query_range(cfg, query, window_minutes, limit)
    return [(ts, host, line) for ts, host, line in rows if _PIHOLE_LOG_PATTERN.search(line)]
