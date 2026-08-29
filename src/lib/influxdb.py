"""InfluxDB 2.x (Flux) query client.

Mirrors lib/prometheus.py's shape — query-only, no writes, even though the
token configured for this instance may be all-access (see config.yaml
comment). Used for data sources that push into InfluxDB directly rather
than being scraped by Prometheus (e.g. Proxmox's built-in metric server —
see the 'proxmox' bucket: cpustat/memory/blockstat/nics/system per guest,
independent of the docker-1/docker-2-only Prometheus/cadvisor pipeline).
"""
from __future__ import annotations

from dataclasses import dataclass, field as _dc_field

import httpx

from config import InfluxDBConfig


@dataclass
class FluxRow:
    """One CSV row from a Flux query result, plus a few commonly-used fields
    pulled out for convenience. `raw` keeps every column for anything else."""
    measurement: str
    field: str
    value: object
    time: str
    tags: dict[str, str] = _dc_field(default_factory=dict)
    raw: dict[str, str] = _dc_field(default_factory=dict)


def _parse_flux_csv(text: str) -> list[FluxRow]:
    """Parse InfluxDB's annotated-CSV Flux response into FluxRow objects.

    Skips comment/annotation lines (start with '#') and empty lines (which
    separate result tables). Only the columns needed for typical
    field-per-row queries are pulled out specially; everything else is kept
    in `raw` so callers can inspect it if needed.
    """
    import csv
    import io

    rows: list[FluxRow] = []
    header: list[str] | None = None
    for line in io.StringIO(text):
        line = line.rstrip("\n").rstrip("\r")
        if not line or line.startswith("#"):
            header = None  # a blank line separates tables; re-read header next
            continue
        cells = next(csv.reader([line]))
        if header is None:
            header = cells
            continue
        row = dict(zip(header, cells))
        tags = {
            k: v for k, v in row.items()
            if k not in ("", "result", "table", "_start", "_stop", "_time",
                         "_value", "_field", "_measurement") and v
        }
        rows.append(FluxRow(
            measurement=row.get("_measurement", ""),
            field=row.get("_field", ""),
            value=row.get("_value", ""),
            time=row.get("_time", ""),
            tags=tags,
            raw=row,
        ))
    return rows


def query_flux(cfg: InfluxDBConfig, flux: str, timeout: int = 30) -> list[FluxRow]:
    """Run a raw Flux query string and return parsed rows."""
    url = f"{cfg.base_url}/api/v2/query"
    with httpx.Client(verify=cfg.verify_ssl, timeout=timeout) as client:
        r = client.post(
            url,
            params={"org": cfg.org},
            headers={
                "Authorization": f"Token {cfg.token}",
                "Content-Type": "application/vnd.flux",
                "Accept": "application/csv",
            },
            content=flux,
        )
        if not r.is_success:
            raise RuntimeError(f"InfluxDB query HTTP {r.status_code}: {r.text[:300]}")
    return _parse_flux_csv(r.text)


def list_buckets(cfg: InfluxDBConfig, timeout: int = 15) -> list[str]:
    url = f"{cfg.base_url}/api/v2/buckets"
    with httpx.Client(verify=cfg.verify_ssl, timeout=timeout) as client:
        r = client.get(url, headers={"Authorization": f"Token {cfg.token}"})
        if not r.is_success:
            raise RuntimeError(f"InfluxDB buckets HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
    return sorted(b["name"] for b in data.get("buckets", []))


def list_measurements(cfg: InfluxDBConfig, bucket: str, timeout: int = 15) -> list[str]:
    """Distinct measurement names in a bucket — cheap discovery query."""
    flux = f'''
import "influxdata/influxdb/schema"
schema.measurements(bucket: "{bucket}")
'''
    rows = query_flux(cfg, flux, timeout=timeout)
    return sorted({r.value for r in rows if r.value})


def query_range(
    cfg: InfluxDBConfig,
    bucket: str,
    measurement: str,
    window_minutes: int = 60,
    field_filter: str | None = None,
    timeout: int = 30,
) -> list[FluxRow]:
    """Convenience range query: all fields (or one, if field_filter given)
    for a measurement over the last `window_minutes`."""
    field_clause = (
        f'|> filter(fn: (r) => r._field == "{field_filter}")\n  ' if field_filter else ""
    )
    flux = f'''
from(bucket: "{bucket}")
  |> range(start: -{window_minutes}m)
  |> filter(fn: (r) => r._measurement == "{measurement}")
  {field_clause}|> sort(columns: ["_time"])
'''
    return query_flux(cfg, flux, timeout=timeout)


def _escape_tag(v: str) -> str:
    """Escape a tag key/value for InfluxDB line protocol (commas, spaces, equals)."""
    return str(v).replace(",", "\\,").replace(" ", "\\ ").replace("=", "\\=")


def _line_protocol(
    measurement: str,
    tags: dict[str, str],
    fields: dict[str, float | int | str | bool],
    timestamp_ns: int | None = None,
) -> str:
    """Build a single InfluxDB line-protocol row."""
    tag_str = "".join(f",{_escape_tag(k)}={_escape_tag(v)}" for k, v in tags.items() if v != "")

    field_parts = []
    for k, v in fields.items():
        if isinstance(v, bool):
            field_parts.append(f"{k}={'t' if v else 'f'}")
        elif isinstance(v, int):
            field_parts.append(f"{k}={v}i")
        elif isinstance(v, float):
            field_parts.append(f"{k}={v}")
        else:
            escaped = str(v).replace('"', '\\"')
            field_parts.append(f'{k}="{escaped}"')
    field_str = ",".join(field_parts)

    line = f"{measurement}{tag_str} {field_str}"
    if timestamp_ns is not None:
        line += f" {timestamp_ns}"
    return line


def write_points(
    cfg: InfluxDBConfig,
    bucket: str,
    measurement: str,
    points: list[dict],
    timeout: int = 30,
) -> None:
    """Write points to InfluxDB via the v2 line-protocol write API.

    Each point in `points` is {"tags": {...}, "fields": {...}, "time_ns": int|None}.
    Requires cfg.token to have write access to `bucket` (existing atc InfluxDB
    token is documented as all-access — see config.yaml comment).
    """
    lines = []
    for p in points:
        lines.append(_line_protocol(
            measurement,
            p.get("tags", {}),
            p["fields"],
            p.get("time_ns"),
        ))
    body = "\n".join(lines)

    url = f"{cfg.base_url}/api/v2/write"
    with httpx.Client(verify=cfg.verify_ssl, timeout=timeout) as client:
        r = client.post(
            url,
            params={"org": cfg.org, "bucket": bucket, "precision": "ns"},
            headers={
                "Authorization": f"Token {cfg.token}",
                "Content-Type": "text/plain; charset=utf-8",
            },
            content=body,
        )
        if not r.is_success:
            raise RuntimeError(f"InfluxDB write HTTP {r.status_code}: {r.text[:300]}")
