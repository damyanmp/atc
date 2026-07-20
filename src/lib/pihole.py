"""Pi-hole v6 REST API client.

Auth flow: POST /api/auth → SID → X-FTL-SID header → DELETE /api/auth on exit.

Mutating operations (pause/unpause/gravity) target ALL instances.
DNS reads target the primary; DNS writes target the primary (sync propagates).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from config import PiholeInstanceConfig


@dataclass
class PiholeStatus:
    name: str
    host: str
    online: bool
    blocking: bool = False
    timer: int | None = None   # None = indefinite disable, N = seconds remaining
    queries_today: int = 0
    blocked_today: int = 0
    blocked_pct: float = 0.0
    version: str = ""
    error: str = ""


@asynccontextmanager
async def _session(cfg: PiholeInstanceConfig):
    """Authenticate, yield (client, headers), then log out."""
    base = cfg.base_url
    sid: str | None = None
    async with httpx.AsyncClient(verify=cfg.verify_ssl, timeout=10) as client:
        try:
            resp = await client.post(f"{base}/api/auth", json={"password": cfg.password})
            resp.raise_for_status()
            sid = resp.json()["session"]["sid"]
            yield client, {"X-FTL-SID": sid}
        finally:
            if sid:
                try:
                    await client.delete(f"{base}/api/auth", headers={"X-FTL-SID": sid})
                except Exception:
                    pass


async def get_status(cfg: PiholeInstanceConfig) -> PiholeStatus:
    try:
        async with _session(cfg) as (client, headers):
            base = cfg.base_url
            blocking_r, summary_r, version_r = await asyncio.gather(
                client.get(f"{base}/api/dns/blocking", headers=headers),
                client.get(f"{base}/api/stats/summary", headers=headers),
                client.get(f"{base}/api/info/version", headers=headers),
                return_exceptions=True,
            )

            blocking, timer = True, None
            if not isinstance(blocking_r, Exception):
                blocking_r.raise_for_status()
                bd = blocking_r.json()
                blocking = bool(bd.get("blocking", True))
                timer = bd.get("timer")  # seconds remaining or null

            queries, blocked, pct = 0, 0, 0.0
            if not isinstance(summary_r, Exception):
                sd = summary_r.json().get("queries", {})
                queries = sd.get("total", 0)
                blocked = sd.get("blocked", 0)
                pct = round(sd.get("percent_blocked", 0.0), 1)

            version = ""
            if not isinstance(version_r, Exception):
                vd = version_r.json()
                version = (
                    vd.get("version", {})
                    .get("core", {})
                    .get("local", {})
                    .get("version", "")
                )

            return PiholeStatus(
                name=cfg.name,
                host=cfg.host,
                online=True,
                blocking=blocking,
                timer=timer,
                queries_today=queries,
                blocked_today=blocked,
                blocked_pct=pct,
                version=version,
            )
    except Exception as e:
        return PiholeStatus(name=cfg.name, host=cfg.host, online=False, error=str(e))


async def fetch_all_status(cfgs: list[PiholeInstanceConfig]) -> list[PiholeStatus]:
    return list(await asyncio.gather(*[get_status(cfg) for cfg in cfgs]))


# ── Blocking control ──────────────────────────────────────────────────────────

async def set_blocking(
    cfg: PiholeInstanceConfig,
    enabled: bool,
    timer: int | None = None,
) -> tuple[bool, str]:
    """Enable or disable blocking.  timer (seconds) only applies when disabling."""
    try:
        async with _session(cfg) as (client, headers):
            body: dict = {"blocking": enabled}
            if not enabled and timer is not None:
                body["timer"] = timer
            r = await client.post(
                f"{cfg.base_url}/api/dns/blocking",
                json=body,
                headers=headers,
            )
            r.raise_for_status()
            return True, ""
    except Exception as e:
        return False, str(e)


async def set_blocking_all(
    cfgs: list[PiholeInstanceConfig],
    enabled: bool,
    timer: int | None = None,
) -> list[tuple[bool, str]]:
    return list(await asyncio.gather(*[set_blocking(cfg, enabled, timer) for cfg in cfgs]))


# ── Custom DNS ────────────────────────────────────────────────────────────────

def _parse_hosts(raw: list[str]) -> list[dict]:
    """Convert dnsmasq-style host entries ("ip domain") to {"domain", "ip"} dicts."""
    out = []
    for entry in raw:
        parts = entry.strip().split(None, 1)
        if len(parts) == 2:
            out.append({"ip": parts[0], "domain": parts[1]})
    return out


async def list_custom_dns(cfg: PiholeInstanceConfig) -> list[dict]:
    """Returns list of {"domain": ..., "ip": ...} dicts from the primary.

    Pi-hole v6 FTL stores custom DNS under /api/config/dns/hosts as an array
    of dnsmasq-format strings ("ip domain").

    Raises on auth failure, network error, or unexpected API response so callers
    can surface a meaningful error instead of silently returning empty.
    """
    async with _session(cfg) as (client, headers):
        r = await client.get(f"{cfg.base_url}/api/config/dns/hosts", headers=headers)
        r.raise_for_status()
        data = r.json()
        # v6 config API: {"config": {"dns": {"hosts": ["ip domain", ...]}}}
        hosts = (
            data.get("config", {}).get("dns", {}).get("hosts")
            or data.get("dns", {}).get("hosts")
            or data.get("hosts")
            or []
        )
        if hosts and isinstance(hosts[0], str):
            return _parse_hosts(hosts)
        # Already {"domain", "ip"} shaped (future-proof)
        return hosts


async def add_custom_dns(
    cfg: PiholeInstanceConfig, domain: str, ip: str
) -> tuple[bool, str]:
    try:
        async with _session(cfg) as (client, headers):
            entry = quote(f"{ip} {domain}", safe="")
            r = await client.put(
                f"{cfg.base_url}/api/config/dns/hosts/{entry}",
                headers=headers,
            )
            r.raise_for_status()
            return True, ""
    except Exception as e:
        return False, str(e)


async def remove_custom_dns(
    cfg: PiholeInstanceConfig, domain: str, ip: str
) -> tuple[bool, str]:
    try:
        async with _session(cfg) as (client, headers):
            entry = quote(f"{ip} {domain}", safe="")
            r = await client.delete(
                f"{cfg.base_url}/api/config/dns/hosts/{entry}",
                headers=headers,
            )
            r.raise_for_status()
            return True, ""
    except Exception as e:
        return False, str(e)


# ── Network devices (clients seen making queries) ────────────────────────────

async def fetch_seen_ips(cfg: PiholeInstanceConfig) -> set[str]:
    """Return every IP address Pi-hole's network table has ever seen a query from.

    Uses GET /api/network/devices, which defaults to returning only the 10
    most recently active devices — pass a high max_devices/max_addresses to
    get the full picture instead of just the top talkers.
    """
    async with _session(cfg) as (client, headers):
        r = await client.get(
            f"{cfg.base_url}/api/network/devices",
            params={"max_devices": 1000, "max_addresses": 50},
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()
        return {
            addr["ip"]
            for device in data.get("devices", [])
            for addr in device.get("ips", [])
            if addr.get("ip")
        }


async def fetch_seen_ips_all(
    cfgs: list[PiholeInstanceConfig],
) -> tuple[set[str], list[tuple[str, str]]]:
    """Union of seen IPs across all Pi-hole instances.

    Returns (ips, errors) where errors is [(instance_name, message), ...] for
    instances that could not be queried, so a single unreachable instance
    doesn't hide the rest of the result.
    """
    results = await asyncio.gather(
        *[fetch_seen_ips(cfg) for cfg in cfgs], return_exceptions=True
    )
    seen: set[str] = set()
    errors: list[tuple[str, str]] = []
    for cfg, r in zip(cfgs, results):
        if isinstance(r, Exception):
            errors.append((cfg.name, str(r)))
        else:
            seen |= r
    return seen, errors


# ── Gravity ───────────────────────────────────────────────────────────────────

async def trigger_gravity(cfg: PiholeInstanceConfig) -> tuple[bool, str]:
    try:
        async with _session(cfg) as (client, headers):
            r = await client.post(f"{cfg.base_url}/api/action/gravity", headers=headers)
            r.raise_for_status()
            return True, ""
    except Exception as e:
        return False, str(e)


async def trigger_gravity_all(
    cfgs: list[PiholeInstanceConfig],
) -> list[tuple[bool, str]]:
    return list(await asyncio.gather(*[trigger_gravity(cfg) for cfg in cfgs]))
