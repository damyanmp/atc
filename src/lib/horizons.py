"""NASA/JPL Horizons API client — spacecraft ephemeris (position & velocity).

Reference: https://ssd.jpl.nasa.gov/api/horizons.api

Usage::

    result = await horizons_vectors(naif_id=-1024, center="500@399")
    if result:
        dist_km, speed_km_s = result

Known spacecraft NAIF IDs
  -125544  International Space Station (ISS)
  -1023    Artemis I  / Orion EM-1  (2022-156A)
  -1024    Artemis II / Orion EM-2  (2026-069A, nickname "Integrity")
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

HORIZONS_API = "https://ssd.jpl.nasa.gov/api/horizons.api"

# Match position and velocity lines in a VECTORS table, e.g.:
#  X =-4.167610090861469E+03 Y = 6.427774643992089E+03 Z = 4.774057898683686E+02
#  VX=-9.926859512834213E+00 VY=-1.868923074835082E+00 VZ=-3.356176930872550E-01
_POS_RE = re.compile(
    r"X\s*=\s*(-?[\d.E+\-]+)\s+Y\s*=\s*(-?[\d.E+\-]+)\s+Z\s*=\s*(-?[\d.E+\-]+)"
)
_VEL_RE = re.compile(
    r"VX\s*=\s*(-?[\d.E+\-]+)\s+VY\s*=\s*(-?[\d.E+\-]+)\s+VZ\s*=\s*(-?[\d.E+\-]+)"
)
# Match only data epoch lines (start with the Julian Date), e.g.:
#  2461134.541666667 = A.D. 2026-Apr-04 01:00:00.0000 TDB
# The header "Start time / Stop time" lines also contain "A.D." but do NOT
# start with a Julian Date number, so anchoring on ^\d avoids false matches.
_EPOCH_RE = re.compile(
    r"^\d+\.\d+\s*=\s*A\.D\.\s+(\d{4}-[A-Za-z]{3}-\d{2}\s+\d{2}:\d{2}:\d{2})",
    re.MULTILINE,
)


def _date_only(dt: datetime) -> str:
    """Format a UTC datetime as YYYY-Mon-DD — the only format Horizons reliably
    accepts when parameters are URL-encoded (colons get percent-encoded)."""
    return dt.strftime("%Y-%b-%d")


def parse_vectors(
    text: str,
    at: Optional[datetime] = None,
) -> Optional[tuple[float, float, float, float, float, float]]:
    """Return (x, y, z, vx, vy, vz) from the SOE entry closest to *at*.

    Parses all epoch timestamps from the VECTORS table and selects the entry
    whose timestamp is nearest to *at* (defaults to now UTC).  Falls back to
    the last entry if timestamps cannot be parsed.
    """
    pos_matches = _POS_RE.findall(text)
    vel_matches = _VEL_RE.findall(text)
    if not pos_matches or not vel_matches:
        return None

    # Try to find the entry closest to the requested time.
    epoch_strs = _EPOCH_RE.findall(text)
    if epoch_strs and len(epoch_strs) == len(pos_matches) == len(vel_matches):
        if at is None:
            at = datetime.now(tz=timezone.utc)
        best_idx = 0
        best_delta = float("inf")
        for i, es in enumerate(epoch_strs):
            try:
                # Horizons uses e.g. "2026-Apr-04 15:00:00" (no tz — it's TDB ≈ UTC)
                ep = datetime.strptime(es.strip(), "%Y-%b-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                delta = abs((ep - at).total_seconds())
                if delta < best_delta:
                    best_delta = delta
                    best_idx = i
            except ValueError:
                continue
        idx = best_idx
    else:
        idx = -1  # fallback: last entry

    x, y, z = (float(v) for v in pos_matches[idx])
    vx, vy, vz = (float(v) for v in vel_matches[idx])
    return x, y, z, vx, vy, vz


def distance_speed(text: str, at: Optional[datetime] = None) -> Optional[tuple[float, float]]:
    """Return (distance_km, speed_km_s) from the SOE entry closest to *at*."""
    v = parse_vectors(text, at=at)
    if v is None:
        return None
    x, y, z, vx, vy, vz = v
    dist = math.sqrt(x**2 + y**2 + z**2)
    speed = math.sqrt(vx**2 + vy**2 + vz**2)
    return dist, speed


async def fetch_vectors(
    client: httpx.AsyncClient,
    naif_id: int,
    center: str = "500@399",
    at: Optional[datetime] = None,
) -> str:
    """Query Horizons VECTORS table and return raw result text.

    Queries today's full 24-hour window at 1-hour resolution so that
    ``parse_vectors`` / ``distance_speed`` can pick the most recent entry
    without needing a time-of-day in the URL (colons break URL encoding).

    *center* examples:
      ``500@399``  geocentre (Earth)
      ``500@301``  Moon centre
      ``500@10``   Sun centre

    Raises ``httpx.HTTPStatusError`` on non-2xx responses.
    """
    if at is None:
        at = datetime.now(tz=timezone.utc)
    # Use date-only to avoid colon URL-encoding issues; window = today + tomorrow
    start = _date_only(at)
    stop = _date_only(at + timedelta(days=1))

    params = {
        "format": "json",
        "COMMAND": str(naif_id),
        "EPHEM_TYPE": "VECTORS",
        "CENTER": center,
        "START_TIME": start,
        "STOP_TIME": stop,
        "STEP_SIZE": "1h",
        "VEC_TABLE": "2",        # XYZ + VXVYVZ only (compact)
        "VEC_CORR": "NONE",
        "OUT_UNITS": "KM-S",
        "REF_PLANE": "FRAME",
        "REF_SYSTEM": "J2000",
        "CSV_FORMAT": "NO",
    }
    resp = await client.get(HORIZONS_API, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("result", "")
