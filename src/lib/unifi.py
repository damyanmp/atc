"""UniFi Network API client.

Supports two auth modes:
  - API key  (UniFi Network Application 8.x+, recommended)
    Generate: Network UI → Settings → Control Plane → Integrations → Create API Key
    Set  `api_key: keychain:unifi-api-key`  in config.yaml.

  - Username / password  (legacy — all controller versions)
    Set  `username`  and  `password: keychain:unifi-password`  in config.yaml.

UDM / UniFi OS devices expose the Network API under the  /proxy/network/  prefix.
Older standalone controllers use direct  /api/  paths.
Set  `udm: true`  (default) or  `udm: false`  in config.yaml accordingly.

Two API generations are supported:
  - Legacy  /proxy/network/api/s/{site}/rest/  — Network <8.x, returns {"data": [...]}
  - Integration  /proxy/network/integration/v1/sites/{siteId}/  — Network 8.x+, zone-based
    firewall model.  Use fetch_sites_v1() to resolve the siteId UUID first.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from config import UniFiConfig


# ── Internal helpers ──────────────────────────────────────────────────────────

def _api_prefix(cfg: UniFiConfig) -> str:
    return "/proxy/network" if cfg.udm else ""


def _client(cfg: UniFiConfig) -> httpx.Client:
    return httpx.Client(verify=cfg.verify_ssl, timeout=15)


def _auth_headers(cfg: UniFiConfig) -> dict:
    if cfg.api_key:
        return {"X-API-KEY": cfg.api_key}
    return {}


def _login(client: httpx.Client, cfg: UniFiConfig) -> None:
    """Perform cookie-based login for username/password auth."""
    url = f"{cfg.base_url}/api/auth/login" if cfg.udm else f"{cfg.base_url}/api/login"
    r = client.post(url, json={"username": cfg.username, "password": cfg.password})
    if not r.is_success:
        raise RuntimeError(f"UniFi login failed HTTP {r.status_code}: {r.text[:200]}")


def _get(cfg: UniFiConfig, path: str) -> dict:
    """GET a legacy UniFi API path.  Returns the parsed JSON (dict or list)."""
    url = f"{cfg.base_url}{_api_prefix(cfg)}{path}"
    if cfg.api_key:
        with _client(cfg) as client:
            r = client.get(url, headers=_auth_headers(cfg))
            if not r.is_success:
                raise RuntimeError(f"UniFi GET {path} HTTP {r.status_code}: {r.text[:200]}")
            return r.json()
    else:
        with _client(cfg) as client:
            _login(client, cfg)
            r = client.get(url)
            if not r.is_success:
                raise RuntimeError(f"UniFi GET {path} HTTP {r.status_code}: {r.text[:200]}")
            return r.json()


def _get_v1(cfg: UniFiConfig, path: str):
    """GET an integration v1 API path.

    Base: /proxy/network/integration/v1{path}
    Returns the parsed JSON as-is (may be a dict or a list depending on endpoint).
    """
    base = f"{cfg.base_url}/proxy/network/integration/v1" if cfg.udm else f"{cfg.base_url}/integration/v1"
    url = f"{base}{path}"
    with _client(cfg) as client:
        r = client.get(url, headers=_auth_headers(cfg))
        if not r.is_success:
            raise RuntimeError(f"UniFi v1 GET {path} HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class UniFiFirewallRule:
    id: str
    name: str
    enabled: bool
    ruleset: str        # e.g. "WAN_IN", "LAN_IN", "GUEST_IN", "LAN_LOCAL", etc.
    rule_index: int
    action: str         # "accept", "drop", "reject"
    protocol: str       # "all", "tcp", "udp", "tcp_udp", "icmp"
    src_firewallgroup_ids: list[str]
    dst_firewallgroup_ids: list[str]
    src_address: str    # single IP or CIDR, empty if using groups
    dst_address: str
    src_net_id: str     # network ID for source, empty if not set
    dst_net_id: str
    icmp_typename: str
    logging: bool
    state_new: bool
    state_established: bool
    state_invalid: bool
    state_related: bool
    ipsec: str          # "match-ipsec", "match-none", "" (any)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def direction_label(self) -> str:
        """Human-readable direction from the ruleset name."""
        mapping = {
            "WAN_IN": "WAN → LAN",
            "WAN_OUT": "LAN → WAN",
            "WAN_LOCAL": "WAN → Router",
            "LAN_IN": "LAN → LAN",
            "LAN_OUT": "LAN → Egress",
            "LAN_LOCAL": "LAN → Router",
            "GUEST_IN": "Guest → LAN",
            "GUEST_OUT": "LAN → Guest",
            "GUEST_LOCAL": "Guest → Router",
        }
        return mapping.get(self.ruleset, self.ruleset)

    @property
    def state_flags(self) -> str:
        """Comma-separated list of tracked state flags."""
        flags = []
        if self.state_new:
            flags.append("new")
        if self.state_established:
            flags.append("established")
        if self.state_related:
            flags.append("related")
        if self.state_invalid:
            flags.append("invalid")
        return ",".join(flags) if flags else "any"


@dataclass
class UniFiFirewallGroup:
    id: str
    name: str
    group_type: str     # "address-group", "port-group", "ipv6-address-group"
    members: list[str]  # IPs/CIDRs or ports depending on group_type


@dataclass
class UniFiNetwork:
    id: str
    name: str
    purpose: str        # "corporate", "guest", "vlan-only", "wan", etc.
    vlan_id: int        # 0 = no VLAN tag (native)
    subnet: str         # e.g. "192.168.10.0/24", empty for WAN
    ip_subnet: str      # gateway CIDR e.g. "192.168.10.1/24"
    dhcp_enabled: bool
    dhcp_start: str
    dhcp_stop: str
    enabled: bool
    raw: dict = field(default_factory=dict, repr=False)


# ── Integration v1 data models (zone-based firewall, Network 8.x+) ────────────

@dataclass
class UniFiSite:
    id: str             # UUID used in v1 API paths
    name: str
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class UniFiFirewallZone:
    id: str
    name: str
    network_ids: list[str]
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class UniFiFirewallPolicy:
    id: str
    name: str
    enabled: bool
    action: str                 # "ALLOW", "BLOCK", "REJECT" (from action.type)
    allow_return_traffic: bool  # stateful: allow established/related reply traffic
    src_zone_id: str
    dst_zone_id: str
    src_zone_name: str          # resolved from zone map
    dst_zone_name: str
    src_traffic_filter: dict    # raw trafficFilter object from source
    dst_traffic_filter: dict    # raw trafficFilter object from destination
    ip_version: str             # "IPV4", "IPV6", "BOTH"
    logging: bool
    index: int                  # display/priority order
    origin: str                 # "USER_DEFINED", "SYSTEM", etc.
    raw: dict = field(default_factory=dict, repr=False)


# ── Fetch functions ───────────────────────────────────────────────────────────

def fetch_firewall_rules(cfg: UniFiConfig, site: str = "default") -> list[UniFiFirewallRule]:
    """Return all firewall rules for the given site, ordered by ruleset and index."""
    data = _get(cfg, f"/api/s/{site}/rest/firewallrule")
    rules = []
    for r in data.get("data", []):
        rules.append(UniFiFirewallRule(
            id=r.get("_id", ""),
            name=r.get("name", ""),
            enabled=r.get("enabled", True),
            ruleset=r.get("ruleset", ""),
            rule_index=int(r.get("rule_index", 0)),
            action=r.get("action", ""),
            protocol=r.get("protocol", "all"),
            src_firewallgroup_ids=r.get("src_firewallgroup_ids", []),
            dst_firewallgroup_ids=r.get("dst_firewallgroup_ids", []),
            src_address=r.get("src_address", ""),
            dst_address=r.get("dst_address", ""),
            src_net_id=r.get("src_networkconf_id", ""),
            dst_net_id=r.get("dst_networkconf_id", ""),
            icmp_typename=r.get("icmp_typename", ""),
            logging=r.get("logging", False),
            state_new=r.get("state_new", False),
            state_established=r.get("state_established", False),
            state_invalid=r.get("state_invalid", False),
            state_related=r.get("state_related", False),
            ipsec=r.get("ipsec", ""),
            raw=r,
        ))
    return sorted(rules, key=lambda x: (x.ruleset, x.rule_index))


def fetch_firewall_groups(cfg: UniFiConfig, site: str = "default") -> list[UniFiFirewallGroup]:
    """Return all firewall groups (address-groups and port-groups)."""
    data = _get(cfg, f"/api/s/{site}/rest/firewallgroup")
    return [
        UniFiFirewallGroup(
            id=g.get("_id", ""),
            name=g.get("name", ""),
            group_type=g.get("group_type", ""),
            members=g.get("group_members", []),
        )
        for g in data.get("data", [])
    ]


def fetch_networks(cfg: UniFiConfig, site: str = "default") -> list[UniFiNetwork]:
    """Return all configured networks (VLANs, WAN, etc.)."""
    data = _get(cfg, f"/api/s/{site}/rest/networkconf")
    networks = []
    for n in data.get("data", []):
        networks.append(UniFiNetwork(
            id=n.get("_id", ""),
            name=n.get("name", ""),
            purpose=n.get("purpose", ""),
            vlan_id=int(n.get("vlan", 0)),
            subnet=n.get("ip_subnet", ""),
            ip_subnet=n.get("ip_subnet", ""),
            dhcp_enabled=n.get("dhcpd_enabled", False),
            dhcp_start=n.get("dhcpd_start", ""),
            dhcp_stop=n.get("dhcpd_stop", ""),
            enabled=n.get("enabled", True),
            raw=n,
        ))
    return sorted(networks, key=lambda x: (x.purpose, x.vlan_id))


# ── Integration v1 fetch functions ───────────────────────────────────────────

def fetch_sites_v1(cfg: UniFiConfig) -> list[UniFiSite]:
    """Return all sites from the integration v1 API (needed to resolve siteId UUIDs)."""
    data = _get_v1(cfg, "/sites")
    items = data if isinstance(data, list) else data.get("data", [])
    return [
        UniFiSite(
            id=s.get("siteId") or s.get("id", ""),
            name=s.get("name", ""),
            raw=s,
        )
        for s in items
    ]


def fetch_firewall_zones_v1(cfg: UniFiConfig, site_id: str) -> list[UniFiFirewallZone]:
    """Return all firewall zones for the given site (integration v1 API)."""
    data = _get_v1(cfg, f"/sites/{site_id}/firewall/zones")
    items = data if isinstance(data, list) else data.get("data", [])
    return [
        UniFiFirewallZone(
            id=z.get("id", ""),
            name=z.get("name", ""),
            network_ids=z.get("networkIds", []),
            raw=z,
        )
        for z in items
    ]


def fetch_firewall_policies_v1(
    cfg: UniFiConfig,
    site_id: str,
    zones: list[UniFiFirewallZone] | None = None,
) -> list[UniFiFirewallPolicy]:
    """Return all firewall policies for the given site (integration v1 API).

    Pass `zones` to resolve zone IDs to names; otherwise zone names are left empty.
    """
    data = _get_v1(cfg, f"/sites/{site_id}/firewall/policies")
    items = data if isinstance(data, list) else data.get("data", [])

    zone_map = {z.id: z.name for z in (zones or [])}

    policies = []
    for i, p in enumerate(items):
        src = p.get("source", {})
        dst = p.get("destination", {})
        action_obj = p.get("action", {})
        src_id = src.get("zoneId", "")
        dst_id = dst.get("zoneId", "")
        policies.append(UniFiFirewallPolicy(
            id=p.get("id", ""),
            name=p.get("name", ""),
            enabled=p.get("enabled", True),
            action=action_obj.get("type", ""),
            allow_return_traffic=action_obj.get("allowReturnTraffic", False),
            src_zone_id=src_id,
            dst_zone_id=dst_id,
            src_zone_name=zone_map.get(src_id, src_id),
            dst_zone_name=zone_map.get(dst_id, dst_id),
            src_traffic_filter=src.get("trafficFilter", {}),
            dst_traffic_filter=dst.get("trafficFilter", {}),
            ip_version=p.get("ipProtocolScope", {}).get("ipVersion", "IPV4"),
            logging=p.get("loggingEnabled", False),
            index=p.get("index", i),
            origin=p.get("metadata", {}).get("origin", ""),
            raw=p,
        ))
    return sorted(policies, key=lambda x: (x.src_zone_name, x.dst_zone_name, x.index))


# ── Export ────────────────────────────────────────────────────────────────────

def export_firewall_config(
    cfg: UniFiConfig,
    output_path: Path,
    site: str = "default",
) -> dict:
    """Fetch all firewall rules, groups, and networks and write to a JSON file.

    Returns the exported dict so the caller can inspect counts without re-reading.
    """
    rules = fetch_firewall_rules(cfg, site)
    groups = fetch_firewall_groups(cfg, site)
    networks = fetch_networks(cfg, site)

    group_map = {g.id: g.name for g in groups}

    export = {
        "site": site,
        "networks": [n.raw for n in networks],
        "firewall_groups": [
            {"id": g.id, "name": g.name, "type": g.group_type, "members": g.members}
            for g in groups
        ],
        "firewall_rules": [
            {
                "id": r.id,
                "name": r.name,
                "enabled": r.enabled,
                "ruleset": r.ruleset,
                "direction": r.direction_label,
                "index": r.rule_index,
                "action": r.action,
                "protocol": r.protocol,
                "src_groups": [group_map.get(gid, gid) for gid in r.src_firewallgroup_ids],
                "dst_groups": [group_map.get(gid, gid) for gid in r.dst_firewallgroup_ids],
                "src_address": r.src_address,
                "dst_address": r.dst_address,
                "states": r.state_flags,
                "logging": r.logging,
                "ipsec": r.ipsec,
            }
            for r in rules
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(export, f, indent=2)

    return export


def print_firewall_summary(
    rules: list[UniFiFirewallRule],
    groups: list[UniFiFirewallGroup],
    networks: list[UniFiNetwork],
) -> None:
    """Print a human-readable summary of the firewall config to stdout."""
    group_map = {g.id: g.name for g in groups}
    net_map = {n.id: n.name for n in networks}

    # Group rules by ruleset
    by_ruleset: dict[str, list[UniFiFirewallRule]] = {}
    for r in rules:
        by_ruleset.setdefault(r.ruleset, []).append(r)

    print(f"\n{'─'*70}")
    print(f"  UniFi Firewall Rules  ({len(rules)} total)")
    print(f"{'─'*70}")

    for ruleset, rs in sorted(by_ruleset.items()):
        direction = rs[0].direction_label if rs else ruleset
        print(f"\n  [{ruleset}]  {direction}")
        for r in rs:
            status = "✓" if r.enabled else "✗"
            src_parts = []
            if r.src_address:
                src_parts.append(r.src_address)
            if r.src_net_id:
                src_parts.append(f"net:{net_map.get(r.src_net_id, r.src_net_id)}")
            for gid in r.src_firewallgroup_ids:
                src_parts.append(f"grp:{group_map.get(gid, gid)}")
            src = ", ".join(src_parts) or "any"

            dst_parts = []
            if r.dst_address:
                dst_parts.append(r.dst_address)
            if r.dst_net_id:
                dst_parts.append(f"net:{net_map.get(r.dst_net_id, r.dst_net_id)}")
            for gid in r.dst_firewallgroup_ids:
                dst_parts.append(f"grp:{group_map.get(gid, gid)}")
            dst = ", ".join(dst_parts) or "any"

            proto = r.protocol if r.protocol != "all" else "*"
            log = " [log]" if r.logging else ""
            print(
                f"    {status} {r.rule_index:>4}  {r.action:<6}  "
                f"{proto:<8}  {src:<30}  →  {dst}{log}"
            )
            print(f"           name: {r.name}")

    print(f"\n  Networks ({len(networks)})")
    for n in networks:
        vlan = f"VLAN {n.vlan_id}" if n.vlan_id else "untagged"
        print(f"    {n.name:<25} {vlan:<10} {n.subnet or '(no subnet)'}  [{n.purpose}]")

    print(f"\n  Firewall Groups ({len(groups)})")
    for g in groups:
        mcount = len(g.members)
        preview = ", ".join(g.members[:3])
        if mcount > 3:
            preview += f", … (+{mcount-3})"
        print(f"    {g.name:<30} {g.group_type:<20} {preview}")

    print()


def export_firewall_config_v1(
    cfg: UniFiConfig,
    output_path: Path,
    site_name: str = "default",
) -> dict:
    """Fetch zone-based firewall config via integration v1 API and write to JSON.

    Resolves the siteId UUID automatically by matching `site_name` against /sites.
    Returns the exported dict.
    """
    sites = fetch_sites_v1(cfg)
    site = next((s for s in sites if s.name.lower() == site_name.lower()), None)
    if site is None and sites:
        site = sites[0]  # fall back to first site
    if site is None:
        raise RuntimeError("No sites returned from UniFi integration API")

    zones = fetch_firewall_zones_v1(cfg, site.id)
    policies = fetch_firewall_policies_v1(cfg, site.id, zones)
    networks = fetch_networks(cfg, site_name)
    groups = fetch_firewall_groups(cfg, site_name)

    export = {
        "site": {"id": site.id, "name": site.name},
        "zones": [z.raw for z in zones],
        "firewall_policies": [
            {
                "id": p.id,
                "name": p.name,
                "enabled": p.enabled,
                "action": p.action,
                "allow_return_traffic": p.allow_return_traffic,
                "src_zone": p.src_zone_name,
                "dst_zone": p.dst_zone_name,
                "src_traffic_filter": p.src_traffic_filter,
                "dst_traffic_filter": p.dst_traffic_filter,
                "ip_version": p.ip_version,
                "logging": p.logging,
                "index": p.index,
                "origin": p.origin,
            }
            for p in policies
        ],
        "networks": [n.raw for n in networks],
        "firewall_groups": [
            {"id": g.id, "name": g.name, "type": g.group_type, "members": g.members}
            for g in groups
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(export, f, indent=2)

    return export


def print_firewall_policy_summary(
    policies: list[UniFiFirewallPolicy],
    zones: list[UniFiFirewallZone],
) -> None:
    """Print a human-readable summary of zone-based firewall policies."""
    # Group by source→destination zone pair
    by_pair: dict[tuple[str, str], list[UniFiFirewallPolicy]] = {}
    for p in policies:
        key = (p.src_zone_name, p.dst_zone_name)
        by_pair.setdefault(key, []).append(p)

    print(f"\n{'─'*70}")
    print(f"  UniFi Zone-Based Firewall Policies  ({len(policies)} total)")
    print(f"{'─'*70}")

    for (src, dst), ps in sorted(by_pair.items()):
        print(f"\n  {src}  →  {dst}")
        for p in sorted(ps, key=lambda x: x.index):
            status = "✓" if p.enabled else "✗"
            log = " [log]" if p.logging else ""
            ret = " [+ret]" if p.allow_return_traffic else ""
            origin = f" [{p.origin}]" if p.origin and p.origin != "USER_DEFINED" else ""
            print(f"    {status} {p.index:>6}  {p.action:<6}  {p.name}{log}{ret}{origin}")

    print(f"\n  Zones ({len(zones)})")
    for z in sorted(zones, key=lambda x: x.name):
        net_count = len(z.network_ids)
        print(f"    {z.name:<30} {net_count} network(s)")

    print()
