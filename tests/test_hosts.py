"""Tests for lib/hosts.py — Proxmox/Beszel host merge logic."""
from __future__ import annotations

from lib.beszel import BeszelSystem
from lib.hosts import merge_hosts, normalize_name
from lib.proxmox import ClusterStatus, GuestInfo


def _guest(vmid, name, gtype="qemu", status="running", **kw) -> GuestInfo:
    return GuestInfo(vmid=vmid, name=name, node="pve1", type=gtype, status=status, **kw)


def _bz(name, host, status="up", **kw) -> BeszelSystem:
    return BeszelSystem(name=name, host=host, status=status, **kw)


class TestNormalizeName:
    def test_lowercases_and_dashes(self):
        assert normalize_name("Docker-1 (local)") == "docker-1-local"

    def test_strips_leading_trailing_dashes(self):
        assert normalize_name("  Plex LXC  ") == "plex-lxc"


class TestMergeHostsByName:
    def test_exact_name_match_merges(self):
        cluster = ClusterStatus(guests=[_guest(110, "docker-2")])
        bz = [_bz("Docker-2", "192.168.1.122", cpu_percent=5.0)]
        hosts = merge_hosts(cluster, bz)
        assert len(hosts) == 1
        assert hosts[0].source == "merged"
        assert hosts[0].cpu_pct == 5.0

    def test_no_match_stays_proxmox_only_and_beszel_appended_standalone(self):
        cluster = ClusterStatus(guests=[_guest(105, "totally-different-name")])
        bz = [_bz("Unrelated System", "10.0.0.5")]
        hosts = merge_hosts(cluster, bz)
        # two separate rows — no relationship between them
        assert len(hosts) == 2
        sources = {h.source for h in hosts}
        assert sources == {"proxmox", "beszel"}


class TestMergeHostsByIpFallback:
    """Beszel agent hostnames frequently don't match the Proxmox guest name
    (e.g. 'Docker-1 (local)' vs 'docker-1', 'Plex LXC' vs 'plex', 'Hermes'
    vs 'hermes-host') even though both report the same IP. Without an IP
    fallback this used to produce a duplicate row per guest — a real
    Proxmox-sourced row plus a fake standalone Beszel 'host' row for the
    same machine.
    """

    def test_ip_match_merges_when_names_differ(self):
        cluster = ClusterStatus(guests=[_guest(107, "docker-1")])
        ip_map = {107: "192.168.1.78"}
        bz = [_bz("Docker-1 (local)", "192.168.1.78", cpu_percent=8.0)]
        hosts = merge_hosts(cluster, bz, ip_map)
        assert len(hosts) == 1, "should merge into a single row, not duplicate"
        assert hosts[0].source == "merged"
        assert hosts[0].name == "docker-1"
        assert hosts[0].cpu_pct == 8.0

    def test_plex_lxc_vs_plex(self):
        cluster = ClusterStatus(guests=[_guest(105, "plex", gtype="lxc")])
        ip_map = {105: "192.168.1.192"}
        bz = [_bz("Plex LXC", "192.168.1.192", mem_percent=8.0)]
        hosts = merge_hosts(cluster, bz, ip_map)
        assert len(hosts) == 1
        assert hosts[0].source == "merged"

    def test_hermes_vs_hermes_host(self):
        cluster = ClusterStatus(guests=[_guest(200, "hermes-host")])
        ip_map = {200: "192.168.1.132"}
        bz = [_bz("Hermes", "192.168.1.132", mem_percent=50.0)]
        hosts = merge_hosts(cluster, bz, ip_map)
        assert len(hosts) == 1
        assert hosts[0].source == "merged"

    def test_no_ip_map_entry_falls_back_to_standalone(self):
        cluster = ClusterStatus(guests=[_guest(107, "docker-1")])
        bz = [_bz("Docker-1 (local)", "192.168.1.78")]
        hosts = merge_hosts(cluster, bz, ip_map=None)
        # can't resolve via name or IP (no ip_map) -> stays as two rows
        assert len(hosts) == 2

    def test_standalone_host_with_no_proxmox_guest_at_all(self):
        cluster = ClusterStatus(guests=[_guest(107, "docker-1")])
        ip_map = {107: "192.168.1.78"}
        bz = [
            _bz("Docker-1 (local)", "192.168.1.78"),
            _bz("Globemaster", "192.168.1.151", cpu_percent=20.0),
        ]
        hosts = merge_hosts(cluster, bz, ip_map)
        assert len(hosts) == 2
        standalone = [h for h in hosts if h.type == "host"]
        assert len(standalone) == 1
        assert standalone[0].name == "Globemaster"
