"""Beszel hub API client — system health metrics.

Beszel is built on PocketBase. Auth uses a Bearer token (API key).
Endpoint: GET /api/collections/systems/records

Known `info` field keys (from Beszel agent):
  cpu   – CPU usage %
  mp    – memory used %
  dp    – disk used %
  u     – uptime seconds
  m     – memory used MiB
  mt    – memory total MiB
  ms    – swap used MiB
  mst   – swap total MiB
  mc    – cached memory MiB
  d     – disk used bytes
  dt    – disk total bytes
  b     – network bandwidth (bytes/s, combined)
  ns    – network bytes sent (rate, bytes/s)
  nr    – network bytes received (rate, bytes/s)
  dr    – disk read rate (bytes/s)
  dw    – disk write rate (bytes/s)
  v     – agent version string
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from config import BeszelConfig


@dataclass
class BeszelSystem:
    name: str
    host: str
    status: str              # "up", "down", "paused", "pending"
    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    disk_percent: float = 0.0
    uptime_seconds: int = 0  # from info.u
    # Memory absolute (MiB)
    mem_used_mib: float = 0.0
    mem_total_mib: float = 0.0
    swap_used_mib: float = 0.0
    swap_total_mib: float = 0.0
    # Disk absolute (bytes)
    disk_used_bytes: int = 0
    disk_total_bytes: int = 0
    # Network rates (bytes/s)
    net_sent_bps: float = 0.0
    net_recv_bps: float = 0.0
    # Disk I/O rates (bytes/s)
    disk_read_bps: float = 0.0
    disk_write_bps: float = 0.0
    error: str = ""
    raw_info: dict = field(default_factory=dict)

    @property
    def online(self) -> bool:
        return self.status == "up"


async def fetch_systems(cfg: BeszelConfig) -> list[BeszelSystem]:
    """Fetch all systems from the Beszel hub API."""
    try:
        async with httpx.AsyncClient(verify=cfg.verify_ssl) as client:
            resp = await client.get(
                f"{cfg.base_url}/api/collections/systems/records",
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                params={"perPage": 500},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            systems: list[BeszelSystem] = []
            for item in data.get("items", []):
                # Beszel stores latest stats directly in the `info` field
                info: dict = item.get("info") or {}

                systems.append(BeszelSystem(
                    name=item.get("name") or item.get("host", "?"),
                    host=item.get("host", ""),
                    status=item.get("status", "down"),
                    cpu_percent=float(info.get("cpu", 0.0)),
                    mem_percent=float(info.get("mp", 0.0)),
                    disk_percent=float(info.get("dp", 0.0)),
                    uptime_seconds=int(info.get("u", 0)),
                    mem_used_mib=float(info.get("m", 0.0)),
                    mem_total_mib=float(info.get("mt", 0.0)),
                    swap_used_mib=float(info.get("ms", 0.0)),
                    swap_total_mib=float(info.get("mst", 0.0)),
                    disk_used_bytes=int(info.get("d", 0)),
                    disk_total_bytes=int(info.get("dt", 0)),
                    net_sent_bps=float(info.get("ns", 0.0)),
                    net_recv_bps=float(info.get("nr", 0.0)),
                    disk_read_bps=float(info.get("dr", 0.0)),
                    disk_write_bps=float(info.get("dw", 0.0)),
                    raw_info=info,
                ))

            return sorted(systems, key=lambda s: s.name)

    except Exception as e:
        return [BeszelSystem(name="beszel", host=cfg.host, status="down", error=str(e))]


async def fetch_raw_info(cfg: BeszelConfig) -> list[dict]:
    """Return the raw `info` dict per system — use this to discover available fields."""
    try:
        async with httpx.AsyncClient(verify=cfg.verify_ssl) as client:
            resp = await client.get(
                f"{cfg.base_url}/api/collections/systems/records",
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                params={"perPage": 500},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return [
                {"name": item.get("name", "?"), "info": item.get("info") or {}}
                for item in data.get("items", [])
            ]
    except Exception as e:
        return [{"error": str(e)}]
