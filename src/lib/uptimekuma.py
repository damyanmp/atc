"""Uptime Kuma API client.

Two modes:
  - api_key set  → uses /metrics (Prometheus endpoint, requires API key).
  - api_key empty → uses public status page API (slug must be a public page).

Generate an API key: Uptime Kuma → Settings → API Keys → Add API Key.

Outage history is always fetched from the status page heartbeat API regardless
of whether an api_key is set, since /metrics only exposes current state.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx


@dataclass
class UptimeKumaMonitor:
    id: int
    name: str
    group: str
    status: int         # 0=down, 1=up, 2=pending, 3=maintenance
    latency_ms: int     # most-recent ping in ms (0 if unknown)
    uptime_24h: float   # 0.0–1.0
    uptime_720h: float  # 0.0–1.0 (30 days)


# Status code → (display label, Rich colour)
_STATUS: dict[int, tuple[str, str]] = {
    0: ("DOWN",        "red bold"),
    1: ("UP",          "green"),
    2: ("PENDING",     "yellow"),
    3: ("MAINTENANCE", "blue"),
}

_METRIC_RE = re.compile(r'^(\w+)\{([^}]*)\}\s+([\d.eE+\-]+)', re.MULTILINE)
_LABEL_RE  = re.compile(r'(\w+)="([^"]*)"')


def status_label(s: int) -> tuple[str, str]:
    """Return (label, rich_color) for a monitor status code."""
    return _STATUS.get(s, ("UNKNOWN", "dim"))


def _parse_labels(s: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _LABEL_RE.finditer(s)}


def _fetch_via_metrics(cfg) -> list[UptimeKumaMonitor]:
    """Fetch monitor statuses from /metrics (Prometheus format, requires API key)."""
    cred = base64.b64encode(f":{cfg.api_key}".encode()).decode()
    headers = {"Authorization": f"Basic {cred}"}
    with httpx.Client(verify=cfg.verify_ssl, timeout=15) as client:
        r = client.get(f"{cfg.base_url}/metrics", headers=headers)
        if not r.is_success:
            raise RuntimeError(f"metrics HTTP {r.status_code}: {r.text[:200]}")
        text = r.text

    statuses:    dict[str, int]   = {}
    latencies:   dict[str, int]   = {}
    uptimes_24:  dict[str, float] = {}
    uptimes_720: dict[str, float] = {}
    names:       dict[str, str]   = {}

    for m in _METRIC_RE.finditer(text):
        metric = m.group(1)
        labels = _parse_labels(m.group(2))
        value  = float(m.group(3))
        mid    = labels.get("monitor_id", "")
        if not mid:
            continue

        names[mid] = labels.get("monitor_name") or labels.get("friendly_name") or f"#{mid}"

        if metric == "monitor_status":
            statuses[mid] = int(value)
        elif metric == "monitor_response_time":
            latencies[mid] = int(value)
        elif metric in ("monitor_uptime_ratio", "monitor_uptime"):
            # Newer Uptime Kuma: monitor_uptime_ratio with window="1d"/"30d"
            # Older Uptime Kuma: monitor_uptime with duration="24"/"720"
            win = labels.get("window") or labels.get("duration", "")
            if win in ("1d", "24"):
                uptimes_24[mid] = value
            elif win in ("30d", "720"):
                uptimes_720[mid] = value

    return [
        UptimeKumaMonitor(
            id          = int(mid) if mid.isdigit() else 0,
            name        = name,
            group       = "",
            status      = statuses.get(mid, 0),
            latency_ms  = latencies.get(mid, 0),
            uptime_24h  = uptimes_24.get(mid, 0.0),
            uptime_720h = uptimes_720.get(mid, 0.0),
        )
        for mid, name in sorted(names.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0)
    ]


def _fetch_via_status_page(cfg) -> list[UptimeKumaMonitor]:
    """Fetch monitors from the public status page API (no API key required)."""
    base = cfg.base_url
    slug = cfg.slug

    with httpx.Client(verify=cfg.verify_ssl, timeout=15) as client:
        page_r = client.get(f"{base}/api/status-page/{slug}")
        if not page_r.is_success:
            raise RuntimeError(
                f"status-page HTTP {page_r.status_code}: {page_r.text[:200]}"
            )
        page = page_r.json()

        hb_r = client.get(f"{base}/api/status-page/heartbeat/{slug}")
        if not hb_r.is_success:
            raise RuntimeError(
                f"heartbeat HTTP {hb_r.status_code}: {hb_r.text[:200]}"
            )
        hb = hb_r.json()

    heartbeats = hb.get("heartbeatList", {})
    uptimes    = hb.get("uptimeList", {})

    monitors: list[UptimeKumaMonitor] = []
    for group in page.get("publicGroupList", []):
        group_name = group.get("name", "")
        for m in group.get("monitorList", []):
            mid    = m["id"]
            key    = str(mid)
            hblist = heartbeats.get(key, [])
            latest = hblist[-1] if hblist else {}

            monitors.append(UptimeKumaMonitor(
                id          = mid,
                name        = m.get("name", f"#{mid}"),
                group       = group_name,
                status      = int(latest.get("status", 0)),
                latency_ms  = int(latest.get("ping") or 0),
                uptime_24h  = float(uptimes.get(f"{mid}_24",  0.0)),
                uptime_720h = float(uptimes.get(f"{mid}_720", 0.0)),
            ))

    return monitors


def fetch_monitors(cfg) -> list[UptimeKumaMonitor]:
    """Fetch all monitors. Uses /metrics if api_key is set, status page API otherwise."""
    if cfg.api_key:
        return _fetch_via_metrics(cfg)
    return _fetch_via_status_page(cfg)


# ── Outage history ─────────────────────────────────────────────────────────────

@dataclass
class UptimeKumaOutage:
    monitor_name: str
    monitor_id: int
    started_at: str    # ISO 8601 timestamp of first DOWN heartbeat
    ended_at: str      # ISO 8601 timestamp of recovery, or "ongoing"
    duration_min: int  # 0 if ongoing
    error_msg: str     # msg from first DOWN heartbeat


def _parse_ts(raw: str) -> datetime | None:
    """Parse an Uptime Kuma heartbeat timestamp (ISO 8601 with or without Z)."""
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _outages_from_heartbeats(
    monitor_name: str,
    monitor_id: int,
    heartbeats: list[dict],
) -> list[UptimeKumaOutage]:
    """Scan a chronological heartbeat list and return discrete outage events."""
    outages: list[UptimeKumaOutage] = []
    in_outage = False
    outage_start: datetime | None = None
    outage_msg = ""

    for hb in heartbeats:
        status = int(hb.get("status", 1))
        ts = _parse_ts(hb.get("time", ""))
        if status == 0:
            if not in_outage:
                in_outage = True
                outage_start = ts
                outage_msg = hb.get("msg", "")
        else:
            if in_outage:
                in_outage = False
                started = outage_start.isoformat() if outage_start else ""
                ended = ts.isoformat() if ts else ""
                dur = 0
                if outage_start and ts:
                    dur = int((ts - outage_start).total_seconds() / 60)
                outages.append(UptimeKumaOutage(
                    monitor_name=monitor_name,
                    monitor_id=monitor_id,
                    started_at=started,
                    ended_at=ended,
                    duration_min=dur,
                    error_msg=outage_msg,
                ))

    # Still in outage at end of list → mark ongoing
    if in_outage and outage_start:
        now = datetime.now(timezone.utc)
        dur = int((now - outage_start).total_seconds() / 60)
        outages.append(UptimeKumaOutage(
            monitor_name=monitor_name,
            monitor_id=monitor_id,
            started_at=outage_start.isoformat(),
            ended_at="ongoing",
            duration_min=dur,
            error_msg=outage_msg,
        ))

    return outages


def fetch_outages(cfg, limit: int = 10) -> list[UptimeKumaOutage]:
    """Return the most recent `limit` outage events across all monitors.

    Always uses the status page heartbeat API (slug required; defaults to
    "default"). This works even when api_key is set, since /metrics only
    provides current state — not historical heartbeats.

    Returns an empty list if no status page exists for the configured slug
    (HTTP 404) — this is not an error condition, just means no page is set up.
    """
    base = cfg.base_url
    slug = cfg.slug

    with httpx.Client(verify=cfg.verify_ssl, timeout=15) as client:
        page_r = client.get(f"{base}/api/status-page/{slug}")
        if page_r.status_code == 404:
            return []
        if not page_r.is_success:
            raise RuntimeError(
                f"status-page HTTP {page_r.status_code}: {page_r.text[:200]}"
            )
        page = page_r.json()

        hb_r = client.get(f"{base}/api/status-page/heartbeat/{slug}")
        if not hb_r.is_success:
            raise RuntimeError(
                f"heartbeat HTTP {hb_r.status_code}: {hb_r.text[:200]}"
            )
        hb_data = hb_r.json()

    heartbeats_by_id: dict[str, list[dict]] = hb_data.get("heartbeatList", {})

    # Build monitor id → name mapping from status page groups
    id_to_name: dict[str, str] = {}
    for group in page.get("publicGroupList", []):
        for m in group.get("monitorList", []):
            id_to_name[str(m["id"])] = m.get("name", f"#{m['id']}")

    all_outages: list[UptimeKumaOutage] = []
    for mid_str, hblist in heartbeats_by_id.items():
        name = id_to_name.get(mid_str, f"#{mid_str}")
        mid = int(mid_str) if mid_str.isdigit() else 0
        all_outages.extend(_outages_from_heartbeats(name, mid, hblist))

    # Sort most-recent first
    def _sort_key(o: UptimeKumaOutage) -> str:
        return o.started_at or ""

    all_outages.sort(key=_sort_key, reverse=True)
    return all_outages[:limit]
