"""Normalized host model — merges Proxmox guests with Beszel system metrics.

Matching strategy: normalize names to lowercase alphanumeric-and-dash, then
do an exact lookup. Proxmox is authoritative for status, type, node, and IP
(via guest agent). Beszel is preferred for cpu/mem/disk (guest OS view).
Beszel systems with no matching Proxmox guest are appended as standalone hosts
(type="host"), e.g. a NAS or bare-metal Docker host.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from beszel import BeszelSystem
from proxmox import ClusterStatus


def normalize_name(name: str) -> str:
    """Lowercase, collapse non-alphanumerics to a single dash."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")


@dataclass
class NormalizedHost:
    name: str
    status: str           # "running" / "stopped" / "up" / "down" / …
    type: str             # "vm", "lxc", "host" (standalone Beszel-only)
    node: str             # Proxmox node name; "" for standalone
    vmid: int             # Proxmox VMID; 0 for standalone
    ip: str               # primary IP; "" if unknown
    cpu_pct: float        # 0–100
    mem_pct: float        # 0–100
    disk_pct: float       # 0–100; -1.0 = not available
    disk_read: int        # bytes read since boot (cumulative, Proxmox); 0 = not available
    disk_write: int       # bytes written since boot (cumulative, Proxmox); 0 = not available
    net_in: int           # bytes received since boot (cumulative, Proxmox); 0 = not available
    net_out: int          # bytes sent since boot (cumulative, Proxmox); 0 = not available
    uptime: int           # seconds; 0 = not available
    tags: str             # Proxmox tags; "" for standalone
    source: str           # "proxmox", "beszel", "merged"
    # Beszel rate metrics (bytes/s, live from agent; 0.0 = not available)
    bz_net_recv_bps: float = 0.0
    bz_net_sent_bps: float = 0.0
    bz_disk_read_bps: float = 0.0
    bz_disk_write_bps: float = 0.0
    # Beszel memory absolute (MiB; 0 = not available)
    bz_mem_used_mib: float = 0.0
    bz_mem_total_mib: float = 0.0

    @property
    def online(self) -> bool:
        return self.status in ("running", "up")

    @property
    def disk_available(self) -> bool:
        return self.disk_pct >= 0.0


def merge_hosts(
    cluster: ClusterStatus,
    beszel_systems: list[BeszelSystem],
    ip_map: dict[int, str] | None = None,
) -> list[NormalizedHost]:
    """Merge Proxmox guests with Beszel systems into a unified host list.

    For each Proxmox guest, attempt to find a matching Beszel system by
    normalized name, falling back to IP match if the names don't line up
    (Beszel agents often report a different hostname than the Proxmox guest
    name, e.g. "Docker-1 (local)" / "Plex LXC" / "Hermes" vs. the Proxmox
    names "docker-1" / "plex" / "hermes-host" — but both report the same
    IP). If found and online, Beszel metrics (cpu/mem/disk) replace the
    Proxmox metrics. Unmatched Beszel systems are appended as standalone
    hosts (type="host").
    """
    ip_map = ip_map or {}
    beszel_index: dict[str, BeszelSystem] = {
        normalize_name(s.name): s for s in beszel_systems
    }
    beszel_by_ip: dict[str, str] = {
        s.host: normalize_name(s.name) for s in beszel_systems if s.host
    }
    unmatched: set[str] = set(beszel_index)

    hosts: list[NormalizedHost] = []

    for g in cluster.guests:
        key = normalize_name(g.name)
        bz = beszel_index.get(key)
        if bz is None:
            guest_ip = ip_map.get(g.vmid, "")
            if guest_ip and guest_ip in beszel_by_ip:
                key = beszel_by_ip[guest_ip]
                bz = beszel_index.get(key)
        if bz is not None:
            unmatched.discard(key)

        # Prefer Proxmox IP (guest agent), fall back to Beszel host field
        ip = ip_map.get(g.vmid, "") or (bz.host if bz else "")

        if bz and bz.online:
            cpu_pct = bz.cpu_percent
            mem_pct = bz.mem_percent
            disk_pct = bz.disk_percent
            source = "merged"
        else:
            cpu_pct = g.cpu * 100 if g.status == "running" else 0.0
            mem_pct = (
                g.mem / g.maxmem * 100
                if g.maxmem > 0 and g.status == "running"
                else 0.0
            )
            # LXC rootfs disk usage is reliable from Proxmox; qemu disk is not
            if g.type == "lxc" and g.disk_total > 0 and g.status == "running":
                disk_pct = g.disk_used / g.disk_total * 100
            else:
                disk_pct = -1.0
            source = "proxmox"

        hosts.append(NormalizedHost(
            name=g.name,
            status=g.status,
            type="vm" if g.type == "qemu" else "lxc",
            node=g.node,
            vmid=g.vmid,
            ip=ip,
            cpu_pct=cpu_pct,
            mem_pct=mem_pct,
            disk_pct=disk_pct,
            disk_read=g.disk_read,
            disk_write=g.disk_write,
            net_in=g.net_in,
            net_out=g.net_out,
            bz_net_recv_bps=bz.net_recv_bps if bz else 0.0,
            bz_net_sent_bps=bz.net_sent_bps if bz else 0.0,
            bz_disk_read_bps=bz.disk_read_bps if bz else 0.0,
            bz_disk_write_bps=bz.disk_write_bps if bz else 0.0,
            bz_mem_used_mib=bz.mem_used_mib if bz else 0.0,
            bz_mem_total_mib=bz.mem_total_mib if bz else 0.0,
            uptime=g.uptime,
            tags=g.tags,
            source=source,
        ))

    # Unmatched Beszel entries → standalone hosts (e.g. NAS, bare-metal Docker)
    for key in sorted(unmatched):
        bz = beszel_index[key]
        hosts.append(NormalizedHost(
            name=bz.name,
            status=bz.status,
            type="host",
            node="",
            vmid=0,
            ip=bz.host,
            cpu_pct=bz.cpu_percent if bz.online else 0.0,
            mem_pct=bz.mem_percent if bz.online else 0.0,
            disk_pct=bz.disk_percent if bz.online else -1.0,
            disk_read=0,
            disk_write=0,
            net_in=0,
            net_out=0,
            bz_net_recv_bps=bz.net_recv_bps if bz.online else 0.0,
            bz_net_sent_bps=bz.net_sent_bps if bz.online else 0.0,
            bz_disk_read_bps=bz.disk_read_bps if bz.online else 0.0,
            bz_disk_write_bps=bz.disk_write_bps if bz.online else 0.0,
            bz_mem_used_mib=bz.mem_used_mib if bz.online else 0.0,
            bz_mem_total_mib=bz.mem_total_mib if bz.online else 0.0,
            uptime=bz.uptime_seconds,
            tags="",
            source="beszel",
        ))

    # Running/up first, standalone hosts last, then alpha by name
    hosts.sort(key=lambda h: (not h.online, h.type == "host", h.name))
    return hosts
