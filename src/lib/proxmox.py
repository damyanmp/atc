"""Proxmox API wrapper using proxmoxer.

Connects to a single cluster node; the cluster resources API returns all nodes
and guests automatically — no need to query each node separately.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from proxmoxer import ProxmoxAPI

from config import ProxmoxClusterConfig


@dataclass
class NodeStatus:
    name: str
    online: bool
    cpu: float = 0.0          # 0.0–1.0
    mem_used: int = 0         # bytes
    mem_total: int = 0        # bytes
    disk_used: int = 0        # bytes
    disk_total: int = 0       # bytes
    uptime: int = 0           # seconds
    error: str = ""


@dataclass
class GuestInfo:
    vmid: int
    name: str
    node: str
    type: str                 # "qemu" or "lxc"
    status: str               # "running", "stopped", etc.
    cpu: float = 0.0
    mem: int = 0              # bytes
    maxmem: int = 0           # bytes
    uptime: int = 0
    tags: str = ""


@dataclass
class ClusterStatus:
    nodes: list[NodeStatus] = field(default_factory=list)
    guests: list[GuestInfo] = field(default_factory=list)


def _connect(cfg: ProxmoxClusterConfig) -> ProxmoxAPI:
    return ProxmoxAPI(
        cfg.host,
        port=cfg.port,
        user=cfg.user,
        token_name=cfg.token_name,
        token_value=cfg.token_value,
        verify_ssl=cfg.verify_ssl,
        timeout=10,
    )


def _fetch_cluster(cfg: ProxmoxClusterConfig) -> ClusterStatus:
    """Fetch all nodes and guests via a single cluster resources call."""
    try:
        px = _connect(cfg)
        resources = px.cluster.resources.get()
    except Exception as e:
        return ClusterStatus(
            nodes=[NodeStatus(name="cluster", online=False, error=str(e))]
        )

    nodes: list[NodeStatus] = []
    guests: list[GuestInfo] = []

    for r in resources:
        rtype = r.get("type")
        if rtype == "node":
            nodes.append(NodeStatus(
                name=r.get("node", "unknown"),
                online=r.get("status") == "online",
                cpu=float(r.get("cpu", 0)),
                mem_used=int(r.get("mem", 0)),
                mem_total=int(r.get("maxmem", 1)),
                disk_used=int(r.get("disk", 0)),
                disk_total=int(r.get("maxdisk", 1)),
                uptime=int(r.get("uptime", 0)),
            ))
        elif rtype in ("qemu", "lxc"):
            guests.append(GuestInfo(
                vmid=int(r["vmid"]),
                name=r.get("name", f"{rtype}-{r['vmid']}"),
                node=r.get("node", "unknown"),
                type=rtype,
                status=r.get("status", "unknown"),
                cpu=float(r.get("cpu", 0)),
                mem=int(r.get("mem", 0)),
                maxmem=int(r.get("maxmem", 0)),
                uptime=int(r.get("uptime", 0)),
                tags=r.get("tags", ""),
            ))

    nodes.sort(key=lambda n: n.name)
    return ClusterStatus(nodes=nodes, guests=guests)


async def fetch_cluster_status(cfg: ProxmoxClusterConfig) -> ClusterStatus:
    """Async wrapper — runs the blocking proxmoxer call in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_cluster, cfg)


def get_node_ip(cfg: ProxmoxClusterConfig, node: str, vmid: int, guest_type: str) -> str | None:
    """Try to get the IP of a running guest via QEMU guest agent or LXC network."""
    try:
        px = _connect(cfg)
        if guest_type == "qemu":
            ifaces = px.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
            for iface in ifaces.get("result", []):
                for addr in iface.get("ip-addresses", []):
                    ip = addr.get("ip-address", "")
                    if addr.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                        return ip
    except Exception:
        pass
    return None


def start_guest(cfg: ProxmoxClusterConfig, node: str, vmid: int, guest_type: str) -> bool:
    try:
        px = _connect(cfg)
        if guest_type == "qemu":
            px.nodes(node).qemu(vmid).status.start.post()
        else:
            px.nodes(node).lxc(vmid).status.start.post()
        return True
    except Exception:
        return False


def stop_guest(cfg: ProxmoxClusterConfig, node: str, vmid: int, guest_type: str) -> bool:
    try:
        px = _connect(cfg)
        if guest_type == "qemu":
            px.nodes(node).qemu(vmid).status.shutdown.post()
        else:
            px.nodes(node).lxc(vmid).status.shutdown.post()
        return True
    except Exception:
        return False


def restart_guest(cfg: ProxmoxClusterConfig, node: str, vmid: int, guest_type: str) -> bool:
    try:
        px = _connect(cfg)
        if guest_type == "qemu":
            px.nodes(node).qemu(vmid).status.reboot.post()
        else:
            px.nodes(node).lxc(vmid).status.reboot.post()
        return True
    except Exception:
        return False


# ── Backup dataclasses ────────────────────────────────────────────────────────

@dataclass
class BackupFile:
    node: str
    storage: str
    volid: str
    vmid: int
    size: int       # bytes
    ctime: int      # unix timestamp
    fmt: str        # e.g. "vma.zst"
    notes: str = ""


@dataclass
class BackupJob:
    id: str
    enabled: bool
    schedule: str   # cron-style or DOW+time (PVE format)
    storage: str
    vmids: str      # "all" or comma-separated VMIDs
    comment: str = ""
    node: str = ""  # "" = all nodes


# ── Backup functions ──────────────────────────────────────────────────────────

def list_backups(cfg: ProxmoxClusterConfig, online_nodes: list[str]) -> list[BackupFile]:
    """List all backup files across the cluster, deduplicating shared storages."""
    px = _connect(cfg)
    results: list[BackupFile] = []
    seen: set[str] = set()  # tracks (shared-storage-name | node/local-storage-name)

    for node in online_nodes:
        try:
            storages = px.nodes(node).storage.get()
        except Exception:
            continue
        for s in storages:
            if "backup" not in s.get("content", ""):
                continue
            storage_name = s["storage"]
            # Shared storages appear on every node — only query once
            shared = bool(s.get("shared", 0))
            key = storage_name if shared else f"{node}/{storage_name}"
            if key in seen:
                continue
            seen.add(key)
            try:
                items = px.nodes(node).storage(storage_name).content.get(content="backup")
                for item in items:
                    results.append(BackupFile(
                        node=node,
                        storage=storage_name,
                        volid=item.get("volid", ""),
                        vmid=int(item.get("vmid", 0)),
                        size=int(item.get("size", 0)),
                        ctime=int(item.get("ctime", 0)),
                        fmt=item.get("format", ""),
                        notes=item.get("notes", ""),
                    ))
            except Exception:
                pass

    results.sort(key=lambda b: (b.vmid, -b.ctime))
    return results


def list_backup_jobs(cfg: ProxmoxClusterConfig) -> list[BackupJob]:
    """List scheduled backup jobs from /cluster/backup."""
    try:
        px = _connect(cfg)
        return [
            BackupJob(
                id=j.get("id", ""),
                enabled=bool(int(j.get("enabled", 1))),
                schedule=j.get("schedule", j.get("starttime", "")),
                storage=j.get("storage", ""),
                vmids=str(j.get("vmid", "all")),
                comment=j.get("comment", ""),
                node=j.get("node", ""),
            )
            for j in px.cluster.backup.get()
        ]
    except Exception:
        return []


def run_backup_job(cfg: ProxmoxClusterConfig, job_id: str) -> bool:
    """Trigger an immediate run of a scheduled backup job (requires PVE 7+)."""
    try:
        px = _connect(cfg)
        px.cluster.backup(job_id).run.post()
        return True
    except Exception:
        return False


def backup_guest(
    cfg: ProxmoxClusterConfig,
    node: str,
    vmid: int,
    storage: str,
    mode: str = "snapshot",
) -> str | None:
    """Trigger a vzdump backup. Returns the task UPID, or None on failure."""
    try:
        px = _connect(cfg)
        return px.nodes(node).vzdump.post(
            vmid=vmid,
            storage=storage,
            mode=mode,
            compress="zstd",
        )
    except Exception:
        return None


def find_backup_storage(cfg: ProxmoxClusterConfig, node: str) -> str | None:
    """Return the first backup-capable storage visible on the given node."""
    try:
        px = _connect(cfg)
        for s in px.nodes(node).storage.get():
            if "backup" in s.get("content", ""):
                return s["storage"]
    except Exception:
        pass
    return None
