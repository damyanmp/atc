"""Prometheus query client.

Minimal wrapper around Prometheus's HTTP query API — mirrors loki.py's shape
(query() / query_range()) so `atc metrics` behaves like `atc` Loki access:
ad-hoc PromQL from the CLI, no auth (self-hosted, LAN-only, same posture as
the existing prometheus config used by `atc blackbox` for alert rules).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from config import PrometheusConfig


@dataclass
class InstantResult:
    metric: dict[str, str]
    value: float
    timestamp: float


@dataclass
class RangeResult:
    metric: dict[str, str]
    values: list[tuple[float, float]] = field(default_factory=list)  # (timestamp, value)


def _raise_on_error(data: dict) -> None:
    if data.get("status") != "success":
        err = data.get("error") or data
        raise RuntimeError(f"Prometheus query failed: {err}")


def query(cfg: PrometheusConfig, promql: str, timeout: int = 15) -> list[InstantResult]:
    """Instant query — current value of `promql` (e.g. 'up', 'node_load1')."""
    url = f"{cfg.base_url}/api/v1/query"
    with httpx.Client(verify=cfg.verify_ssl, timeout=timeout) as client:
        r = client.get(url, params={"query": promql})
        if not r.is_success:
            raise RuntimeError(f"Prometheus query HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
    _raise_on_error(data)

    result = data.get("data", {}).get("result", [])
    out: list[InstantResult] = []
    for item in result:
        metric = item.get("metric", {})
        ts, val = item.get("value", [None, None])
        if ts is None:
            continue
        out.append(InstantResult(metric=metric, value=float(val), timestamp=float(ts)))
    return out


def query_range(
    cfg: PrometheusConfig,
    promql: str,
    window_minutes: int,
    step_seconds: int = 60,
    timeout: int = 30,
) -> list[RangeResult]:
    """Range query over the last `window_minutes`, sampled every `step_seconds`."""
    now = time.time()
    start = now - (window_minutes * 60)

    url = f"{cfg.base_url}/api/v1/query_range"
    params = {
        "query": promql,
        "start": str(start),
        "end": str(now),
        "step": str(step_seconds),
    }
    with httpx.Client(verify=cfg.verify_ssl, timeout=timeout) as client:
        r = client.get(url, params=params)
        if not r.is_success:
            raise RuntimeError(f"Prometheus query_range HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
    _raise_on_error(data)

    result = data.get("data", {}).get("result", [])
    out: list[RangeResult] = []
    for item in result:
        metric = item.get("metric", {})
        values = [(float(ts), float(val)) for ts, val in item.get("values", [])]
        out.append(RangeResult(metric=metric, values=values))
    return out


def list_metric_names(cfg: PrometheusConfig, match: str = "", timeout: int = 15) -> list[str]:
    """Label-values lookup for __name__ — cheap way to discover available metrics,
    optionally filtered by a substring match applied client-side."""
    url = f"{cfg.base_url}/api/v1/label/__name__/values"
    with httpx.Client(verify=cfg.verify_ssl, timeout=timeout) as client:
        r = client.get(url)
        if not r.is_success:
            raise RuntimeError(f"Prometheus label query HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
    _raise_on_error(data)

    names = data.get("data", [])
    if match:
        m = match.lower()
        names = [n for n in names if m in n.lower()]
    return sorted(names)
