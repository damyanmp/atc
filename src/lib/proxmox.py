"""Proxmox API wrapper using proxmoxer.

Connects to a single cluster node; the cluster resources API returns all nodes
and guests automatically — no need to query each node separately.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

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
    load_avg: float = -1.0    # 1-min load average; -1 = not fetched
    io_wait: float = -1.0     # fraction of CPU time spent waiting on I/O; -1 = not fetched
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
    disk_used: int = 0        # bytes (LXC rootfs; unreliable for qemu)
    disk_total: int = 0       # bytes (configured disk size)
    disk_read: int = 0        # bytes read since boot (cumulative)
    disk_write: int = 0       # bytes written since boot (cumulative)
    net_in: int = 0           # bytes received since boot (cumulative)
    net_out: int = 0          # bytes sent since boot (cumulative)
    uptime: int = 0
    tags: str = ""


@dataclass
class ClusterStatus:
    nodes: list[NodeStatus] = field(default_factory=list)
    guests: list[GuestInfo] = field(default_factory=list)


@dataclass
class GuestDetail:
    vmid: int
    name: str
    node: str
    type: str           # "qemu" or "lxc"
    status: str
    cpu_usage: float    # 0.0–1.0 current utilisation
    cpu_cores: int      # allocated vCPUs
    mem_used: int       # bytes
    mem_total: int      # bytes
    disk_used: int      # bytes
    disk_total: int     # bytes
    ips: list[str]
    uptime: int         # seconds
    tags: str
    ostype: str = ""
    error: str = ""


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
                disk_used=int(r.get("disk", 0)),
                disk_total=int(r.get("maxdisk", 0)),
                disk_read=int(r.get("diskread", 0)),
                disk_write=int(r.get("diskwrite", 0)),
                net_in=int(r.get("netin", 0)),
                net_out=int(r.get("netout", 0)),
                uptime=int(r.get("uptime", 0)),
                tags=r.get("tags", ""),
            ))

    nodes.sort(key=lambda n: n.name)
    return ClusterStatus(nodes=nodes, guests=guests)


async def fetch_cluster_status(cfg: ProxmoxClusterConfig) -> ClusterStatus:
    """Async wrapper — runs the blocking proxmoxer call in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_cluster, cfg)


async def fetch_node_loads(
    cfg: ProxmoxClusterConfig,
    node_names: list[str],
) -> dict[str, tuple[float, float, int]]:
    """Fetch per-node stats in parallel from /nodes/{n}/status.

    Returns {node_name: (load_avg_1m, io_wait_fraction, nproc)}.
    load_avg_1m: 1-minute load average (-1 if unavailable).
    io_wait_fraction: fraction of CPU time spent waiting on I/O (-1 if unavailable).
    nproc: logical CPU count (0 if unavailable).
    """
    loop = asyncio.get_event_loop()

    def _get(name: str) -> tuple[str, float, float, int]:
        try:
            px = _connect(cfg)
            s = px.nodes(name).status.get()
            la = s.get("loadavg", [])
            load = float(la[0]) if la else -1.0
            wait = float(s.get("wait", -1.0))
            nproc = int(s.get("cpuinfo", {}).get("cpus", 0))
            return name, load, wait, nproc
        except Exception:
            return name, -1.0, -1.0, 0

    results = await asyncio.gather(*[
        loop.run_in_executor(None, _get, n) for n in node_names
    ])
    return {name: (load, wait, nproc) for name, load, wait, nproc in results}


async def fetch_guest_ips(cfg: ProxmoxClusterConfig, guests: list[GuestInfo]) -> dict[int, str]:
    """Fetch IPs for all running guests in parallel. Returns {vmid: ip}."""
    running = [g for g in guests if g.status == "running"]
    loop = asyncio.get_event_loop()

    async def _get(g: GuestInfo) -> tuple[int, str]:
        ip = await loop.run_in_executor(None, get_node_ip, cfg, g.node, g.vmid, g.type)
        return g.vmid, ip or ""

    results = await asyncio.gather(*[_get(g) for g in running])
    return {vmid: ip for vmid, ip in results if ip}


def get_node_ip(cfg: ProxmoxClusterConfig, node: str, vmid: int, guest_type: str) -> str | None:
    """Try to get the IP of a running guest via QEMU guest agent or LXC network interfaces."""
    try:
        px = _connect(cfg)
        if guest_type == "qemu":
            ifaces = px.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
            for iface in ifaces.get("result", []):
                for addr in iface.get("ip-addresses", []):
                    ip = addr.get("ip-address", "")
                    if addr.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                        return ip
        elif guest_type == "lxc":
            for iface in px.nodes(node).lxc(vmid).interfaces.get():
                inet = iface.get("inet", "")
                if inet and not inet.startswith("127."):
                    return inet.split("/")[0]
    except Exception:
        pass
    return None


def fetch_guest_detail(
    cfg: ProxmoxClusterConfig, node: str, vmid: int, guest_type: str
) -> GuestDetail:
    """Fetch detailed runtime stats and config for a single VM or LXC."""
    try:
        px = _connect(cfg)
        if guest_type == "lxc":
            status = px.nodes(node).lxc(vmid).status.current.get()
            config = px.nodes(node).lxc(vmid).config.get()
            ips: list[str] = []
            try:
                for iface in px.nodes(node).lxc(vmid).interfaces.get():
                    inet = iface.get("inet", "")
                    if inet and not inet.startswith("127."):
                        ips.append(inet.split("/")[0])
            except Exception:
                pass
        else:  # qemu
            status = px.nodes(node).qemu(vmid).status.current.get()
            config = px.nodes(node).qemu(vmid).config.get()
            ips = []
            try:
                result = px.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
                for iface in result.get("result", []):
                    for addr in iface.get("ip-addresses", []):
                        ip = addr.get("ip-address", "")
                        if addr.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                            ips.append(ip)
            except Exception:
                pass

        cpu_cores = int(config.get("cores", config.get("cpus", 1)))
        name = status.get("name", config.get("hostname", f"{guest_type}-{vmid}"))
        tags = status.get("tags", config.get("tags", ""))

        return GuestDetail(
            vmid=vmid,
            name=name,
            node=node,
            type=guest_type,
            status=status.get("status", "unknown"),
            cpu_usage=float(status.get("cpu", 0)),
            cpu_cores=cpu_cores,
            mem_used=int(status.get("mem", 0)),
            mem_total=int(status.get("maxmem", 0)),
            disk_used=int(status.get("disk", 0)),
            disk_total=int(status.get("maxdisk", 0)),
            ips=ips,
            uptime=int(status.get("uptime", 0)),
            tags=tags,
            ostype=config.get("ostype", ""),
        )
    except Exception as e:
        return GuestDetail(
            vmid=vmid,
            name=f"{guest_type}-{vmid}",
            node=node,
            type=guest_type,
            status="unknown",
            cpu_usage=0.0,
            cpu_cores=0,
            mem_used=0,
            mem_total=0,
            disk_used=0,
            disk_total=0,
            ips=[],
            uptime=0,
            tags="",
            error=str(e),
        )


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


# ── Storage dataclasses ─────────────────────────────────────────────────────────

@dataclass
class StorageStatus:
    node: str
    storage: str
    type: str        # "dir", "lvmthin", "zfspool", "nfs", etc.
    used: int         # bytes
    total: int        # bytes
    avail: int        # bytes
    shared: bool
    enabled: bool
    active: bool


def fetch_storage_status(cfg: ProxmoxClusterConfig, online_nodes: list[str]) -> list[StorageStatus]:
    """List storage pool capacity/usage across the cluster.

    This is distinct from NodeStatus.disk_used/disk_total, which only reflects
    the node's root filesystem — not the datastore(s) backing VM/LXC disks.
    Deduplicates shared storages (they'd otherwise appear once per node).
    """
    px = _connect(cfg)
    results: list[StorageStatus] = []
    seen: set[str] = set()  # (shared-storage-name | node/local-storage-name)

    for node in online_nodes:
        try:
            storages = px.nodes(node).storage.get()
        except Exception:
            continue
        for s in storages:
            storage_name = s.get("storage", "unknown")
            shared = bool(s.get("shared", 0))
            key = storage_name if shared else f"{node}/{storage_name}"
            if key in seen:
                continue
            seen.add(key)
            results.append(StorageStatus(
                node=node,
                storage=storage_name,
                type=s.get("type", ""),
                used=int(s.get("used", 0)),
                total=int(s.get("total", 0)),
                avail=int(s.get("avail", 0)),
                shared=shared,
                enabled=bool(s.get("enabled", 1)),
                active=bool(s.get("active", 0)),
            ))

    results.sort(key=lambda s: (s.node, s.storage))
    return results


async def fetch_storage_status_async(cfg: ProxmoxClusterConfig, online_nodes: list[str]) -> list[StorageStatus]:
    """Async wrapper — runs the blocking proxmoxer calls in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_storage_status, cfg, online_nodes)


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


# ── SSH key injection ──────────────────────────────────────────────────────────

def ssh_key_inject_script(pubkey: str, user: str = "root") -> str:
    """Build an idempotent shell script that appends pubkey to a user's authorized_keys."""
    key = pubkey.strip()
    home = "/root" if user == "root" else f"/home/{user}"
    ssh_dir = f"{home}/.ssh"
    auth_keys = f"{ssh_dir}/authorized_keys"
    return (
        f"mkdir -p {ssh_dir} && "
        f"chmod 700 {ssh_dir} && "
        f"grep -qxF '{key}' {auth_keys} 2>/dev/null "
        f"|| echo '{key}' >> {auth_keys} && "
        f"chmod 600 {auth_keys}"
    )


def push_ssh_key_qemu(
    cfg: ProxmoxClusterConfig,
    node: str,
    vmid: int,
    pubkey: str,
    user: str = "root",
) -> tuple[bool, str]:
    """Inject an SSH public key into a QEMU VM via the guest agent exec API.

    Requires qemu-guest-agent running inside the VM.
    Returns (success, message).
    """
    script = ssh_key_inject_script(pubkey, user)
    try:
        px = _connect(cfg)
        result = px.nodes(node).qemu(vmid).agent("exec").post(**{"command": ["bash", "-c", script]})
        pid = result.get("pid")
        if pid is None:
            return False, "guest agent exec returned no pid"
        for _ in range(60):
            st = px.nodes(node).qemu(vmid).agent("exec-status").get(pid=pid)
            if st.get("exited"):
                ec = int(st.get("exitcode", 1))
                err = st.get("err-data", "")
                return ec == 0, (err.strip() or f"exit: {ec}")
            time.sleep(0.5)
        return False, "timeout waiting for guest agent exec"
    except Exception as e:
        return False, str(e)
