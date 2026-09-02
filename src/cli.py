"""atc CLI — non-interactive subcommands via Typer + Rich."""
from __future__ import annotations

import asyncio
import difflib
import getpass
import json as _json
import subprocess
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import click
import typer
from rich.console import Console
from rich.table import Table
from rich import box
from rich.prompt import Confirm, Prompt
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn
from rich.text import Text
from rich.markup import escape as rich_escape

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "lib"))

from config import (
    Config, load_config,
    secret_set, secret_delete, secret_verify, secret_repair, secrets_scan_config, secrets_list,
    save_guest_user, save_guest_update_cmd, remove_guest_update_cmd,
    KEYCHAIN_SERVICE,
)
from lib.pihole import (
    fetch_all_status as pihole_fetch_all,
    set_blocking_all,
    list_custom_dns,
    add_custom_dns,
    remove_custom_dns,
    trigger_gravity_all,
    fetch_seen_ips_all,
)
from lib.unifi import fetch_clients as unifi_fetch_clients
from lib.influxdb import write_points as influx_write_points
from lib.nginx_proxy_manager import (
    list_proxy_hosts as npm_list_proxy_hosts,
    find_proxy_host as npm_find_proxy_host,
    create_proxy_host as npm_create_proxy_host,
    update_proxy_host as npm_update_proxy_host,
    delete_proxy_host as npm_delete_proxy_host,
    enable_proxy_host as npm_enable_proxy_host,
    disable_proxy_host as npm_disable_proxy_host,
    list_certificates as npm_list_certificates,
)
from lib.ha import (
    get_status as ha_get_status,
    get_entity_state,
    call_service as ha_call_service,
    get_stats as ha_get_stats,
    list_updates as ha_list_updates,
)
from lib.proxmox import (
    fetch_cluster_status, fetch_guest_ips, fetch_node_loads,
    ClusterStatus, GuestInfo,
    fetch_guest_detail, GuestDetail,
    get_node_ip,
    start_guest, stop_guest, restart_guest,
    list_backups, BackupFile,
    list_backup_jobs, BackupJob,
    run_backup_job, backup_guest, find_backup_storage,
    push_ssh_key_qemu, ssh_key_inject_script,
    fetch_storage_status_async, StorageStatus,
)
from lib.beszel import fetch_systems as fetch_beszel, BeszelSystem
from lib.hosts import NormalizedHost, merge_hosts
from lib.tls import check_all_certs, CertStatus
from lib.ssh import UpdateTarget, run_update, run_command, stream_command
from lib.prometheus import query as prom_query, query_range as prom_query_range, list_metric_names as prom_list_names
from screens.scripts import _find_scripts, _get_script_description

console = Console()

# ── Feature flags ─────────────────────────────────────────────────────────────
# Set to True to allow Radarr/Sonarr search commands (missing, search-today,
# and the interactive pipeline search action) to actually fire indexer searches.
# Disabled by default to prevent accidental indexer hammering.
ENABLE_ARR_SEARCH: bool = False

# ── Output format enum ────────────────────────────────────────────────────────

class _Out(str, Enum):
    table = "table"   # compact, no borders (default)
    wide  = "wide"    # full columns with metrics
    json  = "json"    # raw JSON for scripting


# ── AliasedGroup (ported from pepper) ────────────────────────────────────────

class AliasedGroup(click.Group):
    """Click group that supports per-command aliases shown inline in help."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aliases: dict[str, str] = {}   # alias -> canonical

    def add_alias(self, alias: str, canonical: str) -> None:
        self._aliases[alias] = canonical

    def get_command(self, ctx, cmd_name):
        rv = super().get_command(ctx, cmd_name)
        if rv is not None:
            return rv
        canonical = self._aliases.get(cmd_name)
        if canonical:
            return super().get_command(ctx, canonical)
        return None

    def list_commands(self, ctx):
        return [c for c in super().list_commands(ctx) if c not in self._aliases]

    def format_commands(self, ctx, formatter):
        alias_for = {v: k for k, v in self._aliases.items()}
        commands = []
        for name in self.list_commands(ctx):
            cmd = self.commands.get(name)
            if cmd is None or cmd.hidden:
                continue
            alias = alias_for.get(name)
            display = f"{name} ({alias})" if alias else name
            commands.append((display, cmd.get_short_help_str(limit=formatter.width)))
        if commands:
            with formatter.section("Commands"):
                formatter.write_dl(commands)


app = typer.Typer(
    name="atc",
    help="Air Traffic Control — homelab management CLI",
    no_args_is_help=True,
    rich_markup_mode="rich",
    add_completion=False,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load(cfg_path: Optional[Path] = None) -> Config:
    try:
        return load_config(cfg_path)
    except FileNotFoundError as e:
        console.print(f"[red]❌  {e}[/]")
        raise typer.Exit(1)


def _fmt_bytes(b: int) -> str:
    if b >= 1024 ** 3:
        return f"{b / 1024**3:.1f}G"
    if b >= 1024 ** 2:
        return f"{b / 1024**2:.0f}M"
    return f"{b / 1024:.0f}K"


def _fmt_pct(val: float, warn: float = 0.75, crit: float = 0.90) -> str:
    pct = val * 100
    color = "red" if val >= crit else ("yellow" if val >= warn else "green")
    return f"[{color}]{pct:.0f}%[/]"


def _fmt_uptime(s: int) -> str:
    if s <= 0:
        return "—"
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def _pct_color(val: float, warn: float = 0.75, crit: float = 0.90) -> str:
    return "red" if val >= crit else ("yellow" if val >= warn else "green")


def _bar_pct(val: float, width: int = 24) -> str:
    filled = round(min(val, 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def _guest_tags(guest) -> list[str]:
    raw = guest.tags or ""
    return [t.strip().lower() for t in raw.replace(",", ";").split(";") if t.strip()]


def _shell_quote(s: str) -> str:
    """Wrap a string in single quotes, escaping any embedded single quotes."""
    return "'" + s.replace("'", "'\\''") + "'"


def _resolve_guests(
    cluster: ClusterStatus,
    selector: str,
    label: Optional[str] = None,
    require_status: Optional[str] = None,
) -> list[GuestInfo]:
    """Resolve guests matching selector (name substring, tag, VMID, or '*').

    `require_status`: if set, pre-filters by 'running' / 'stopped' / etc.
    `label`: applied on top of the selector as an additional tag filter.
    """
    pool = cluster.guests
    if require_status:
        pool = [g for g in pool if g.status == require_status]

    if selector == "*":
        matched = list(pool)
    elif selector.isdigit():
        vmid = int(selector)
        matched = [g for g in pool if g.vmid == vmid]
    else:
        sel = selector.lower()
        by_tag  = [g for g in pool if sel in _guest_tags(g)]
        by_name = [g for g in pool if sel in g.name.lower()]
        seen: set[int] = set()
        matched = []
        for g in by_tag + by_name:
            if g.vmid not in seen:
                seen.add(g.vmid)
                matched.append(g)

    if label:
        lf = label.lower()
        matched = [g for g in matched if lf in _guest_tags(g)]

    return matched


# ── JSON serializers ──────────────────────────────────────────────────────────

def _nodes_to_json(cluster: ClusterStatus) -> list[dict]:
    return [
        {
            "name": n.name,
            "online": n.online,
            "cpu": round(n.cpu, 4),
            "mem_used": n.mem_used,
            "mem_total": n.mem_total,
            "disk_used": n.disk_used,
            "disk_total": n.disk_total,
            "uptime": n.uptime,
        }
        for n in cluster.nodes
    ]


def _storage_to_json(storages: list[StorageStatus]) -> list[dict]:
    return [
        {
            "node": s.node,
            "storage": s.storage,
            "type": s.type,
            "used": s.used,
            "total": s.total,
            "avail": s.avail,
            "pct": round(s.used / s.total * 100, 2) if s.total else None,
            "shared": s.shared,
            "enabled": s.enabled,
            "active": s.active,
        }
        for s in storages
    ]


def _guests_to_json(
    cluster: ClusterStatus,
    guest_type: str | None = None,
    ip_map: dict[int, str] | None = None,
) -> list[dict]:
    guests = [
        g for g in cluster.guests
        if guest_type is None or g.type == guest_type
    ]
    return [
        {
            "vmid": g.vmid,
            "name": g.name,
            "type": g.type,
            "node": g.node,
            "status": g.status,
            "ip": (ip_map or {}).get(g.vmid, ""),
            "cpu": round(g.cpu, 4),
            "mem": g.mem,
            "maxmem": g.maxmem,
            "uptime": g.uptime,
            "tags": g.tags,
        }
        for g in guests
    ]



def _hosts_to_json(hosts: list[NormalizedHost]) -> list[dict]:
    return [
        {
            "name": h.name,
            "type": h.type,
            "node": h.node or None,
            "vmid": h.vmid or None,
            "status": h.status,
            "ip": h.ip or None,
            "cpu_pct": round(h.cpu_pct, 2),
            "mem_pct": round(h.mem_pct, 2),
            "disk_pct": round(h.disk_pct, 2) if h.disk_available else None,
            "uptime": h.uptime or None,
            "tags": h.tags or None,
            "source": h.source,
        }
        for h in hosts
    ]


# ── Table renderers (wide) ────────────────────────────────────────────────────

def _render_nodes_wide(cluster: ClusterStatus) -> None:
    guest_counts: dict[str, tuple[int, int]] = {}
    for g in cluster.guests:
        vm_c, lxc_c = guest_counts.get(g.node, (0, 0))
        guest_counts[g.node] = (vm_c + 1, lxc_c) if g.type == "qemu" else (vm_c, lxc_c + 1)

    t = Table(title="Proxmox Nodes", box=box.ROUNDED, show_lines=False)
    t.add_column("Node", style="bold")
    t.add_column("Status")
    t.add_column("CPU", justify="right")
    t.add_column("Memory", justify="right")
    t.add_column("Disk", justify="right")
    t.add_column("VMs", justify="right")
    t.add_column("LXCs", justify="right")
    t.add_column("Uptime")

    for node in cluster.nodes:
        if node.online:
            status = "[green]● online[/]"
            cpu = _fmt_pct(node.cpu)
            mem_pct = node.mem_used / node.mem_total if node.mem_total else 0
            mem = f"{_fmt_pct(mem_pct)} {_fmt_bytes(node.mem_used)}/{_fmt_bytes(node.mem_total)}"
            disk_pct = node.disk_used / node.disk_total if node.disk_total else 0
            disk = f"{_fmt_pct(disk_pct)} {_fmt_bytes(node.disk_used)}/{_fmt_bytes(node.disk_total)}"
            vms, lxcs = guest_counts.get(node.name, (0, 0))
            uptime = _fmt_uptime(node.uptime)
        else:
            status = "[red]● offline[/]"
            cpu = mem = disk = uptime = "—"
            vms = lxcs = 0
        t.add_row(node.name, status, cpu, mem, disk, str(vms), str(lxcs), uptime)

    console.print(t)


def _render_guests_wide(
    cluster: ClusterStatus,
    guest_type: str | None,
    ip_map: dict[int, str] | None = None,
) -> None:
    guests = sorted(
        [g for g in cluster.guests if guest_type is None or g.type == guest_type],
        key=lambda g: (g.status != "running", g.type, g.name),
    )
    if guest_type == "qemu":
        label, icon = "Virtual Machines", "🖥 "
    elif guest_type == "lxc":
        label, icon = "LXC Containers", "📦"
    else:
        label, icon = "Guests", "🖥 "

    t = Table(title=f"{icon} {label}", box=box.ROUNDED, show_lines=False)
    t.add_column("", width=2)
    t.add_column("Name", style="bold")
    t.add_column("Type", width=4)
    t.add_column("VMID", justify="right")
    t.add_column("Node")
    t.add_column("IP")
    t.add_column("Status")
    t.add_column("CPU", justify="right")
    t.add_column("Memory", justify="right")
    t.add_column("Uptime")
    t.add_column("Tags", style="dim")

    for g in guests:
        dot = "[green]●[/]" if g.status == "running" else "[dim]●[/]"
        kind = "lxc" if g.type == "lxc" else "vm "
        status = f"[green]{g.status}[/]" if g.status == "running" else f"[dim]{g.status}[/]"
        cpu = f"{g.cpu * 100:.0f}%" if g.status == "running" else "—"
        mem = (
            f"{_fmt_bytes(g.mem)}/{_fmt_bytes(g.maxmem)}"
            if g.status == "running" and g.maxmem > 0 else "—"
        )
        uptime = _fmt_uptime(g.uptime) if g.status == "running" else "—"
        tags = g.tags.replace(";", " ") if g.tags else "—"
        ip = (ip_map or {}).get(g.vmid, "[dim]—[/]") or "[dim]—[/]"
        t.add_row(dot, g.name, kind, str(g.vmid), g.node, ip, status, cpu, mem, uptime, tags)

    console.print(t)



def _render_hosts_wide(hosts: list[NormalizedHost]) -> None:
    t = Table(title="🖥  All Hosts", box=box.ROUNDED, show_lines=False)
    t.add_column("", width=2)
    t.add_column("Name", style="bold")
    t.add_column("Type", width=4)
    t.add_column("ID", justify="right", style="dim")
    t.add_column("Node")
    t.add_column("IP")
    t.add_column("Status")
    t.add_column("CPU", justify="right")
    t.add_column("Mem", justify="right")
    t.add_column("Disk", justify="right")
    t.add_column("Uptime")
    t.add_column("Tags", style="dim")

    for h in hosts:
        dot = "[green]●[/]" if h.online else "[dim]●[/]"
        kind = {"vm": "[blue]vm [/]", "lxc": "[cyan]lxc[/]"}.get(h.type, "[dim]host[/]")
        vmid_str = str(h.vmid) if h.vmid else "[dim]—[/]"
        node = h.node or "[dim]—[/]"
        ip = h.ip or "[dim]—[/]"
        status = f"[green]{h.status}[/]" if h.online else f"[dim]{h.status}[/]"
        cpu = _fmt_pct(h.cpu_pct / 100) if h.online else "[dim]—[/]"
        mem = _fmt_pct(h.mem_pct / 100) if h.online else "[dim]—[/]"
        disk = _fmt_pct(h.disk_pct / 100) if h.disk_available else "[dim]—[/]"
        uptime = _fmt_uptime(h.uptime) if h.uptime > 0 else "[dim]—[/]"
        tags = h.tags.replace(";", " ") if h.tags else "[dim]—[/]"
        t.add_row(dot, h.name, kind, vmid_str, node, ip, status, cpu, mem, disk, uptime, tags)

    console.print(t)


def _render_hosts_table(hosts: list[NormalizedHost]) -> None:
    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    t.add_column("", width=2)
    t.add_column("Name", style="bold", min_width=20)
    t.add_column("Type", width=4)
    t.add_column("Node", min_width=10)
    t.add_column("IP", min_width=15)
    t.add_column("CPU", justify="right", min_width=6)
    t.add_column("Mem", justify="right", min_width=6)
    t.add_column("Disk", justify="right", min_width=6)
    t.add_column("Tags", style="dim", min_width=8)

    for h in hosts:
        dot = "[green]●[/]" if h.online else "[dim]●[/]"
        kind = {"vm": "[blue]vm [/]", "lxc": "[cyan]lxc[/]"}.get(h.type, "[dim]hst[/]")
        node = h.node or "[dim]—[/]"
        ip = h.ip or "[dim]—[/]"
        name = h.name if h.online else f"[dim]{h.name}[/]"
        cpu = _fmt_pct(h.cpu_pct / 100) if h.online else "[dim]—[/]"
        mem = _fmt_pct(h.mem_pct / 100) if h.online else "[dim]—[/]"
        disk = _fmt_pct(h.disk_pct / 100) if h.disk_available else "[dim]—[/]"
        tags = h.tags.replace(";", " ") if h.tags else "[dim]—[/]"
        t.add_row(dot, name, kind, node, ip, cpu, mem, disk, tags)

    console.print(t)


def _fmt_io(read: int, write: int) -> str:
    """Format cumulative disk or net I/O as 'Xr Xw'."""
    if read == 0 and write == 0:
        return "[dim]—[/]"
    return f"{_fmt_bytes(read)}r {_fmt_bytes(write)}w"


def _fmt_bps(recv: float, sent: float, *, rx_label: str = "↓", tx_label: str = "↑") -> str:
    """Format bytes-per-second rates as 'X↓ X↑'. Returns '—' if both are zero."""
    if recv == 0.0 and sent == 0.0:
        return "[dim]—[/]"
    return f"{_fmt_bytes(int(recv))}{rx_label} {_fmt_bytes(int(sent))}{tx_label}"


def _render_cluster_grouped(
    cluster: ClusterStatus,
    hosts: list[NormalizedHost],
    node_loads: dict[str, tuple[float, float, int]],
) -> None:
    """Single grouped table: Proxmox nodes with guests nested underneath,
    followed by standalone Beszel-only hosts (NAS, bare-metal Docker, etc.).

    Node columns:  ● Name | node | CPU | Load/nproc | IOWait | Mem | Disk | Uptime
    Guest columns: ● Name | type | CPU |     —      |   —    | Mem | Disk | DiskIO | NetIO | Uptime
    """
    by_node: dict[str, list[NormalizedHost]] = {}
    for h in hosts:
        if h.type in ("vm", "lxc"):
            by_node.setdefault(h.node, []).append(h)
    standalone = [h for h in hosts if h.type == "host"]

    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False, show_edge=False)
    t.add_column("", width=2)
    t.add_column("ID", justify="right", style="dim", min_width=4)
    t.add_column("Name", min_width=22)
    t.add_column("Type", width=5)
    t.add_column("CPU", justify="right", min_width=5)
    t.add_column("Load", justify="right", min_width=8)    # "0.42/4" or "—"
    t.add_column("IOWait", justify="right", min_width=7)
    t.add_column("Mem", justify="right", min_width=5)
    t.add_column("Disk", justify="right", min_width=5)
    t.add_column("Disk I/O", justify="right", min_width=14)   # "12Gr 3Gw"
    t.add_column("Net I/O", justify="right", min_width=14)    # "2Gr 800Mw"
    t.add_column("Uptime", min_width=8)

    def _guest_rows(guests: list[NormalizedHost]) -> None:
        running = [h for h in guests if h.online]
        stopped = [h for h in guests if not h.online]
        ordered = sorted(running, key=lambda h: h.name) + sorted(stopped, key=lambda h: h.name)
        for i, h in enumerate(ordered):
            is_last = i == len(ordered) - 1
            prefix = "  └ " if is_last else "  ├ "
            dot = "[green]●[/]" if h.online else "[dim]●[/]"
            kind = "[cyan]lxc[/]" if h.type == "lxc" else "[blue]vm [/]"
            name = f"{prefix}{h.name}" if h.online else f"{prefix}[dim]{h.name}[/]"
            cpu = _fmt_pct(h.cpu_pct / 100) if h.online else "[dim]—[/]"
            mem = _fmt_pct(h.mem_pct / 100) if h.online else "[dim]—[/]"
            disk = _fmt_pct(h.disk_pct / 100) if h.disk_available else "[dim]—[/]"
            # Prefer Beszel rate data; fall back to Proxmox cumulative
            if h.online and h.source == "merged":
                diskio = _fmt_bps(h.bz_disk_read_bps, h.bz_disk_write_bps, rx_label="r", tx_label="w")
                netio = _fmt_bps(h.bz_net_recv_bps, h.bz_net_sent_bps)
            elif h.online:
                diskio = _fmt_io(h.disk_read, h.disk_write)
                netio = _fmt_io(h.net_in, h.net_out)
            else:
                diskio = netio = "[dim]—[/]"
            uptime = _fmt_uptime(h.uptime) if h.uptime > 0 else "[dim]—[/]"
            vmid_str = str(h.vmid) if h.vmid else "[dim]—[/]"
            t.add_row(dot, vmid_str, name, kind, cpu, "[dim]—[/]", "[dim]—[/]", mem, disk, diskio, netio, uptime)

    for node in cluster.nodes:
        dot = "[green]●[/]" if node.online else "[red]●[/]"
        if node.online:
            cpu = _fmt_pct(node.cpu)
            load_val, wait_val, nproc = node_loads.get(node.name, (-1.0, -1.0, 0))
            if load_val >= 0:
                load = f"{load_val:.2f}/{nproc}" if nproc > 0 else f"{load_val:.2f}"
            else:
                load = "[dim]—[/]"
            wait = _fmt_pct(wait_val) if wait_val >= 0 else "[dim]—[/]"
            mem = _fmt_pct(node.mem_used / node.mem_total if node.mem_total else 0)
            disk = _fmt_pct(node.disk_used / node.disk_total if node.disk_total else 0)
            uptime = _fmt_uptime(node.uptime)
        else:
            cpu = load = wait = mem = disk = uptime = "[dim]—[/]"
        t.add_row(dot, "[dim]—[/]", f"[bold]{node.name}[/]", "[dim]node[/]",
                  cpu, load, wait, mem, disk, "[dim]—[/]", "[dim]—[/]", uptime)
        _guest_rows(by_node.get(node.name, []))

    for h in standalone:
        dot = "[green]●[/]" if h.online else "[red]●[/]"
        cpu = _fmt_pct(h.cpu_pct / 100) if h.online else "[dim]—[/]"
        mem = _fmt_pct(h.mem_pct / 100) if h.online else "[dim]—[/]"
        disk = _fmt_pct(h.disk_pct / 100) if h.disk_available else "[dim]—[/]"
        diskio = _fmt_bps(h.bz_disk_read_bps, h.bz_disk_write_bps, rx_label="r", tx_label="w") if h.online else "[dim]—[/]"
        netio = _fmt_bps(h.bz_net_recv_bps, h.bz_net_sent_bps) if h.online else "[dim]—[/]"
        uptime = _fmt_uptime(h.uptime) if h.uptime > 0 else "[dim]—[/]"
        t.add_row(dot, "[dim]—[/]", f"[bold]{h.name}[/]", "[dim]host[/]",
                  cpu, "[dim]—[/]", "[dim]—[/]", mem, disk, diskio, netio, uptime)

    console.print(t)


# ── Table renderers (compact / table) ────────────────────────────────────────

def _render_nodes_table(cluster: ClusterStatus) -> None:
    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    t.add_column("", width=2)
    t.add_column("Node", style="bold", min_width=16)
    t.add_column("CPU", justify="right", min_width=6)
    t.add_column("Mem", justify="right", min_width=6)
    t.add_column("Uptime", min_width=8)

    for n in cluster.nodes:
        if n.online:
            dot = "[green]●[/]"
            cpu = _fmt_pct(n.cpu)
            mem_pct = n.mem_used / n.mem_total if n.mem_total else 0
            mem = _fmt_pct(mem_pct)
            uptime = _fmt_uptime(n.uptime)
        else:
            dot = "[red]●[/]"
            cpu = mem = uptime = "[dim]—[/]"
        t.add_row(dot, n.name, cpu, mem, uptime)

    console.print(t)


def _render_storage_table(storages: list[StorageStatus], wide: bool = False) -> None:
    t = Table(
        title="Proxmox Storage Pools" if wide else None,
        box=box.ROUNDED if wide else box.SIMPLE,
        show_header=True,
        pad_edge=False,
    )
    t.add_column("Node", style="bold", min_width=14)
    t.add_column("Storage", min_width=12)
    t.add_column("Type", min_width=8)
    t.add_column("Used", justify="right")
    if wide:
        t.add_column("Used/Total", justify="right")
    else:
        t.add_column("Total", justify="right")
    t.add_column("Shared", justify="center")

    if not storages:
        console.print("[dim]No storage pools found (or all nodes offline).[/]")
        return

    for s in storages:
        if not s.enabled:
            used = "[dim]disabled[/]"
        elif not s.active:
            used = "[dim]inactive[/]"
        else:
            pct = s.used / s.total if s.total else 0
            used = _fmt_pct(pct, warn=0.70, crit=0.90)
        shared = "[cyan]●[/]" if s.shared else "[dim]—[/]"
        if wide:
            total_col = f"{_fmt_bytes(s.used)}/{_fmt_bytes(s.total)}"
            t.add_row(s.node, s.storage, s.type, used, total_col, shared)
        else:
            t.add_row(s.node, s.storage, s.type, used, _fmt_bytes(s.total), shared)

    console.print(t)


def _render_guests_table(
    cluster: ClusterStatus,
    guest_type: str | None,
    label_filter: str | None = None,
    ip_map: dict[int, str] | None = None,
) -> None:
    guests = [
        g for g in cluster.guests
        if guest_type is None or g.type == guest_type
    ]
    if label_filter:
        lf = label_filter.lower()
        guests = [g for g in guests if lf in _guest_tags(g)]
    guests.sort(key=lambda g: (g.status != "running", g.type, g.name))

    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    t.add_column("", width=2)
    t.add_column("ID", justify="right", style="dim", min_width=4)
    t.add_column("Name", style="bold", min_width=20)
    t.add_column("Type", width=4)
    t.add_column("Node", min_width=10)
    t.add_column("IP", min_width=15)
    t.add_column("Tags", style="dim", min_width=10)

    for g in guests:
        dot = "[green]●[/]" if g.status == "running" else "[dim]●[/]"
        kind = "[cyan]lxc[/]" if g.type == "lxc" else "[blue]vm[/] "
        tags = g.tags.replace(";", " ") if g.tags else "[dim]—[/]"
        name = g.name if g.status == "running" else f"[dim]{g.name}[/]"
        ip = (ip_map or {}).get(g.vmid, "[dim]—[/]") or "[dim]—[/]"
        t.add_row(dot, str(g.vmid), name, kind, g.node, ip, tags)

    console.print(t)


# ── get ───────────────────────────────────────────────────────────────────────

get_app = typer.Typer(help="Query cluster state — nodes, VMs, LXCs, and all hosts", no_args_is_help=False, rich_markup_mode="rich")
app.add_typer(get_app, name="get")


@get_app.callback(invoke_without_command=True)
def get_all(
    ctx: typer.Context,
    tag: Optional[str] = typer.Option(None, "-t", "--tag", help="Show detail for all hosts with this tag"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | wide | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """Show everything: Proxmox nodes and all hosts (VMs, LXCs, standalone).

    Use [cyan]-t <tag>[/] to show detail panels for all hosts matching a tag.
    """
    if ctx.invoked_subcommand is not None:
        return
    if json_flag:
        output = _Out.json

    # -t without a selector: detail panels for all matching guests
    if tag:
        run_get_detail("*", tag, output.value)
        return

    cfg = _load()
    with console.status("[bold]Fetching cluster state…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))

    async def _fetch_all():
        ip_task = fetch_guest_ips(cfg.proxmox, cluster.guests)
        load_task = fetch_node_loads(cfg.proxmox, [n.name for n in cluster.nodes if n.online])
        beszel_task = fetch_beszel(cfg.beszel) if cfg.beszel else asyncio.sleep(0)
        ip_map, node_loads, beszel_raw = await asyncio.gather(ip_task, load_task, beszel_task)
        return ip_map, node_loads, beszel_raw or []

    with console.status("[bold]Fetching metrics…[/]"):
        ip_map, node_loads, beszel_systems = asyncio.run(_fetch_all())

    hosts = merge_hosts(cluster, beszel_systems, ip_map)

    if output == _Out.json:
        console.print_json(_json.dumps({
            "nodes": _nodes_to_json(cluster),
            "hosts": _hosts_to_json(hosts),
        }))
    elif output == _Out.wide:
        _render_cluster_grouped(cluster, hosts, node_loads)
    else:
        _render_cluster_grouped(cluster, hosts, node_loads)


@get_app.command("nodes")
def get_nodes(
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | wide | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """List Proxmox hypervisor nodes."""
    if json_flag:
        output = _Out.json
    cfg = _load()
    with console.status("[bold]Fetching node status…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))

    if output == _Out.json:
        console.print_json(_json.dumps(_nodes_to_json(cluster)))
    elif output == _Out.wide:
        _render_nodes_wide(cluster)
    else:
        _render_nodes_table(cluster)


@get_app.command("vms")
def get_vms(
    label: Optional[str] = typer.Option(None, "-l", "--label", help="Filter by tag"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | wide | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """List virtual machines."""
    if json_flag:
        output = _Out.json
    cfg = _load()
    with console.status("[bold]Fetching VM status…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))
    with console.status("[bold]Fetching guest IPs…[/]"):
        ip_map = asyncio.run(fetch_guest_ips(cfg.proxmox, cluster.guests))

    if output == _Out.json:
        guests = _guests_to_json(cluster, "qemu", ip_map=ip_map)
        if label:
            guests = [g for g in guests if label.lower() in [t.lower() for t in (g.get("tags") or "").replace(",", ";").split(";") if t.strip()]]
        console.print_json(_json.dumps(guests))
    elif output == _Out.wide:
        _render_guests_wide(cluster, "qemu", ip_map=ip_map)
    else:
        _render_guests_table(cluster, "qemu", label_filter=label, ip_map=ip_map)


@get_app.command("lxcs")
def get_lxcs(
    label: Optional[str] = typer.Option(None, "-l", "--label", help="Filter by tag"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | wide | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """List LXC containers."""
    if json_flag:
        output = _Out.json
    cfg = _load()
    with console.status("[bold]Fetching LXC status…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))
    with console.status("[bold]Fetching guest IPs…[/]"):
        ip_map = asyncio.run(fetch_guest_ips(cfg.proxmox, cluster.guests))

    if output == _Out.json:
        guests = _guests_to_json(cluster, "lxc", ip_map=ip_map)
        if label:
            guests = [g for g in guests if label.lower() in [t.lower() for t in (g.get("tags") or "").replace(",", ";").split(";") if t.strip()]]
        console.print_json(_json.dumps(guests))
    elif output == _Out.wide:
        _render_guests_wide(cluster, "lxc", ip_map=ip_map)
    else:
        _render_guests_table(cluster, "lxc", label_filter=label, ip_map=ip_map)


@get_app.command("storage")
def get_storage(
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | wide | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """List Proxmox storage pool capacity/usage (the datastores backing VM/LXC disks).

    Unlike the Disk column on [cyan]atc get nodes[/], which reflects the node's
    root filesystem, this shows the actual storage pools (local-lvm, ZFS, NFS, etc.)
    where guest virtual disks live — the thing that fills up when VMs run out of space.
    """
    if json_flag:
        output = _Out.json
    cfg = _load()
    with console.status("[bold]Fetching cluster state…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))
    online_nodes = [n.name for n in cluster.nodes if n.online]
    with console.status("[bold]Fetching storage pools…[/]"):
        storages = asyncio.run(fetch_storage_status_async(cfg.proxmox, online_nodes))

    if output == _Out.json:
        console.print_json(_json.dumps(_storage_to_json(storages)))
    elif output == _Out.wide:
        _render_storage_table(storages, wide=True)
    else:
        _render_storage_table(storages, wide=False)


def _do_get_hosts(label: Optional[str], output: _Out) -> None:
    """Shared logic for 'get hosts', 'get ls', and 'ps' — avoids Typer OptionInfo default issue."""
    cfg = _load()
    with console.status("[bold]Fetching cluster state…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))

    async def _fetch_all():
        ip_task = fetch_guest_ips(cfg.proxmox, cluster.guests)
        load_task = fetch_node_loads(cfg.proxmox, [n.name for n in cluster.nodes if n.online])
        beszel_task = fetch_beszel(cfg.beszel) if cfg.beszel else asyncio.sleep(0)
        ip_map, node_loads, beszel_raw = await asyncio.gather(ip_task, load_task, beszel_task)
        return ip_map, node_loads, beszel_raw or []

    with console.status("[bold]Fetching metrics…[/]"):
        ip_map, node_loads, beszel_systems = asyncio.run(_fetch_all())

    hosts = merge_hosts(cluster, beszel_systems, ip_map)

    if label:
        lf = label.lower()
        hosts = [h for h in hosts if lf in [t for t in h.tags.replace(";", " ").split() if t]]

    if output == _Out.json:
        console.print_json(_json.dumps(_hosts_to_json(hosts)))
    elif output == _Out.wide:
        _render_hosts_wide(hosts)
    else:
        _render_cluster_grouped(cluster, hosts, node_loads)


@get_app.command("hosts")
def get_hosts(
    label: Optional[str] = typer.Option(None, "-l", "--label", help="Filter by tag"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | wide | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """List all hosts — Proxmox VMs, LXCs, and standalone Beszel-only hosts."""
    if json_flag:
        output = _Out.json
    _do_get_hosts(label, output)


@get_app.command("ls", hidden=True)
def get_ls(
    label: Optional[str] = typer.Option(None, "-l", "--label", help="Filter by tag"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | wide | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """Alias for 'get hosts'."""
    if json_flag:
        output = _Out.json
    _do_get_hosts(label, output)


# ── get <selector> — detail view (guests and nodes) ──────────────────────────

def _render_node_detail(node: "NodeStatus") -> None:
    BAR_W = 24
    console.print()
    console.rule(f"[bold]⬡  {node.name}  ·  node[/]")
    console.print()

    dot_c = "green" if node.online else "red"
    status_str = "online" if node.online else "offline"
    status_c = "green" if node.online else "red"
    uptime_part = f"   [dim]up[/] {_fmt_uptime(node.uptime)}" if node.online else ""
    console.print(f"   [{dot_c}]●[/] [{status_c}]{status_str}[/]{uptime_part}")
    console.print()

    if node.online:
        cpu_c = _pct_color(node.cpu)
        console.print(
            f"   CPU    [{cpu_c}]{_bar_pct(node.cpu, BAR_W)}[/]"
            f"  [{cpu_c}]{node.cpu * 100:>3.0f}%[/]"
        )
        if node.mem_total:
            mem_pct = node.mem_used / node.mem_total
            mem_c = _pct_color(mem_pct)
            console.print(
                f"   Mem    [{mem_c}]{_bar_pct(mem_pct, BAR_W)}[/]"
                f"  [{mem_c}]{mem_pct * 100:>3.0f}%[/]"
                f"  [dim]{_fmt_bytes(node.mem_used)} / {_fmt_bytes(node.mem_total)}[/]"
            )
        if node.disk_total:
            disk_pct = node.disk_used / node.disk_total
            disk_c = _pct_color(disk_pct)
            console.print(
                f"   Disk   [{disk_c}]{_bar_pct(disk_pct, BAR_W)}[/]"
                f"  [{disk_c}]{disk_pct * 100:>3.0f}%[/]"
                f"  [dim]{_fmt_bytes(node.disk_used)} / {_fmt_bytes(node.disk_total)}[/]"
            )

    if node.error:
        console.print(f"\n   [red]Error: {node.error}[/]")

    console.print()
    console.rule()
    console.print()


def _render_guest_detail(detail: GuestDetail) -> None:
    BAR_W = 24
    kind = "LXC" if detail.type == "lxc" else "VM"
    icon = "📦" if detail.type == "lxc" else "🖥 "

    console.print()
    console.rule(f"[bold]{icon} {detail.name}  ·  {kind} #{detail.vmid}[/]")
    console.print()

    # ── Status line ───────────────────────────────────────────────────────────
    dot_c = "green" if detail.status == "running" else "dim"
    status_c = "green" if detail.status == "running" else "dim"
    console.print(
        f"   [{dot_c}]●[/] [{status_c}]{detail.status}[/]"
        f"   [dim]node[/] {detail.node}"
        f"   [dim]up[/] {_fmt_uptime(detail.uptime)}"
    )
    console.print()

    # ── Metrics with bars ─────────────────────────────────────────────────────
    cores_label = f"{detail.cpu_cores} core{'s' if detail.cpu_cores != 1 else ''}"
    cpu_c = _pct_color(detail.cpu_usage)
    console.print(
        f"   CPU    [{cpu_c}]{_bar_pct(detail.cpu_usage, BAR_W)}[/]"
        f"  [{cpu_c}]{detail.cpu_usage * 100:>3.0f}%[/]"
        f"  [dim]({cores_label})[/]"
    )

    if detail.mem_total:
        mem_pct = detail.mem_used / detail.mem_total
        mem_c = _pct_color(mem_pct)
        console.print(
            f"   Mem    [{mem_c}]{_bar_pct(mem_pct, BAR_W)}[/]"
            f"  [{mem_c}]{mem_pct * 100:>3.0f}%[/]"
            f"  [dim]{_fmt_bytes(detail.mem_used)} / {_fmt_bytes(detail.mem_total)}[/]"
        )

    if detail.disk_total:
        disk_pct = detail.disk_used / detail.disk_total
        disk_c = _pct_color(disk_pct)
        console.print(
            f"   Disk   [{disk_c}]{_bar_pct(disk_pct, BAR_W)}[/]"
            f"  [{disk_c}]{disk_pct * 100:>3.0f}%[/]"
            f"  [dim]{_fmt_bytes(detail.disk_used)} / {_fmt_bytes(detail.disk_total)}[/]"
        )

    console.print()

    # ── Network / info ────────────────────────────────────────────────────────
    if detail.ips:
        console.print(f"   [dim]IP(s)[/]  {'  '.join(detail.ips)}")
    else:
        console.print("   [dim]IP(s)[/]  [dim]—[/]")

    if detail.ostype:
        console.print(f"   [dim]OS[/]     {detail.ostype}")

    if detail.tags:
        console.print(f"   [dim]Tags[/]   {detail.tags.replace(';', ' ')}")

    if detail.error:
        console.print(f"\n   [red]Error: {detail.error}[/]")

    console.print()
    console.rule()
    console.print()


def _guest_detail_to_json(detail: GuestDetail) -> dict:
    return {
        "vmid": detail.vmid,
        "name": detail.name,
        "node": detail.node,
        "type": detail.type,
        "status": detail.status,
        "cpu_usage": round(detail.cpu_usage, 4),
        "cpu_cores": detail.cpu_cores,
        "mem_used": detail.mem_used,
        "mem_total": detail.mem_total,
        "disk_used": detail.disk_used,
        "disk_total": detail.disk_total,
        "ips": detail.ips,
        "uptime": detail.uptime,
        "tags": detail.tags,
        "ostype": detail.ostype,
        **({"error": detail.error} if detail.error else {}),
    }


def run_get_detail(selector: str, tag: Optional[str], output: str = "table") -> None:
    """Resolve selector → node or guest(s) and render detail panel(s).

    Called from main.py's patched 'get' group for unknown subcommand names,
    and also from the get_all callback when -t is used without a subcommand.
    """
    cfg = _load()
    with console.status("[bold]Fetching cluster state…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))

    # Node match takes priority (nodes have no VMID)
    sel_lower = selector.lower()
    node_match = next((n for n in cluster.nodes if n.name.lower() == sel_lower), None)
    if node_match:
        if output == "json":
            console.print_json(_json.dumps({
                "name": node_match.name, "online": node_match.online,
                "cpu": round(node_match.cpu, 4),
                "mem_used": node_match.mem_used, "mem_total": node_match.mem_total,
                "disk_used": node_match.disk_used, "disk_total": node_match.disk_total,
                "uptime": node_match.uptime,
            }))
        else:
            _render_node_detail(node_match)
        return

    # Guest match
    matched = _resolve_guests(cluster, selector, label=tag)
    if not matched:
        hint = f" with tag '{tag}'" if (tag and not selector) else f" matching '{selector}'"
        console.print(f"[yellow]No hosts found{hint}.[/]")
        raise SystemExit(1)

    results: list[GuestDetail] = []
    for g in matched:
        with console.status(f"[bold]Fetching details for {g.name}…[/]"):
            results.append(fetch_guest_detail(cfg.proxmox, g.node, g.vmid, g.type))

    if output == "json":
        console.print_json(_json.dumps([_guest_detail_to_json(d) for d in results]))
    else:
        for detail in results:
            _render_guest_detail(detail)


def run_update_hosts(
    selector: str,
    tag: Optional[str],
    yes: bool = False,
    verbose: bool = False,
) -> None:
    """Resolve selector → running guest(s) and run apt upgrade via SSH.

    Called from main.py's patched 'update' group for unknown subcommand names,
    and also from the update_hosts Typer command.
    """
    cfg = _load()
    with console.status("[bold]Fetching cluster state…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))

    matched = _resolve_guests(cluster, selector, tag, require_status="running")
    if not matched:
        hint = f" with tag {repr(tag)}" if tag else ""
        console.print(f"[yellow]No running guests found matching '{selector}'{hint}[/]")
        raise SystemExit(1)

    with console.status("[bold]Resolving guest IPs…[/]"):
        # Keyed by vmid, not name — Proxmox guest names aren't unique across a
        # cluster (e.g. every community-script Pi-hole LXC is named "pihole"),
        # so a name-keyed dict silently collapses distinct guests onto one IP.
        ips = {g.vmid: get_node_ip(cfg.proxmox, g.node, g.vmid, g.type) for g in matched}

    # Separate HA OS guest (matched by IP) from regular apt targets
    ha_guest: GuestInfo | None = None
    ssh_guests: list[GuestInfo] = []
    for g in matched:
        if cfg.ha and ips.get(g.vmid) == cfg.ha.host:
            ha_guest = g
        else:
            ssh_guests.append(g)

    targets = []
    for g in ssh_guests:
        user = cfg.ssh.guest_user(g.name)
        if user is None:
            user = Prompt.ask(
                f"SSH user for [cyan]{g.name}[/]",
                default=cfg.ssh.default_user,
            )
            save_guest_user(cfg.config_path, g.name, user)
            cfg.ssh.guest_users[g.name] = user
        custom = cfg.ssh.guest_update_cmd(g.name)
        targets.append(UpdateTarget(
            name=f"{g.name} [dim]({'lxc' if g.type == 'lxc' else 'vm'} #{g.vmid} @ {g.node})[/dim]",
            host=ips[g.vmid] or g.name,
            user=user,
            update_cmd=custom.cmd if custom else None,
            update_cmd_headless=custom.headless if custom else False,
        ))

    if targets:
        _confirm_and_update(targets, cfg, yes, verbose=verbose)

    if ha_guest:
        _update_ha_via_api(ha_guest, cfg, yes)


def run_backup_run(
    selector: str,
    tag: Optional[str],
    storage: Optional[str] = None,
    yes: bool = False,
) -> None:
    """Resolve selector → running guest(s) and trigger vzdump backups.

    Called from main.py's patched 'backup' group for unknown subcommand names,
    and also from the backup_run Typer command.
    """
    cfg = _load()
    with console.status("[bold]Fetching cluster state…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))

    matched = _resolve_guests(cluster, selector, tag, require_status="running")
    if not matched:
        console.print(f"[yellow]No running guests found matching '{selector}'[/]")
        raise SystemExit(1)

    console.print("\nGuests to back up:")
    for g in matched:
        kind = "lxc" if g.type == "lxc" else "vm"
        console.print(f"  [cyan]•[/] {g.name} [dim]({kind} #{g.vmid} @ {g.node})[/]")

    if not yes and not Confirm.ask(f"\nBack up {len(matched)} guest(s)?"):
        raise SystemExit(0)

    for g in matched:
        stor = storage
        if not stor:
            with console.status(f"[bold]Finding backup storage on {g.node}…[/]"):
                stor = find_backup_storage(cfg.proxmox, g.node)
            if not stor:
                console.print(f"[red]✗[/] {g.name} — no backup storage found on {g.node}")
                continue

        with console.status(f"[bold]Backing up {g.name} → {stor}…[/]"):
            upid = backup_guest(cfg.proxmox, g.node, g.vmid, stor)
        if upid:
            console.print(f"[green]✓[/] {g.name} — task started [dim]({upid})[/]")
        else:
            console.print(f"[red]✗[/] {g.name} — failed to start backup")


# ── ps (alias: get hosts) ─────────────────────────────────────────────────────

@app.command("ps", hidden=False)
def ps_cmd(
    label: Optional[str] = typer.Option(None, "-l", "--label", help="Filter by tag"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | wide | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """List all hosts — VMs, LXCs, and standalone hosts. [dim](alias: get hosts)[/dim]"""
    if json_flag:
        output = _Out.json
    _do_get_hosts(label, output)


# ── start / stop / restart ────────────────────────────────────────────────────

def _power_action(
    selector: str,
    label: Optional[str],
    yes: bool,
    require_status: Optional[str],
    verb: str,
    action_fn,
) -> None:
    cfg = _load()
    with console.status("[bold]Fetching cluster state…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))

    matched = _resolve_guests(cluster, selector, label, require_status)
    if not matched:
        hint = f" with status '{require_status}'" if require_status else ""
        console.print(f"[yellow]No guests found matching '{selector}'{hint}[/]")
        raise typer.Exit(1)

    if verb in ("restart", "stop"):
        pihole_label_by_node = {p.name.lower(): p.name for p in cfg.pihole_instances}
        dns_hits = [
            pihole_label_by_node.get(g.node.lower(), f"{g.name} @ {g.node}")
            for g in matched
            if "pihole" in g.name.lower() or "pihole" in _guest_tags(g) or "adblock" in _guest_tags(g)
        ]
        if dns_hits:
            console.print(
                f"\n[bold red]⚠  DNS server(s) in this batch: {', '.join(dns_hits)}[/] — "
                "clients pointed at it will lose DNS resolution while it's down."
            )

    console.print(f"\nGuests to {verb}:")
    for g in matched:
        kind = "lxc" if g.type == "lxc" else "vm"
        console.print(f"  [cyan]•[/] {g.name} [dim]({kind} #{g.vmid} @ {g.node})[/]")

    if not yes and not Confirm.ask(f"\n{verb.capitalize()} {len(matched)} guest(s)?"):
        raise typer.Exit(0)

    for g in matched:
        with console.status(f"[bold]{verb.capitalize()}ing {g.name}…[/]"):
            ok = action_fn(cfg.proxmox, g.node, g.vmid, g.type)
        icon = "[green]✓[/]" if ok else "[red]✗[/]"
        result = f"{verb} issued" if ok else "failed"
        console.print(f"{icon} {g.name} — {result}")


@app.command("start")
def cmd_start(
    selector: str = typer.Argument(..., help="Name, tag, VMID, or '*'"),
    label: Optional[str] = typer.Option(None, "-l", "--label", help="Filter by tag"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Start stopped guest(s)."""
    _power_action(selector, label, yes, "stopped", "start", start_guest)


@app.command("stop")
def cmd_stop(
    selector: str = typer.Argument(..., help="Name, tag, VMID, or '*'"),
    label: Optional[str] = typer.Option(None, "-l", "--label", help="Filter by tag"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Gracefully shut down guest(s)."""
    _power_action(selector, label, yes, "running", "stop", stop_guest)


@app.command("restart")
def cmd_restart(
    selector: str = typer.Argument(..., help="Name, tag, VMID, or '*'"),
    label: Optional[str] = typer.Option(None, "-l", "--label", help="Filter by tag"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Restart running guest(s)."""
    _power_action(selector, label, yes, "running", "restart", restart_guest)


# ── update ────────────────────────────────────────────────────────────────────

update_app = typer.Typer(
    help="Run apt updates on nodes, VMs, or LXCs",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(update_app, name="update")


@update_app.command("nodes")
def update_nodes(
    name: str = typer.Argument("all", help="Node name or 'all'"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full apt output"),
) -> None:
    """Run apt upgrade on Proxmox hypervisor nodes.

    SSH hostname resolved via [bold]node_host_pattern[/] in config.yaml
    (default: [cyan]{name}[/], e.g. [cyan]pve-{name}.local.example.com[/]).
    """
    cfg = _load()
    with console.status("[bold]Fetching cluster state…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))

    online = [n for n in cluster.nodes if n.online]

    if name == "all":
        matched = online
    else:
        matched = [n for n in online if n.name.lower() == name.lower()]
        if not matched:
            matched = [n for n in online if name.lower() in n.name.lower()]

    if not matched:
        console.print(f"[yellow]No online node found matching '{name}'[/]")
        raise typer.Exit(1)

    targets = [
        UpdateTarget(name=n.name, host=cfg.ssh.node_host(n.name), user=cfg.ssh.default_user)
        for n in matched
    ]
    _confirm_and_update(targets, cfg, yes, verbose=verbose)


@update_app.command("hosts")
def update_hosts(
    selector: str = typer.Argument("*", help="Name (substring), tag, VMID, or '*' for all"),
    label: Optional[str] = typer.Option(None, "-l", "--label", help="Filter by tag (alternative to selector)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show full apt output"),
) -> None:
    """Run apt upgrade on VMs and LXC containers.

    [bold]Selector[/] can be:
      [cyan]*[/]        — all running guests
      [cyan]101[/]      — guest by VMID
      [cyan]pihole[/]   — name substring [dim]or[/] exact tag match

    [bold]-l / --label[/] filters by tag (can combine with [cyan]*[/] selector).
    """
    run_update_hosts(selector, label, yes, verbose)


@update_app.command("set-cmd")
def update_set_cmd(
    guest: str = typer.Argument(..., help="Guest name (must match exactly, e.g. 'pihole')"),
    cmd: str = typer.Argument(..., help="Custom update command to run after apt (e.g. 'update')"),
    headless: bool = typer.Option(False, "--headless", help="Stream output instead of handing off a PTY"),
) -> None:
    """Register a custom post-apt update command for a guest.

    Community-script LXCs (Pi-hole, AdGuard, etc.) ship their own updater
    that refreshes both packages and the project repo. Register it here so
    [bold]atc update hosts[/] runs it after the normal apt pass.

    Use [bold]--headless[/] when the script supports unattended mode (no TUI wizard),
    so atc streams the output inline instead of handing off the terminal.

    Example:
      [cyan]atc update set-cmd pihole "pihole updatePihole --unattended" --headless[/]
      [cyan]atc update set-cmd adguard update[/]   [dim]# interactive TUI — no flag[/]
    """
    cfg = _load()
    save_guest_update_cmd(cfg.config_path, guest, cmd, headless=headless)
    mode = "[green]headless[/] (streamed)" if headless else "[yellow]interactive[/] (PTY handoff)"
    console.print(f"[green]✓[/] Saved: [cyan]{guest}[/] → [bold]{cmd}[/] ({mode})")
    console.print(f"[dim]Next [bold]atc update hosts {guest}[/bold] will run apt, then [bold]{cmd}[/bold].[/]")


@update_app.command("remove-cmd")
def update_remove_cmd(
    guest: str = typer.Argument(..., help="Guest name whose custom update command should be removed"),
) -> None:
    """Remove a guest's custom post-apt update command.

    After removal, [bold]atc update hosts[/] will only run the standard apt pass for that guest.

    Example:
      [cyan]atc update remove-cmd plex[/]
    """
    cfg = _load()
    removed = remove_guest_update_cmd(cfg.config_path, guest)
    if removed:
        console.print(f"[green]✓[/] Removed custom update command for [cyan]{guest}[/].")
    else:
        console.print(f"[yellow]![/] No custom update command found for [cyan]{guest}[/].")


def _parse_exit(lines: list[str]) -> bool:
    """Return True if the update command exited 0 (reads __ATC_EXIT__:N marker)."""
    for line in reversed(lines):
        if line.startswith(_EXIT_MARKER + ":"):
            return line.split(":", 1)[1].strip() == "0"
    return False  # marker missing = SSH error or unexpected termination


def _confirm_and_update(targets: list[UpdateTarget], cfg: Config, yes: bool, verbose: bool = False) -> None:
    console.print("\nTargets to update:")
    for t in targets:
        if t.update_cmd:
            mode = "headless" if t.update_cmd_headless else "interactive"
            extra = f" [dim]+ [cyan]{t.update_cmd}[/cyan] ({mode})[/dim]"
        else:
            extra = ""
        console.print(f"  [cyan]•[/] {t.name}{extra}")

    if not yes:
        if not Confirm.ask(f"\nRun apt upgrade on {len(targets)} target(s)?"):
            raise typer.Exit(0)

    base_cmd = _UPDATE_CMD_VERBOSE if verbose else _UPDATE_CMD_QUIET

    def _cmd_for(target: UpdateTarget) -> str:
        if target.user != "root":
            return f"sudo bash -c '{base_cmd}'"
        return base_cmd

    # (target, success, needs_reboot, failed_units, output_lines)
    results: list[tuple[UpdateTarget, bool, bool, list[str], list[str]]] = []

    async def _post_checks(target: UpdateTarget) -> tuple[bool, list[str]]:
        reboot_r = await run_command(
            target.host,
            "test -f /var/run/reboot-required && cat /var/run/reboot-required.pkgs 2>/dev/null || true",
            cfg.ssh, user=target.user,
        )
        failed_r = await run_command(
            target.host,
            "systemctl --failed --no-legend 2>/dev/null | awk '{print ($1~/\\./) ? $1 : $2}'",
            cfg.ssh, user=target.user,
        )
        needs_reboot = bool(reboot_r.stdout.strip())
        _NOISE_UNITS = {"motd-news.service", "apt-news.service", "ua-timer.service"}
        failed_units = [
            u for u in failed_r.stdout.strip().splitlines()
            if u and u not in _NOISE_UNITS
        ]
        return needs_reboot, failed_units

    async def _run_all() -> None:
        if verbose:
            for target in targets:
                guest_name = Text.from_markup(target.name).plain
                console.rule(f"[bold]{guest_name}[/] [dim]({target.host})[/]")
                lines: list[str] = []
                async for line in stream_command(target.host, _cmd_for(target), cfg.ssh, user=target.user):
                    lines.append(line)
                    if line.startswith(_EXIT_MARKER):
                        break
                    console.print(f"  {line}")
                success = _parse_exit(lines)
                needs_reboot, failed_units = await _post_checks(target)
                icon = "❌" if not success or failed_units else ("⚠️ " if needs_reboot else "✅")
                console.print(f"\n{icon} {guest_name} ({target.host}) done\n")
                results.append((target, success, needs_reboot, failed_units, lines))
        else:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(bar_width=20),
                MofNCompleteColumn(),
                TextColumn("[dim]{task.fields[status]}"),
                console=console,
            ) as progress:
                overall = progress.add_task(
                    f"[bold]Updating {len(targets)} host(s)", total=len(targets), status=""
                )
                for target in targets:
                    guest_name = Text.from_markup(target.name).plain
                    htask = progress.add_task(
                        f"  [cyan]{guest_name}[/] [dim]({target.host})[/]", total=None, status="connecting…"
                    )
                    lines2: list[str] = []
                    async for line in stream_command(target.host, _cmd_for(target), cfg.ssh, user=target.user):
                        lines2.append(line)
                        if line.startswith(_EXIT_MARKER):
                            break
                        short = line.strip()[:70]
                        if short:
                            progress.update(htask, status=short)
                    success = _parse_exit(lines2)
                    needs_reboot, failed_units = await _post_checks(target)
                    icon = "❌" if not success or failed_units else ("⚠️ " if needs_reboot else "✅")
                    progress.update(htask, completed=1, total=1, status=f"{icon} done")
                    progress.advance(overall)
                    results.append((target, success, needs_reboot, failed_units, lines2))

    asyncio.run(_run_all())

    # Phase 2 — run custom update commands (headless streamed or interactive PTY)
    # Maps plain guest name → exit code
    custom_results: dict[str, int] = {}
    custom_targets = [t for t in targets if t.update_cmd]

    async def _run_headless_custom(t: UpdateTarget) -> int:
        """Stream a headless custom command, return exit code."""
        cmd = f"{{ TERM=xterm-256color {t.update_cmd}; }} 2>&1; echo {_EXIT_MARKER}:$?"
        if t.user != "root":
            cmd = f"sudo bash -c '{cmd}'"
        lines: list[str] = []
        async for line in stream_command(t.host, cmd, cfg.ssh, user=t.user):
            lines.append(line)
            if line.startswith(_EXIT_MARKER):
                break
            short = line.strip()[:70]
            if short:
                console.print(f"  [dim]{short}[/]")
        return 0 if _parse_exit(lines) else 1

    if custom_targets:
        headless = [t for t in custom_targets if t.update_cmd_headless]
        interactive = [t for t in custom_targets if not t.update_cmd_headless]

        if headless:
            console.print(f"\n[bold]Running headless custom update(s) on {len(headless)} host(s)…[/bold]")
            for t in headless:
                guest_name = Text.from_markup(t.name).plain
                console.rule(f"[bold]{guest_name}[/] — [cyan]{t.update_cmd}[/]")
                rc = asyncio.run(_run_headless_custom(t))
                custom_results[guest_name] = rc
                icon = "✅" if rc == 0 else "❌"
                console.rule(f"{icon} [bold]{guest_name}[/] — [cyan]{t.update_cmd}[/] done")

        if interactive:
            console.print(
                f"\n[bold]Running interactive custom update(s) on "
                f"{len(interactive)} host(s) — terminal handed off for each:[/bold]"
            )
            for t in interactive:
                guest_name = Text.from_markup(t.name).plain
                console.rule(f"[bold]{guest_name}[/] — [cyan]{t.update_cmd}[/]")
                ssh_args = ["ssh"]
                key = cfg.ssh.expanded_key_file
                if key:
                    ssh_args += ["-i", key]
                if not cfg.ssh.verify_host_keys:
                    ssh_args += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
                ssh_args += ["-t", f"{t.user}@{t.host}", f"TERM=xterm-256color {t.update_cmd}"]
                proc = subprocess.run(ssh_args)
                custom_results[guest_name] = proc.returncode
                icon = "✅" if proc.returncode == 0 else "❌"
                console.rule(f"{icon} [bold]{guest_name}[/] — [cyan]{t.update_cmd}[/] done")

    _print_update_summary(results, cfg, custom_results=custom_results)


def _print_update_summary(
    results: list[tuple[UpdateTarget, bool, bool, list[str], list[str]]],
    cfg: Config,
    custom_results: dict[str, int] | None = None,
) -> None:
    custom_results = custom_results or {}
    has_custom = any(t.update_cmd for t, *_ in results)

    table = Table(title="Update Summary", box=box.ROUNDED)
    table.add_column("Guest", style="cyan")
    table.add_column("IP")
    table.add_column("apt", justify="center")
    if has_custom:
        table.add_column("custom cmd", justify="center")
    table.add_column("Reboot", justify="center")
    table.add_column("Failed Units")

    for target, success, needs_reboot, failed_units, _lines in results:
        guest_name = Text.from_markup(target.name).plain
        apt_icon = "✅" if success and not failed_units else "❌"
        row = [guest_name, target.host, apt_icon]
        if has_custom:
            if target.update_cmd:
                rc = custom_results.get(guest_name)
                row.append("✅" if rc == 0 else ("❌" if rc is not None else "—"))
            else:
                row.append("—")
        row += ["⚠️  yes" if needs_reboot else "—", ", ".join(failed_units) if failed_units else "—"]
        table.add_row(*row)

    console.print()
    console.print(table)

    for target, success, needs_reboot, failed_units, output_lines in results:
        guest_name = Text.from_markup(target.name).plain
        user = target.user
        if not success and not failed_units:
            # Show last few non-empty output lines as the error context
            relevant = [l for l in output_lines if l.strip() and not l.startswith(_EXIT_MARKER)][-8:]
            if relevant:
                console.print(f"\n[red]❌  {guest_name} ({target.host}) — last output:[/]")
                for l in relevant:
                    console.print(f"    [dim]{l}[/]")
        if needs_reboot:
            console.print(f"\n[yellow]⚠️  {guest_name} requires a reboot:[/]")
            console.print(f"    ssh {user}@{target.host} reboot")
        if failed_units:
            console.print(f"\n[red]❌  {guest_name} — failed units:[/]")
            for unit in failed_units:
                console.print(f"    ssh {user}@{target.host} systemctl status {unit}")
                console.print(f"    ssh {user}@{target.host} journalctl -u {unit} -n 50")


_HA_RESTART_CATEGORIES = {"core", "supervisor", "os"}

def _update_ha_via_api(guest: GuestInfo, cfg: Config, yes: bool) -> None:
    """Update Home Assistant OS VM via the HA REST API."""
    if cfg.ha is None:
        return
    console.rule(f"[bold]Home Assistant[/] [dim](vm #{guest.vmid} @ {guest.node})[/]")
    with console.status("[bold]Fetching pending HA updates…[/]"):
        try:
            pending = ha_list_updates(cfg.ha, pending_only=True)
        except Exception as e:
            console.print(f"[red]✗[/] Could not reach Home Assistant: {e}")
            return

    if not pending:
        console.print("[green]✓[/] Home Assistant — all up to date.")
        return

    # Separate updates HA can't actually install via the API (no INSTALL support —
    # e.g. Pi-hole, most Zigbee firmware) from everything else, and sort restarters
    # (core/os/supervisor) to run last.
    not_installable = [u for u in pending if not u.supports_install]
    restarters = [u for u in pending if u.short_category in _HA_RESTART_CATEGORIES]
    normal = [u for u in pending if u not in not_installable and u not in restarters]
    installable = normal + restarters

    console.print(f"[bold]{len(pending)}[/] update(s) available:")
    for u in sorted(pending, key=lambda x: (x.short_category, x.title)):
        note = " [dim](install via HA UI)[/]" if u in not_installable else ""
        console.print(f"  [dim]•[/] [bold]{u.title}[/]  {u.installed_version} → [yellow]{u.latest_version}[/]  [dim]({u.short_category})[/]{note}")

    if restarters:
        console.print(f"\n[yellow]⚠[/]  {', '.join(u.title for u in restarters)} will restart HA — install last.")
    if not_installable:
        console.print(f"[dim]ℹ  {len(not_installable)} update(s) skipped — no API install support, trigger from the HA UI.[/]")

    if not installable:
        return
    if not yes and not Confirm.ask(f"\nInstall {len(installable)} HA update(s)?"):
        return

    errors = []
    for u in installable:
        will_restart = u.short_category in _HA_RESTART_CATEGORIES
        try:
            ha_call_service(cfg.ha, "update", "install", {"entity_id": u.entity_id})
            console.print(f"  [green]✓[/] {u.title}")
        except Exception as e:
            err_str = str(e)
            if will_restart and ("timed out" in err_str.lower() or "connect" in err_str.lower()):
                console.print(f"  [yellow]↺[/] {u.title} — HA is restarting…")
            elif "500" in err_str:
                console.print(f"  [yellow]⚠[/] {u.title} — not installable via API, use the HA UI")
            else:
                console.print(f"  [red]✗[/] {u.title}: {e}")
                errors.append(u.entity_id)

    if errors:
        console.print(f"[red]{len(errors)} HA update(s) failed.[/]")
    else:
        console.print("[green]✓[/] HA updates initiated.")


_EXIT_MARKER = "__ATC_EXIT__"

_UPDATE_CMD_QUIET = (
    "{ DEBIAN_FRONTEND=noninteractive apt-get update -q && "
    "DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y -q && "
    f"apt-get autoremove -y -q; }} 2>&1; echo {_EXIT_MARKER}:$?"
)

_UPDATE_CMD_VERBOSE = (
    "{ DEBIAN_FRONTEND=noninteractive apt-get update && "
    "DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y && "
    f"apt-get autoremove -y; }} 2>&1; echo {_EXIT_MARKER}:$?"
)


# ── push-key ──────────────────────────────────────────────────────────────────

@app.command("push-key")
def push_key(
    selector: str = typer.Argument("*", help="Name (substring), tag, VMID, or '*' for all"),
    key_file: Optional[str] = typer.Option(None, "--key-file", "-k", help="Public key file (default: auto-detect)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Push your SSH public key to guests without needing existing SSH access.

    [bold]LXC containers[/] — SSHes to the Proxmox node and runs [cyan]pct exec[/].
    [bold]QEMU VMs[/]       — uses the QEMU guest agent exec API.

    Target user defaults to [cyan]ssh.default_user[/] in config, overridable
    per-guest via [cyan]ssh.guest_users[/] or [bold]atc get push-key[/] --user.
    Run once to unlock [bold]atc update hosts[/] for community-spun LXCs.
    """
    cfg = _load()

    # Resolve public key file
    if key_file:
        pub_path = Path(key_file).expanduser()
    else:
        for candidate in ["~/.ssh/id_ed25519.pub", "~/.ssh/id_rsa.pub"]:
            pub_path = Path(candidate).expanduser()
            if pub_path.exists():
                break
        else:
            console.print("[red]No public key found. Create ~/.ssh/id_ed25519.pub or pass --key-file.[/]")
            raise typer.Exit(1)

    if not pub_path.exists():
        console.print(f"[red]Key file not found: {pub_path}[/]")
        raise typer.Exit(1)

    pubkey = pub_path.read_text().strip()
    console.print(f"[dim]Key:[/] {pub_path}")
    console.print(f"[dim]{pubkey[:72]}{'…' if len(pubkey) > 72 else ''}[/]\n")

    with console.status("[bold]Fetching cluster state…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))

    matched = _resolve_guests(cluster, selector, None, require_status="running")
    if not matched:
        console.print(f"[yellow]No running guests found matching '{selector}'[/]")
        raise typer.Exit(1)

    console.print("Guests to receive key:")
    for g in matched:
        kind = "lxc" if g.type == "lxc" else "vm"
        console.print(f"  [cyan]•[/] {g.name} [dim]({kind} #{g.vmid} @ {g.node})[/]")

    if not yes and not Confirm.ask(f"\nPush key to {len(matched)} guest(s)?"):
        raise typer.Exit(0)

    console.print()
    failed = 0
    for g in matched:
        kind = "lxc" if g.type == "lxc" else "vm"
        target_user = cfg.ssh.guest_user(g.name) or cfg.ssh.default_user
        with console.status(f"  [bold]{g.name}[/] [dim]({target_user})[/]…"):
            if g.type == "lxc":
                # SSH to the Proxmox node and run pct exec
                key_script = ssh_key_inject_script(pubkey, target_user)
                node_host = cfg.ssh.node_host(g.node)
                pct_cmd = f"pct exec {g.vmid} -- bash -c {_shell_quote(key_script)}"
                result = asyncio.run(run_command(node_host, pct_cmd, cfg.ssh))
                ok = result.success
                msg = (result.stderr or result.stdout).strip() if not ok else ""
            else:
                # QEMU VM — use guest agent exec API
                ok, msg = push_ssh_key_qemu(cfg.proxmox, g.node, g.vmid, pubkey, user=target_user)
        if ok:
            console.print(f"  ✅ {g.name} [dim]({kind} #{g.vmid})[/]")
        else:
            console.print(f"  ❌ {g.name} [dim]({kind} #{g.vmid})[/] — [red]{msg}[/]")
            failed += 1

    console.print()
    if failed:
        console.print(
            f"[red]{failed} guest(s) failed.[/]  "
            "LXCs need SSH access to the Proxmox node (root). "
            "VMs need qemu-guest-agent running inside."
        )
        raise typer.Exit(1)
    else:
        console.print(f"[green]✓[/] Key pushed to {len(matched)} guest(s). You can now run [bold]atc update hosts[/].")


# ── backup ────────────────────────────────────────────────────────────────────

backup_app = typer.Typer(
    help="List and trigger Proxmox backups",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(backup_app, name="backup")


@backup_app.command("list")
def backup_list(
    label: Optional[str] = typer.Option(None, "-l", "--label", help="Filter by guest tag"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | wide | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """List backup files across all cluster nodes.

    [bold]table[/] (default): latest backup per guest.
    [bold]wide[/]: every individual backup file.
    """
    if json_flag:
        output = _Out.json
    cfg = _load()
    with console.status("[bold]Fetching cluster state…[/]"):
        cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))
    online_nodes = [n.name for n in cluster.nodes if n.online]
    with console.status("[bold]Fetching backups…[/]"):
        backups = list_backups(cfg.proxmox, online_nodes)

    name_map = {g.vmid: g.name for g in cluster.guests}

    if label:
        lf = label.lower()
        tagged = {g.vmid for g in cluster.guests if lf in _guest_tags(g)}
        backups = [b for b in backups if b.vmid in tagged]

    if output == _Out.json:
        console.print_json(_json.dumps([
            {
                "vmid": b.vmid,
                "name": name_map.get(b.vmid, f"vmid-{b.vmid}"),
                "storage": b.storage,
                "volid": b.volid,
                "size": b.size,
                "ctime": b.ctime,
                "format": b.fmt,
                "notes": b.notes,
            }
            for b in backups
        ]))
        return

    if output == _Out.wide:
        t = Table(title="Backups", box=box.ROUNDED)
        t.add_column("VMID", justify="right", style="dim")
        t.add_column("Name", style="bold")
        t.add_column("Date")
        t.add_column("Size", justify="right")
        t.add_column("Storage")
        t.add_column("Format", style="dim")
        for b in backups:
            dt = datetime.fromtimestamp(b.ctime).strftime("%Y-%m-%d %H:%M") if b.ctime else "—"
            t.add_row(str(b.vmid), name_map.get(b.vmid, f"vmid-{b.vmid}"), dt,
                      _fmt_bytes(b.size), b.storage, b.fmt)
        console.print(t)
        return

    # Compact: latest backup per VM
    grouped: dict[int, list[BackupFile]] = {}
    for b in backups:
        grouped.setdefault(b.vmid, []).append(b)

    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    t.add_column("ID", justify="right", style="dim", min_width=4)
    t.add_column("Name", style="bold", min_width=20)
    t.add_column("Latest Backup", min_width=16)
    t.add_column("Size", justify="right", min_width=6)
    t.add_column("Count", justify="right", min_width=5)
    t.add_column("Storage", style="dim")

    for vmid in sorted(grouped):
        files = grouped[vmid]  # sorted by -ctime already
        latest = files[0]
        dt = datetime.fromtimestamp(latest.ctime).strftime("%Y-%m-%d %H:%M") if latest.ctime else "—"
        t.add_row(str(vmid), name_map.get(vmid, f"vmid-{vmid}"), dt,
                  _fmt_bytes(latest.size), str(len(files)), latest.storage)
    console.print(t)


@backup_app.command("ls", hidden=True)
def backup_ls(
    label: Optional[str] = typer.Option(None, "-l", "--label", help="Filter by guest tag"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | wide | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """Alias for 'list'."""
    if json_flag:
        output = _Out.json
    backup_list(label=label, output=output)


@backup_app.command("jobs")
def backup_jobs() -> None:
    """List scheduled backup jobs."""
    cfg = _load()
    with console.status("[bold]Fetching backup jobs…[/]"):
        jobs = list_backup_jobs(cfg.proxmox)

    if not jobs:
        console.print("[dim]No scheduled backup jobs found.[/]")
        return

    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    t.add_column("", width=2)
    t.add_column("ID", style="bold", min_width=16)
    t.add_column("Schedule", min_width=14)
    t.add_column("Storage", min_width=10)
    t.add_column("VMs")
    t.add_column("Node", style="dim")
    t.add_column("Comment", style="dim")

    for j in jobs:
        dot = "[green]●[/]" if j.enabled else "[dim]●[/]"
        node_str = j.node or "[dim]all[/]"
        t.add_row(dot, j.id, j.schedule, j.storage, j.vmids, node_str, j.comment or "—")
    console.print(t)
    console.print(f"\n[dim]Run a job now: [bold]atc backup trigger <id>[/][/]")


@backup_app.command("trigger")
def backup_trigger(
    job_id: str = typer.Argument(..., help="Job ID from 'atc backup jobs'"),
) -> None:
    """Trigger an immediate run of a scheduled backup job."""
    cfg = _load()
    with console.status(f"[bold]Triggering job '{job_id}'…[/]"):
        ok = run_backup_job(cfg.proxmox, job_id)
    if ok:
        console.print(f"[green]✓[/] Job [cyan]{job_id}[/] started.")
    else:
        console.print(f"[red]✗[/] Failed to trigger '{job_id}'. Check the ID with [bold]atc backup jobs[/].")
        raise typer.Exit(1)


@backup_app.command("run")
def backup_run(
    selector: str = typer.Argument(..., help="Name, tag, VMID, or '*'"),
    label: Optional[str] = typer.Option(None, "-l", "--label", help="Filter by tag"),
    storage: Optional[str] = typer.Option(None, "--storage", "-s", help="Backup storage (auto-detected if omitted)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Create an immediate backup of guest(s)."""
    run_backup_run(selector, label, storage, yes)


# ── run ───────────────────────────────────────────────────────────────────────

@app.command("run")
def run_script(
    name: Optional[str] = typer.Argument(None, help="Script name or partial path. Omit to list all."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Run with bash -v (echo each command)"),
) -> None:
    """Run a script from the repo. Omit NAME to list available scripts."""
    cfg = _load()
    scripts_dir = cfg.resolved_scripts_dir
    scripts = _find_scripts(scripts_dir)

    if not name:
        _list_scripts(scripts, scripts_dir)
        return

    matches = [
        s for s in scripts
        if name.lower() in s.name.lower() or name.lower() in str(s.relative_to(scripts_dir)).lower()
    ]

    if not matches:
        console.print(f"[red]No script found matching '{name}'[/]")
        console.print("Run [bold]atc run[/] (no args) to list available scripts.")
        raise typer.Exit(1)

    if len(matches) > 1:
        console.print("[yellow]Multiple matches:[/]")
        _list_scripts(matches, scripts_dir)
        console.print("Be more specific.")
        raise typer.Exit(1)

    script = matches[0]
    rel = script.relative_to(scripts_dir)
    console.rule(f"[bold]{rel}[/]")

    import subprocess
    cmd = ["bash", "-v", str(script)] if verbose else ["bash", str(script)]
    result = subprocess.run(cmd, cwd=str(script.parent))
    if result.returncode != 0:
        console.print(f"\n[red]Exit code: {result.returncode}[/]")
        raise typer.Exit(result.returncode)


def _list_scripts(scripts: list[Path], scripts_dir: Path) -> None:
    t = Table(title="Available Scripts", box=box.SIMPLE)
    t.add_column("Script", style="cyan")
    t.add_column("Description", style="dim")
    for s in scripts:
        try:
            rel = s.relative_to(scripts_dir)
        except ValueError:
            rel = s
        desc = _get_script_description(s)
        t.add_row(str(rel), desc)
    console.print(t)


# ── secrets ───────────────────────────────────────────────────────────────────

secrets_app = typer.Typer(help="Manage secrets stored in the system keychain", no_args_is_help=False, rich_markup_mode="rich")
app.add_typer(secrets_app, name="secrets")


@secrets_app.callback(invoke_without_command=True)
def secrets_default(ctx: typer.Context) -> None:
    """List all secrets stored for atc."""
    if ctx.invoked_subcommand is None:
        _secrets_list()


@secrets_app.command("list")
def _secrets_list() -> None:
    """List all secret keys stored in the keychain."""
    keys = secrets_list()
    if not keys:
        console.print(f"[dim]No secrets stored yet for service '{KEYCHAIN_SERVICE}'.[/]")
        console.print("Secrets are created automatically on first use of a [bold]keychain:<key>[/] value in config.yaml.")
        return

    t = Table(title=f"Keychain secrets (service: '{KEYCHAIN_SERVICE}')", box=box.ROUNDED)
    t.add_column("Key", style="cyan bold")
    for key in keys:
        t.add_row(key)
    console.print(t)


@secrets_app.command("set")
def secrets_set(
    key: str = typer.Argument(..., help="Secret key name"),
    value: Optional[str] = typer.Argument(None, help="Value (prompted securely if omitted)"),
) -> None:
    """Set or update a secret in the keychain."""
    if value is None:
        value = getpass.getpass(f"Value for '{key}': ").strip()
        if not value:
            console.print("[red]Empty value — aborted.[/]")
            raise typer.Exit(1)
    secret_set(key, value)
    console.print(f"[green]✓[/] Stored [cyan]{key!r}[/] in keychain.")


@secrets_app.command("delete")
def secrets_delete(
    key: str = typer.Argument(..., help="Secret key to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a secret from the keychain."""
    if not yes:
        if not Confirm.ask(f"Delete secret '{key}'?"):
            raise typer.Exit(0)
    if secret_delete(key):
        console.print(f"[green]✓[/] Deleted [cyan]{key!r}[/].")
    else:
        console.print(f"[red]Key '{key}' not found.[/]")
        raise typer.Exit(1)


@secrets_app.command("verify")
def secrets_verify_cmd(
    key: str = typer.Argument(..., help="Secret key to verify against"),
) -> None:
    """Verify a value matches the stored secret by comparing SHA-256 hashes.

    Prompts for re-entry. Never prints or reveals the stored value.
    """
    candidate = getpass.getpass(f"Enter value to verify for '{key}': ").strip()
    if not candidate:
        console.print("[red]Empty value — aborted.[/]")
        raise typer.Exit(1)
    if secret_verify(key, candidate):
        console.print(f"[green]✓[/] Match — the stored value for [cyan]{key!r}[/] is correct.")
    else:
        console.print(f"[red]✗[/] No match for [cyan]{key!r}[/].")
        raise typer.Exit(1)


@secrets_app.command("scan")
def secrets_scan_cmd() -> None:
    """Scan config.yaml for keychain: references and repair the index.

    Checks which referenced keys are stored in the keychain and adds any
    missing ones to the index. No values are read or displayed.
    """
    repaired, missing = secrets_scan_config()
    if repaired:
        for key in repaired:
            console.print(f"[green]✓[/] Added to index: [cyan]{key}[/]")
    if missing:
        for key in missing:
            console.print(f"[yellow]![/] Not in keychain: [cyan]{key}[/] — run [bold]atc secrets set {key}[/]")
    if not repaired and not missing:
        console.print("[dim]Index is complete — all config.yaml secrets are indexed.[/]")
    elif not repaired:
        console.print("[dim]No new keys added to index.[/]")


@secrets_app.command("repair")
def secrets_repair_cmd(
    key: str = typer.Argument(..., help="Secret key to repair in the index"),
) -> None:
    """Re-index a secret by verifying it matches the stored value via SHA-256 hash.

    Prompts for the value. If the hash matches what is stored in the keychain,
    the key is added to the index so it appears in 'atc secrets list'.
    The stored value is never printed or returned.
    """
    candidate = getpass.getpass(f"Enter value for '{key}' to verify and re-index: ").strip()
    if not candidate:
        console.print("[red]Empty value — aborted.[/]")
        raise typer.Exit(1)
    if secret_repair(key, candidate):
        console.print(f"[green]✓[/] Hash matched — [cyan]{key!r}[/] added to index.")
    else:
        console.print(f"[red]✗[/] Hash did not match for [cyan]{key!r}[/] — index not updated.")
        raise typer.Exit(1)


@secrets_app.command("ls", hidden=True)
def secrets_ls() -> None:
    """Alias for 'list'."""
    _secrets_list()


@secrets_app.command("rm", hidden=True)
def secrets_rm(
    key: str = typer.Argument(..., help="Secret key to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Alias for 'delete'."""
    secrets_delete(key=key, yes=yes)


# ── auth ──────────────────────────────────────────────────────────────────────

from auth import has_pin, cmd_set_pin, cmd_clear_pin

auth_app = typer.Typer(
    help="Manage PIN-based access control for atc.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(auth_app, name="auth")


@auth_app.command("set-pin")
def auth_set_pin() -> None:
    """Set or change the PIN required to run atc."""
    cmd_set_pin()


@auth_app.command("clear")
def auth_clear() -> None:
    """Remove the PIN, disabling auth entirely."""
    cmd_clear_pin()


@auth_app.command("status")
def auth_status() -> None:
    """Show whether a PIN is currently configured."""
    if has_pin():
        console.print("[green]●[/]  PIN is [bold]enabled[/]. Prompted once per terminal session.")
    else:
        console.print("[dim]●[/]  No PIN configured. Run [bold]atc auth set-pin[/] to enable.")


# ── unifi ─────────────────────────────────────────────────────────────────────

unifi_app = typer.Typer(
    help="Query the UniFi Network controller",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(unifi_app, name="unifi")


def _unifi_cfg(cfg):
    if cfg.unifi is None:
        console.print("[red]No unifi config in config.yaml[/]")
        raise typer.Exit(1)
    return cfg.unifi


def _print_clients_table(clients: list) -> None:
    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    t.add_column("IP", min_width=15)
    t.add_column("Name", style="bold", min_width=20)
    t.add_column("MAC", style="dim", min_width=17)
    t.add_column("Network", min_width=14)
    t.add_column("Link", min_width=6)

    for c in clients:
        link = "wired" if c.is_wired else "wifi"
        t.add_row(c.ip or "—", c.display_name, c.mac, c.network or "—", link)

    console.print(t)


@unifi_app.command("clients")
def unifi_clients(
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | json"),
) -> None:
    """List currently-connected UniFi clients."""
    cfg = _load()
    unifi_cfg = _unifi_cfg(cfg)

    with console.status("[bold]Fetching UniFi clients…[/]"):
        clients = unifi_fetch_clients(unifi_cfg, site=unifi_cfg.site)

    if output == _Out.json:
        console.print_json(_json.dumps([
            {"ip": c.ip, "mac": c.mac, "name": c.display_name, "network": c.network, "wired": c.is_wired}
            for c in clients
        ]))
        return

    _print_clients_table(clients)
    console.print(f"\n[dim]{len(clients)} connected client(s)[/]")


# ── pihole ────────────────────────────────────────────────────────────────────

pihole_app = typer.Typer(
    help="Manage Pi-hole instances",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(pihole_app, name="pihole")

pihole_dns_app = typer.Typer(
    help="Manage custom local DNS records (primary instance, sync propagates)",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
pihole_app.add_typer(pihole_dns_app, name="dns")



def _pihole_instances(cfg) -> list:
    if not cfg.pihole_instances:
        console.print("[red]No pihole instances configured in config.yaml[/]")
        raise typer.Exit(1)
    return cfg.pihole_instances


def _pihole_primary(cfg):
    instances = _pihole_instances(cfg)
    primary = next((p for p in instances if p.primary), instances[0])
    return primary


def _fmt_blocking(status) -> str:
    if not status.blocking:
        if status.timer is not None:
            return f"[yellow]paused ({status.timer}s remaining)[/]"
        return "[yellow]paused[/]"
    return "[green]blocking[/]"


@pihole_app.command("status")
def pihole_status() -> None:
    """Show status of all Pi-hole instances."""
    cfg = _load()
    instances = _pihole_instances(cfg)
    with console.status("[bold]Querying Pi-hole instances…[/]"):
        statuses = asyncio.run(pihole_fetch_all(instances))

    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    t.add_column("", width=2)
    t.add_column("Instance", style="bold", min_width=14)
    t.add_column("Host", style="dim", min_width=14)
    t.add_column("Blocking", min_width=20)
    t.add_column("Queries", justify="right", min_width=8)
    t.add_column("Blocked", justify="right", min_width=8)
    t.add_column("Block%", justify="right", min_width=6)
    t.add_column("Version", style="dim")

    for s in statuses:
        if s.online:
            dot = "[green]●[/]"
            blocking = _fmt_blocking(s)
            queries = f"{s.queries_today:,}"
            blocked = f"{s.blocked_today:,}"
            pct = f"{s.blocked_pct:.1f}%"
            ver = s.version or "—"
        else:
            dot = "[red]●[/]"
            blocking = f"[red]offline[/]"
            queries = blocked = pct = ver = "[dim]—[/]"
        primary_mark = " [dim](primary)[/]" if getattr(next(
            (i for i in instances if i.name == s.name), None), "primary", False
        ) else ""
        t.add_row(dot, f"{s.name}{primary_mark}", s.host, blocking, queries, blocked, pct, ver)

    online = [s for s in statuses if s.online]
    if online:
        total_q = sum(s.queries_today for s in online)
        total_b = sum(s.blocked_today for s in online)
        avg_pct = sum(s.blocked_pct for s in online) / len(online)
        t.add_section()
        t.add_row(
            "", "[bold]TOTAL[/]", "", "",
            f"[bold]{total_q:,}[/]",
            f"[bold]{total_b:,}[/]",
            f"[bold]{avg_pct:.1f}%[/]",
            "",
        )

    console.print(t)


@pihole_app.command("pause")
def pihole_pause(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Pause DNS blocking indefinitely on ALL instances."""
    cfg = _load()
    instances = _pihole_instances(cfg)

    names = ", ".join(i.name for i in instances)
    console.print(f"Pausing blocking on: [bold]{names}[/]")
    if not yes and not Confirm.ask("Continue?"):
        raise typer.Exit(0)

    with console.status("[bold]Pausing…[/]"):
        results = asyncio.run(set_blocking_all(instances, enabled=False))

    for inst, (ok, err) in zip(instances, results):
        icon = "[green]✓[/]" if ok else "[red]✗[/]"
        msg = "blocking paused" if ok else f"failed — {err}"
        console.print(f"{icon} {inst.name} — {msg}")


@pihole_app.command("unpause")
def pihole_unpause(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Re-enable DNS blocking on ALL instances."""
    cfg = _load()
    instances = _pihole_instances(cfg)

    names = ", ".join(i.name for i in instances)
    console.print(f"Enabling blocking on: [bold]{names}[/]")
    if not yes and not Confirm.ask("Continue?"):
        raise typer.Exit(0)

    with console.status("[bold]Enabling…[/]"):
        results = asyncio.run(set_blocking_all(instances, enabled=True))

    for inst, (ok, err) in zip(instances, results):
        icon = "[green]✓[/]" if ok else "[red]✗[/]"
        msg = "blocking enabled" if ok else f"failed — {err}"
        console.print(f"{icon} {inst.name} — {msg}")


@pihole_dns_app.command("list")
def pihole_dns_list(
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """List custom local DNS records (reads from primary instance)."""
    if json_flag:
        output = _Out.json
    cfg = _load()
    primary = _pihole_primary(cfg)

    try:
        with console.status(f"[bold]Fetching DNS records from {primary.name}…[/]"):
            records = asyncio.run(list_custom_dns(primary))
    except Exception as e:
        console.print(f"[red]Error fetching DNS records from {primary.name}:[/] {e}")
        raise typer.Exit(1)

    if output == _Out.json:
        console.print_json(_json.dumps(records))
        return

    if not records:
        console.print(f"[dim]No custom DNS records on {primary.name}.[/]")
        return

    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    t.add_column("Domain", style="bold", min_width=30)
    t.add_column("IP", min_width=15)

    for r in sorted(records, key=lambda x: x.get("domain", "")):
        t.add_row(r.get("domain", "?"), r.get("ip", "?"))

    console.print(t)
    console.print(f"\n[dim]{len(records)} record(s) — source: {primary.name}[/]")


@pihole_dns_app.command("add")
def pihole_dns_add(
    domain: str = typer.Argument(..., help="Fully-qualified domain name, e.g. myhost.local"),
    ip: str = typer.Argument(..., help="IP address to resolve to"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Add a custom local DNS record (primary instance; sync propagates)."""
    cfg = _load()
    primary = _pihole_primary(cfg)

    console.print(f"Adding DNS: [bold]{domain}[/] → [cyan]{ip}[/] on [bold]{primary.name}[/]")
    if not yes and not Confirm.ask("Continue?"):
        raise typer.Exit(0)

    with console.status("[bold]Adding record…[/]"):
        ok, err = asyncio.run(add_custom_dns(primary, domain, ip))

    if ok:
        console.print(f"[green]✓[/] {domain} → {ip} added.")
    else:
        console.print(f"[red]✗[/] Failed — {err}")
        raise typer.Exit(1)


@pihole_dns_app.command("remove")
def pihole_dns_remove(
    domain: str = typer.Argument(..., help="Domain to remove"),
    ip: str = typer.Argument(..., help="IP address of the record to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Remove a custom local DNS record (primary instance)."""
    cfg = _load()
    primary = _pihole_primary(cfg)

    console.print(f"Removing DNS: [bold]{domain}[/] → [cyan]{ip}[/] from [bold]{primary.name}[/]")
    if not yes and not Confirm.ask("Continue?"):
        raise typer.Exit(0)

    with console.status("[bold]Removing record…[/]"):
        ok, err = asyncio.run(remove_custom_dns(primary, domain, ip))

    if ok:
        console.print(f"[green]✓[/] {domain} removed.")
    else:
        console.print(f"[red]✗[/] Failed — {err}")
        raise typer.Exit(1)


@pihole_app.command("gravity")
def pihole_gravity(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Trigger a gravity (blocklist) update on ALL instances."""
    cfg = _load()
    instances = _pihole_instances(cfg)

    names = ", ".join(i.name for i in instances)
    console.print(f"Running gravity update on: [bold]{names}[/]")
    if not yes and not Confirm.ask("Continue?"):
        raise typer.Exit(0)

    with console.status("[bold]Triggering gravity update…[/]"):
        results = asyncio.run(trigger_gravity_all(instances))

    for inst, (ok, err) in zip(instances, results):
        icon = "[green]✓[/]" if ok else "[red]✗[/]"
        msg = "gravity update triggered" if ok else f"failed — {err}"
        console.print(f"{icon} {inst.name} — {msg}")

    console.print("\n[dim]Updates run in the background on each instance.[/]")


# ── nginx proxy manager ─────────────────────────────────────────────────────

npm_app = typer.Typer(
    help="Manage Nginx Proxy Manager (NPM) reverse-proxy hosts",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(npm_app, name="npm")


def _npm_cfg(cfg):
    if not cfg.nginx_proxy_manager:
        console.print("[red]nginx_proxy_manager not configured in config.yaml[/]")
        raise typer.Exit(1)
    return cfg.nginx_proxy_manager


@npm_app.command("list")
def npm_list(
    output: str = typer.Option("table", "-o", "--output", help="table | json"),
) -> None:
    """List all configured proxy hosts."""
    cfg = _load()
    npm_cfg = _npm_cfg(cfg)
    hosts = npm_list_proxy_hosts(npm_cfg)

    if output == "json":
        console.print_json(_json.dumps([
            {
                "id": h.id, "domains": h.domain_names, "forward": f"{h.forward_scheme}://{h.forward_host}:{h.forward_port}",
                "enabled": h.enabled, "ssl_forced": h.ssl_forced, "certificate_id": h.certificate_id,
            }
            for h in hosts
        ]))
        return

    t = Table(box=box.SIMPLE_HEAVY)
    t.add_column("ID", justify="right")
    t.add_column("Domain(s)", style="bold")
    t.add_column("Forward")
    t.add_column("SSL")
    t.add_column("Enabled")
    for h in sorted(hosts, key=lambda x: x.primary_domain):
        ssl = "forced" if h.ssl_forced else ("cert" if h.certificate_id else "—")
        t.add_row(
            str(h.id),
            ", ".join(h.domain_names),
            f"{h.forward_scheme}://{h.forward_host}:{h.forward_port}",
            ssl,
            "[green]yes[/]" if h.enabled else "[red]no[/]",
        )
    console.print(t)
    console.print(f"\n[dim]{len(hosts)} proxy host(s)[/]")


def _resolve_certificate_id(
    npm_cfg,
    certificate_id: Optional[int],
    certificate_name: Optional[str],
) -> int:
    """Resolve a --certificate-id / --certificate-name pair to a concrete NPM
    certificate id, or 0 (no cert) if neither was given.

    certificate_name matches against each cert's nice_name or any of its
    domain_names (exact or wildcard, e.g. "local.damyanpetkov.com" matches
    a "*.local.damyanpetkov.com" cert). Prints the known cert list and exits
    on no-match or ambiguous-match, so the caller never silently gets 0.
    """
    if certificate_id is not None and certificate_name is not None:
        console.print("[red]✗[/] Pass only one of --certificate-id / --certificate-name, not both.")
        raise typer.Exit(1)
    if certificate_id is not None:
        return certificate_id
    if certificate_name is None:
        return 0

    certs = npm_list_certificates(npm_cfg)

    def _matches(c) -> bool:
        if certificate_name == c.nice_name:
            return True
        for d in c.domain_names:
            if d == certificate_name:
                return True
            if d.startswith("*.") and certificate_name.endswith(d[1:]):
                return True
        return False

    matches = [c for c in certs if _matches(c)]
    if not matches:
        console.print(f"[red]✗[/] No certificate matches '{certificate_name}'. Known certificates:")
        for c in certs:
            console.print(f"  #{c.id}  {c.nice_name}  ({', '.join(c.domain_names)})")
        raise typer.Exit(1)
    if len(matches) > 1:
        console.print(f"[red]✗[/] '{certificate_name}' matches multiple certificates — use --certificate-id instead:")
        for c in matches:
            console.print(f"  #{c.id}  {c.nice_name}  ({', '.join(c.domain_names)})")
        raise typer.Exit(1)
    return matches[0].id


@npm_app.command("create")
def npm_create(
    domain: str = typer.Argument(..., help="Domain name, e.g. myservice.local.damyanpetkov.com"),
    forward_host: str = typer.Argument(..., help="IP or hostname to forward to"),
    forward_port: int = typer.Argument(..., help="Port to forward to"),
    scheme: str = typer.Option("http", "--scheme", help="http or https"),
    websocket: bool = typer.Option(True, "--websocket/--no-websocket", help="Allow websocket upgrade"),
    block_exploits: bool = typer.Option(True, "--block-exploits/--no-block-exploits"),
    certificate_id: Optional[int] = typer.Option(
        None, "--certificate-id", help="Existing NPM certificate id to attach (see 'atc npm certs'). 0/omitted = no cert.",
    ),
    certificate_name: Optional[str] = typer.Option(
        None, "--certificate-name",
        help="Attach a cert by name/domain instead of id, e.g. '*.local.damyanpetkov.com' (see 'atc npm certs'). Errors out on no/ambiguous match rather than silently leaving the host cert-less.",
    ),
    ssl_forced: bool = typer.Option(
        False, "--ssl-forced/--no-ssl-forced", help="Force SSL (requires a certificate to actually be attached).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Create a new proxy host. Fails loudly if the domain already exists — use 'update' instead."""
    cfg = _load()
    npm_cfg = _npm_cfg(cfg)

    existing = npm_find_proxy_host(npm_cfg, domain)
    if existing:
        console.print(f"[red]✗[/] {domain} already exists as proxy host #{existing.id}. Use 'atc npm update' instead.")
        raise typer.Exit(1)

    resolved_cert_id = _resolve_certificate_id(npm_cfg, certificate_id, certificate_name)
    if ssl_forced and not resolved_cert_id:
        console.print("[red]✗[/] --ssl-forced requires a certificate — pass --certificate-id or --certificate-name (see 'atc npm certs').")
        raise typer.Exit(1)

    console.print(f"Creating proxy host: [bold]{domain}[/] → [cyan]{scheme}://{forward_host}:{forward_port}[/]")
    if resolved_cert_id:
        console.print(f"  SSL: certificate #{resolved_cert_id}{' (forced)' if ssl_forced else ''}")
    else:
        console.print("  SSL: [yellow]none — plain HTTP, no cert attached[/]")
    if not yes and not Confirm.ask("Continue?"):
        raise typer.Exit(0)

    with console.status("[bold]Creating…[/]"):
        host = npm_create_proxy_host(
            npm_cfg, domain, forward_host, forward_port,
            forward_scheme=scheme, block_exploits=block_exploits, allow_websocket_upgrade=websocket,
            certificate_id=resolved_cert_id, ssl_forced=ssl_forced,
        )
    console.print(f"[green]✓[/] Created proxy host #{host.id}: {domain}")


@npm_app.command("update")
def npm_update(
    domain: str = typer.Argument(..., help="Domain of the existing proxy host to update"),
    forward_host: Optional[str] = typer.Option(None, "--forward-host", help="New forward host/IP"),
    forward_port: Optional[int] = typer.Option(None, "--forward-port", help="New forward port"),
    scheme: Optional[str] = typer.Option(None, "--scheme", help="New forward scheme (http/https)"),
    certificate_id: Optional[int] = typer.Option(
        None, "--certificate-id", help="Attach this existing NPM certificate id (see 'atc npm certs'). Pass 0 to detach.",
    ),
    certificate_name: Optional[str] = typer.Option(
        None, "--certificate-name",
        help="Attach a cert by name/domain instead of id, e.g. '*.local.damyanpetkov.com' (see 'atc npm certs').",
    ),
    ssl_forced: Optional[bool] = typer.Option(None, "--ssl-forced/--no-ssl-forced", help="Force SSL on/off"),
    websocket: Optional[bool] = typer.Option(None, "--websocket/--no-websocket"),
    block_exploits: Optional[bool] = typer.Option(None, "--block-exploits/--no-block-exploits"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Update an existing proxy host (forward target, cert, SSL enforcement, etc).

    Common fix: a host created without a cert (see 'SSL: —' in 'atc npm list')
    is silently served over plain HTTP or falls through to the backend's own
    cert — attach the right one here, e.g.:
      atc npm update mail.local.damyanpetkov.com --certificate-name '*.local.damyanpetkov.com' --ssl-forced
    """
    cfg = _load()
    npm_cfg = _npm_cfg(cfg)

    host = npm_find_proxy_host(npm_cfg, domain)
    if not host:
        console.print(f"[red]✗[/] No proxy host found for {domain}. Use 'atc npm create' instead.")
        raise typer.Exit(1)

    fields: dict = {}
    if forward_host is not None:
        fields["forward_host"] = forward_host
    if forward_port is not None:
        fields["forward_port"] = forward_port
    if scheme is not None:
        fields["forward_scheme"] = scheme
    if websocket is not None:
        fields["allow_websocket_upgrade"] = websocket
    if block_exploits is not None:
        fields["block_exploits"] = block_exploits

    if certificate_id is not None or certificate_name is not None:
        resolved_cert_id = _resolve_certificate_id(npm_cfg, certificate_id, certificate_name)
        fields["certificate_id"] = resolved_cert_id
        fields["http2_support"] = bool(resolved_cert_id)
    if ssl_forced is not None:
        fields["ssl_forced"] = ssl_forced

    if not fields:
        console.print("[yellow]Nothing to update — pass at least one option.[/]")
        raise typer.Exit(0)

    effective_cert_id = fields.get("certificate_id", host.certificate_id)
    if fields.get("ssl_forced", host.ssl_forced) and not effective_cert_id:
        console.print("[red]✗[/] --ssl-forced requires a certificate — pass --certificate-id/--certificate-name too.")
        raise typer.Exit(1)

    console.print(f"Updating proxy host #{host.id}: [bold]{domain}[/]")
    for k, v in fields.items():
        console.print(f"  {k}: {v}")
    if not yes and not Confirm.ask("Continue?"):
        raise typer.Exit(0)

    with console.status("[bold]Updating…[/]"):
        updated = npm_update_proxy_host(npm_cfg, host.id, fields)
    console.print(f"[green]✓[/] Updated proxy host #{updated.id}: {domain}")


@npm_app.command("delete")
def npm_delete(
    domain: str = typer.Argument(..., help="Domain of the proxy host to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a proxy host by domain."""
    cfg = _load()
    npm_cfg = _npm_cfg(cfg)

    host = npm_find_proxy_host(npm_cfg, domain)
    if not host:
        console.print(f"[red]✗[/] No proxy host found for {domain}")
        raise typer.Exit(1)

    console.print(f"Deleting proxy host #{host.id}: [bold]{domain}[/] → {host.forward_scheme}://{host.forward_host}:{host.forward_port}")
    if not yes and not Confirm.ask("Continue?"):
        raise typer.Exit(0)

    npm_delete_proxy_host(npm_cfg, host.id)
    console.print(f"[green]✓[/] Deleted.")


@npm_app.command("certs")
def npm_certs(
    output: str = typer.Option("table", "-o", "--output", help="table | json"),
) -> None:
    """List SSL certificates known to NPM."""
    cfg = _load()
    npm_cfg = _npm_cfg(cfg)
    certs = npm_list_certificates(npm_cfg)

    if output == "json":
        console.print_json(_json.dumps([
            {"id": c.id, "name": c.nice_name, "domains": c.domain_names, "provider": c.provider, "expires": c.expires_on}
            for c in certs
        ]))
        return

    t = Table(box=box.SIMPLE_HEAVY)
    t.add_column("ID", justify="right")
    t.add_column("Name", style="bold")
    t.add_column("Domain(s)")
    t.add_column("Provider")
    t.add_column("Expires")
    for c in certs:
        t.add_row(str(c.id), c.nice_name, ", ".join(c.domain_names), c.provider, c.expires_on or "—")
    console.print(t)


# ── backup freshness reporting (writes to InfluxDB for Grafana) ───────────────
# Read-only against Proxmox — Proxmox owns the actual backup schedule (e.g.
# the weekly Sunday 04:00 vzdump job, see 'atc backup list' above). This just
# snapshots freshness/size into InfluxDB so Grafana can show staleness trends
# and catch things like a VM silently falling out of the backup rotation.


@backup_app.command("snapshot")
def backup_snapshot() -> None:
    """Compute per-VM backup freshness and write it to InfluxDB for Grafana.

    Read-only against Proxmox (just lists existing backup files) — writes
    only to the 'proxmox' InfluxDB bucket, measurement 'backup_freshness'.
    Run this manually whenever you want the dashboard refreshed (no cron
    wired up by default).
    """
    cfg = _load()
    cluster = asyncio.run(fetch_cluster_status(cfg.proxmox))
    online = [n.name for n in cluster.nodes if n.online]
    backups = list_backups(cfg.proxmox, online)

    latest: dict[int, BackupFile] = {}
    for b in backups:
        if b.vmid not in latest or b.ctime > latest[b.vmid].ctime:
            latest[b.vmid] = b

    guest_names = {g.vmid: g.name for g in cluster.guests}
    now = datetime.now(timezone.utc).timestamp()

    points = []
    for vmid, b in latest.items():
        age_hours = (now - b.ctime) / 3600
        points.append({
            "tags": {
                "vmid": str(vmid),
                "name": guest_names.get(vmid, b.notes) or str(vmid),
                "storage": b.storage,
                "node": b.node,
            },
            "fields": {
                "age_hours": round(age_hours, 2),
                "size_bytes": int(b.size),
                "last_backup_epoch": int(b.ctime),
            },
        })

    if not cfg.influxdb:
        console.print("[red]✗[/] influxdb not configured in config.yaml")
        raise typer.Exit(1)

    with console.status(f"[bold]Writing {len(points)} backup-freshness points to InfluxDB…[/]"):
        influx_write_points(cfg.influxdb, "proxmox", "backup_freshness", points)

    console.print(f"[green]✓[/] Wrote freshness data for {len(points)} VM(s)/CT(s) to InfluxDB.")
    stale = [p for p in points if p["fields"]["age_hours"] > 24 * 8]
    if stale:
        console.print(f"[yellow]⚠[/]  {len(stale)} VM(s) with backups older than 8 days:")
        for p in stale:
            console.print(f"    VMID {p['tags']['vmid']} ({p['tags']['name']}): {p['fields']['age_hours']/24:.1f}d ago")


# ── service publish (DNS + NPM combined) ─────────────────────────────────────

service_app = typer.Typer(
    help="Cross-cutting service deployment helpers (DNS + reverse proxy in one step)",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(service_app, name="service")


@service_app.command("publish")
def service_publish(
    domain: str = typer.Argument(..., help="Local domain, e.g. myservice.local.damyanpetkov.com"),
    forward_host: str = typer.Argument(..., help="IP the service actually runs on, e.g. 192.168.1.78"),
    forward_port: int = typer.Argument(..., help="Port the service listens on"),
    dns_ip: Optional[str] = typer.Option(
        None, "--dns-ip",
        help="IP to point the DNS record at. Defaults to forward_host — pass explicitly if NPM and the DNS target differ (e.g. NPM host IP vs. a Docker-internal service IP).",
    ),
    scheme: str = typer.Option("http", "--scheme", help="http or https, for the NPM forward"),
    websocket: bool = typer.Option(True, "--websocket/--no-websocket"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """One-shot: add the local DNS record (Pi-hole) AND the reverse-proxy host (NPM) for a new service.

    This is the two manual steps (pihole dns add + npm create) collapsed into
    one, for the common case of standing up a new internal service with its
    own subdomain. Runs DNS first, then NPM — if NPM fails, the DNS record
    is left in place (rerun 'atc npm create' once the issue is fixed rather
    than re-running this whole command, to avoid a duplicate DNS attempt).
    """
    cfg = _load()
    npm_cfg = _npm_cfg(cfg)
    primary_pihole = _pihole_primary(cfg)
    resolved_dns_ip = dns_ip or forward_host

    console.print(f"[bold]Publishing service:[/] {domain}")
    console.print(f"  DNS:   {domain} → {resolved_dns_ip}  (on {primary_pihole.name})")
    console.print(f"  Proxy: {domain} → {scheme}://{forward_host}:{forward_port}  (NPM)")
    if not yes and not Confirm.ask("Continue?"):
        raise typer.Exit(0)

    existing_host = npm_find_proxy_host(npm_cfg, domain)
    if existing_host:
        console.print(f"[red]✗[/] {domain} already exists as NPM proxy host #{existing_host.id} — aborting before touching DNS. Use 'atc npm update' for changes.")
        raise typer.Exit(1)

    with console.status("[bold]Adding DNS record…[/]"):
        dns_ok, dns_err = asyncio.run(add_custom_dns(primary_pihole, domain, resolved_dns_ip))
    if not dns_ok:
        console.print(f"[red]✗[/] DNS record failed — {dns_err}")
        raise typer.Exit(1)
    console.print(f"[green]✓[/] DNS record added.")

    with console.status("[bold]Creating NPM proxy host…[/]"):
        try:
            host = npm_create_proxy_host(
                npm_cfg, domain, forward_host, forward_port,
                forward_scheme=scheme, allow_websocket_upgrade=websocket,
            )
        except Exception as e:
            console.print(f"[red]✗[/] NPM proxy host failed — {e}")
            console.print(f"[yellow]![/] DNS record for {domain} was already added and was NOT rolled back. "
                          f"Fix the NPM issue and run 'atc npm create {domain} {forward_host} {forward_port}' "
                          f"directly rather than re-running publish (it would try to re-add the same DNS entry).")
            raise typer.Exit(1)

    console.print(f"[green]✓[/] NPM proxy host #{host.id} created.")
    console.print(f"\n[bold green]{domain}[/] is live — https://{domain} once DNS propagates (Pi-hole is usually instant on your LAN).")


@pihole_app.command("audit")
def pihole_audit(
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | json"),
) -> None:
    """Flag connected UniFi clients that Pi-hole has never seen a query from.

    Cross-checks the live UniFi client list against every Pi-hole instance's
    network table (devices that have ever queried it). Anything connected to
    the network but missing from Pi-hole's history is likely using a
    different DNS server — misconfigured DHCP, a hardcoded resolver, or DoH.
    """
    cfg = _load()
    instances = _pihole_instances(cfg)
    unifi_cfg = _unifi_cfg(cfg)

    with console.status("[bold]Fetching UniFi clients and Pi-hole query history…[/]"):
        clients = [c for c in unifi_fetch_clients(unifi_cfg, site=unifi_cfg.site) if c.ip]
        seen_ips, errors = asyncio.run(fetch_seen_ips_all(instances))

    for name, err in errors:
        console.print(f"[yellow]⚠[/]  {name}: could not fetch query history — {err}")

    bypassing = [c for c in clients if c.ip not in seen_ips]

    if output == _Out.json:
        console.print_json(_json.dumps([
            {"ip": c.ip, "mac": c.mac, "name": c.display_name, "network": c.network, "wired": c.is_wired}
            for c in bypassing
        ]))
        return

    if not bypassing:
        console.print(f"\n[green]✓[/] All {len(clients)} connected client(s) have been seen by Pi-hole.")
        return

    _print_clients_table(sorted(bypassing, key=lambda x: x.ip))
    console.print(
        f"\n[yellow]{len(bypassing)}[/] of [bold]{len(clients)}[/] connected client(s) "
        f"have not been seen by any Pi-hole instance — check their DNS settings."
    )


# ── ha ────────────────────────────────────────────────────────────────────────

ha_app = typer.Typer(
    help="Manage Home Assistant",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(ha_app, name="ha")


def _ha(cfg) -> object:
    if cfg.ha is None:
        console.print("[red]No homeassistant section in config.yaml[/]")
        raise typer.Exit(1)
    return cfg.ha


@ha_app.command("status")
def ha_status() -> None:
    """Show Home Assistant version and state."""
    cfg = _load()
    ha_cfg = _ha(cfg)
    with console.status("[bold]Querying Home Assistant…[/]"):
        s = ha_get_status(ha_cfg)

    if s.online:
        state_color = "green" if s.state == "RUNNING" else "yellow"
        console.print(f"[green]●[/] Home Assistant [bold]{s.version}[/]")
        console.print(f"  State:    [{state_color}]{s.state}[/]")
        if s.location:
            console.print(f"  Location: {s.location}")
        console.print(f"  URL:      [dim]{ha_cfg.base_url}[/]")
    else:
        console.print(f"[red]●[/] Home Assistant — offline")
        console.print(f"  [dim]{s.error}[/]")
        raise typer.Exit(1)


@ha_app.command("get")
def ha_get(
    entity_id: str = typer.Argument(..., help="Entity ID, e.g. sensor.loft_temperature"),
) -> None:
    """Get the current state and attributes of an entity."""
    cfg = _load()
    ha_cfg = _ha(cfg)
    try:
        s = get_entity_state(ha_cfg, entity_id)
    except Exception as e:
        console.print(f"[red]✗[/] {e}")
        raise typer.Exit(1)

    unit = s.attributes.get("unit_of_measurement", "")
    friendly = s.attributes.get("friendly_name", "")
    state_str = f"{s.state}{(' ' + unit) if unit else ''}"

    console.print(f"[bold]{entity_id}[/]" + (f"  [dim]{friendly}[/]" if friendly else ""))
    console.print(f"  State:   [cyan]{state_str}[/]")

    skip = {"unit_of_measurement", "friendly_name"}
    attrs = {k: v for k, v in s.attributes.items() if k not in skip}
    if attrs:
        console.print("  Attributes:")
        for k, v in attrs.items():
            console.print(f"    [dim]{k}:[/] {v}")

    if s.last_changed:
        console.print(f"  Changed: [dim]{s.last_changed[:19].replace('T', ' ')}[/]")


@ha_app.command("turn-on")
def ha_turn_on(
    entity_id: str = typer.Argument(..., help="Entity ID to turn on"),
) -> None:
    """Turn on an entity (switch, light, fan, etc.)."""
    cfg = _load()
    ha_cfg = _ha(cfg)
    try:
        ha_call_service(ha_cfg, "homeassistant", "turn_on", {"entity_id": entity_id})
        console.print(f"[green]✓[/] Turned on [bold]{entity_id}[/]")
    except Exception as e:
        console.print(f"[red]✗[/] {e}")
        raise typer.Exit(1)


@ha_app.command("turn-off")
def ha_turn_off(
    entity_id: str = typer.Argument(..., help="Entity ID to turn off"),
) -> None:
    """Turn off an entity (switch, light, fan, etc.)."""
    cfg = _load()
    ha_cfg = _ha(cfg)
    try:
        ha_call_service(ha_cfg, "homeassistant", "turn_off", {"entity_id": entity_id})
        console.print(f"[green]✓[/] Turned off [bold]{entity_id}[/]")
    except Exception as e:
        console.print(f"[red]✗[/] {e}")
        raise typer.Exit(1)


@ha_app.command("call")
def ha_call(
    service: str = typer.Argument(..., help="domain.service, e.g. light.turn_on"),
    entity_id: Optional[str] = typer.Argument(None, help="Entity ID (optional)"),
    data: Optional[str] = typer.Option(None, "--data", "-d", help='Extra JSON data, e.g. \'{"brightness": 128}\''),
) -> None:
    """Call a Home Assistant service."""
    cfg = _load()
    ha_cfg = _ha(cfg)

    if "." not in service:
        console.print("[red]Service must be in domain.service format, e.g. light.turn_on[/]")
        raise typer.Exit(1)

    domain, svc = service.split(".", 1)
    payload: dict = {}
    if entity_id:
        payload["entity_id"] = entity_id
    if data:
        try:
            payload.update(_json.loads(data))
        except _json.JSONDecodeError as e:
            console.print(f"[red]Invalid JSON in --data: {e}[/]")
            raise typer.Exit(1)

    try:
        changed = ha_call_service(ha_cfg, domain, svc, payload)
        console.print(f"[green]✓[/] Called [bold]{service}[/]" + (f" on {entity_id}" if entity_id else ""))
        if changed:
            console.print(f"  [dim]{len(changed)} state(s) affected[/]")
    except Exception as e:
        console.print(f"[red]✗[/] {e}")
        raise typer.Exit(1)


@ha_app.command("reload")
def ha_reload(
    domain: Optional[str] = typer.Argument(
        None,
        help="Domain to reload (automation, script, template, …). Omit for all.",
    ),
) -> None:
    """Reload YAML configuration — all or a specific domain."""
    cfg = _load()
    ha_cfg = _ha(cfg)

    if domain:
        svc_domain, svc_name = domain, "reload"
        label = f"{domain} YAML"
    else:
        svc_domain, svc_name = "homeassistant", "reload_all"
        label = "all YAML configuration"

    try:
        ha_call_service(ha_cfg, svc_domain, svc_name)
        console.print(f"[green]✓[/] Reloaded {label}.")
    except Exception as e:
        console.print(f"[red]✗[/] {e}")
        raise typer.Exit(1)


@ha_app.command("stats")
def ha_stats() -> None:
    """Show configured sensor stats (power, temperature, humidity, etc.)."""
    cfg = _load()
    ha_cfg = _ha(cfg)

    if not ha_cfg.stats:
        console.print("[yellow]No stats configured. Add a 'stats' list under 'homeassistant' in config.yaml.[/]")
        raise typer.Exit(1)

    with console.status("[bold]Fetching stats…[/]"):
        results = ha_get_stats(ha_cfg)

    t = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    t.add_column("Name", style="bold", min_width=20)
    t.add_column("Value", min_width=12)
    t.add_column("Entity", style="dim")

    for r in results:
        t.add_row(r.cfg.name, r.display, r.cfg.entity)

    console.print(t)


@ha_app.command("restart")
def ha_restart(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Restart the Home Assistant core."""
    cfg = _load()
    ha_cfg = _ha(cfg)

    console.print("[yellow]⚠[/]  This will restart Home Assistant.")
    if not yes and not Confirm.ask("Restart Home Assistant core?"):
        raise typer.Exit(0)

    try:
        ha_call_service(ha_cfg, "homeassistant", "restart")
        console.print("[green]✓[/] Restart initiated.")
    except Exception as e:
        console.print(f"[red]✗[/] {e}")
        raise typer.Exit(1)


@ha_app.command("updates")
def ha_updates() -> None:
    """List pending updates (core, supervisor, OS, HACS, integrations, add-ons)."""
    cfg = _load()
    ha_cfg = _ha(cfg)
    with console.status("[bold]Fetching update entities…[/]"):
        try:
            updates = ha_list_updates(ha_cfg, pending_only=True)
        except Exception as e:
            console.print(f"[red]✗[/] {e}")
            raise typer.Exit(1)

    if not updates:
        console.print("[green]✓[/] All up to date.")
        return

    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    t.add_column("Category", style="dim", min_width=12)
    t.add_column("Name", style="bold", min_width=24)
    t.add_column("Installed", justify="right", min_width=10)
    t.add_column("Available", justify="right", style="yellow", min_width=10)
    t.add_column("Entity ID", style="dim")

    for u in sorted(updates, key=lambda x: (x.short_category, x.title)):
        t.add_row(u.short_category, u.title, u.installed_version or "—", u.latest_version or "—", u.entity_id)

    console.print(t)
    console.print(f"[dim]{len(updates)} update(s) available. Run [bold]atc ha update --all[/] to install all.[/]")


@ha_app.command("update")
def ha_update(
    entity_id: Optional[str] = typer.Argument(None, help="Specific update entity ID (e.g. update.home_assistant_core_update)"),
    all_: bool = typer.Option(False, "--all", help="Install all pending updates"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Install a specific update or all pending updates."""
    cfg = _load()
    ha_cfg = _ha(cfg)

    if not entity_id and not all_:
        console.print("[yellow]Specify an entity ID or pass --all.[/]  Run [bold]atc ha updates[/] to list pending.")
        raise typer.Exit(1)

    with console.status("[bold]Fetching update entities…[/]"):
        try:
            pending = ha_list_updates(ha_cfg, pending_only=True)
        except Exception as e:
            console.print(f"[red]✗[/] {e}")
            raise typer.Exit(1)

    if all_:
        targets = pending
    else:
        targets = [u for u in pending if u.entity_id == entity_id]
        if not targets:
            # Maybe entity exists but is already up to date — check explicitly
            console.print(f"[yellow]No pending update found for[/] [bold]{entity_id}[/]")
            raise typer.Exit(1)

    if not targets:
        console.print("[green]✓[/] Nothing to update.")
        return

    not_installable = [u for u in targets if not u.supports_install]
    if not_installable:
        targets = [u for u in targets if u.supports_install]
        for u in not_installable:
            console.print(f"[dim]ℹ  {u.title} — no API install support, skipped. Use the HA UI.[/]")
        if not targets:
            raise typer.Exit(0)

    console.print(f"Installing [bold]{len(targets)}[/] update(s):")
    for u in targets:
        console.print(f"  [dim]•[/] [bold]{u.title}[/]  {u.installed_version} → [yellow]{u.latest_version}[/]")

    if not yes and not Confirm.ask("Proceed?"):
        raise typer.Exit(0)

    errors = []
    for u in targets:
        try:
            ha_call_service(ha_cfg, "update", "install", {"entity_id": u.entity_id})
            console.print(f"[green]✓[/] {u.title}")
        except Exception as e:
            console.print(f"[red]✗[/] {u.title}: {e}")
            errors.append(u.entity_id)

    if errors:
        raise typer.Exit(1)


# ── certs ─────────────────────────────────────────────────────────────────────

certs_app = typer.Typer(
    help="Show TLS certificate expiry, or deploy a fresh cert to Pi-hole instances.",
    no_args_is_help=False,
    rich_markup_mode="rich",
)
app.add_typer(certs_app, name="certs")


def _certs_check() -> None:
    cfg = _load()
    if not cfg.tls_checks:
        console.print("[yellow]No TLS checks configured. Add a 'tls:' section to config.yaml.[/]")
        raise typer.Exit(0)

    with console.status("[bold]Checking certificates…[/]"):
        results: list[CertStatus] = asyncio.run(check_all_certs(cfg.tls_checks))

    t = Table(box=box.SIMPLE, pad_edge=False)
    t.add_column("Name", style="cyan", no_wrap=True)
    t.add_column("Host:Port", style="dim")
    t.add_column("Subject")
    t.add_column("Expires")
    t.add_column("Days Left", justify="right")

    for r in results:
        host_port = f"{r.host}:{r.port}"
        if not r.ok:
            t.add_row(r.name, host_port, "—", "—", f"[red]ERR: {r.error[:35]}[/]")
            continue
        expiry_str = r.expiry.strftime("%Y-%m-%d") if r.expiry else "—"
        if r.days_left <= 14:
            days_str = f"[red bold]{r.days_left}d ⚠[/]"
        elif r.days_left <= 30:
            days_str = f"[yellow]{r.days_left}d[/]"
        else:
            days_str = f"[green]{r.days_left}d[/]"
        t.add_row(r.name, host_port, r.subject, expiry_str, days_str)

    console.print(t)


@certs_app.callback(invoke_without_command=True)
def certs_default(ctx: typer.Context) -> None:
    """Show TLS certificate expiry for all configured endpoints."""
    if ctx.invoked_subcommand is None:
        _certs_check()


@certs_app.command("check")
def certs_check() -> None:
    """Show TLS certificate expiry for all configured endpoints. (same as bare 'atc certs')"""
    _certs_check()


@certs_app.command("deploy")
def certs_deploy(
    target: Optional[str] = typer.Argument(
        None, help="Target name (substring, e.g. 'globemaster', 'viggen'). Omit for all targets."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Deploy the current nginx-proxy-manager cert to Pi-hole instances.

    Fetches fullchain+privkey from Docker-01 (nginx-proxy-manager) and
    installs it on Globemaster (Synology) and the Viggen/Draken LXCs,
    backing up each existing cert first and restarting pihole-FTL after.
    Mirrors infrastructure/pihole/certs/deploy-pihole-tls.sh, but callable
    directly (no interactive script, scriptable with -y, JSON-friendly errors).
    """
    from lib.tls_deploy import ALL_TARGETS, fetch_combined_pem, deploy_target

    targets = ALL_TARGETS
    if target:
        tf = target.lower()
        targets = [t for t in ALL_TARGETS if tf in t.name.lower()]
        if not targets:
            names = ", ".join(t.name for t in ALL_TARGETS)
            console.print(f"[red]No target matching '{target}'. Available: {names}[/]")
            raise typer.Exit(1)

    console.print("[bold]Cert deploy targets:[/]")
    for t in targets:
        console.print(f"  [cyan]•[/] {t.name} [dim]({t.host})[/]")

    if not yes and not Confirm.ask(f"\nDeploy cert to {len(targets)} target(s)?"):
        raise typer.Exit(0)

    async def _run() -> int:
        console.print("\n[bold]Fetching cert from Docker-01…[/]")
        pem = await fetch_combined_pem(lambda line: console.print(line))

        failed = 0
        for t in targets:
            console.print(f"\n[bold]{t.name}[/]")
            ok = await deploy_target(t, pem, lambda line: console.print(line))
            if ok:
                console.print(f"  [green]✓[/] {t.name} done")
            else:
                console.print(f"  [red]✗[/] {t.name} failed")
                failed += 1
        return failed

    failed = asyncio.run(_run())
    console.print()
    if failed:
        console.print(f"[red]{failed}/{len(targets)} target(s) failed.[/]")
        raise typer.Exit(1)
    console.print(f"[green]✓[/] Cert deployed to {len(targets)} target(s).")


# ── metrics (Prometheus) ─────────────────────────────────────────────────────

metrics_app = typer.Typer(
    help="Run ad-hoc PromQL queries against Prometheus.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(metrics_app, name="metrics")


def _require_prometheus(cfg: Config):
    if not cfg.prometheus:
        console.print("[yellow]No Prometheus configured. Add a 'prometheus:' section to config.yaml.[/]")
        raise typer.Exit(0)
    return cfg.prometheus


@metrics_app.command("query")
def metrics_query(
    promql: str = typer.Argument(..., help="PromQL expression, e.g. 'up' or 'node_load1'"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """Instant PromQL query — current value(s) of an expression."""
    if json_flag:
        output = _Out.json
    cfg = _load()
    pcfg = _require_prometheus(cfg)

    try:
        with console.status("[bold]Querying Prometheus…[/]"):
            results = prom_query(pcfg, promql)
    except Exception as e:
        console.print(f"[red]❌  {e}[/]")
        raise typer.Exit(1)

    if not results:
        console.print("[dim](no results)[/]")
        return

    if output == _Out.json:
        console.print_json(_json.dumps([
            {"metric": r.metric, "value": r.value, "timestamp": r.timestamp}
            for r in results
        ]))
        return

    t = Table(box=box.SIMPLE, pad_edge=False)
    t.add_column("Labels", style="cyan")
    t.add_column("Value", justify="right")
    for r in results:
        labels = ", ".join(f"{k}={v}" for k, v in r.metric.items() if k != "__name__")
        name = r.metric.get("__name__", "")
        label_str = f"{name}{{{labels}}}" if labels else (name or "[dim](scalar)[/]")
        t.add_row(label_str, f"{r.value:g}")
    console.print(t)


@metrics_app.command("range")
def metrics_range(
    promql: str = typer.Argument(..., help="PromQL expression"),
    minutes: int = typer.Option(60, "--minutes", "-m", help="Lookback window in minutes"),
    step: int = typer.Option(60, "--step", "-s", help="Sample step in seconds"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | json"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """Range PromQL query — samples over a time window (for trend/history, not just a snapshot)."""
    if json_flag:
        output = _Out.json
    cfg = _load()
    pcfg = _require_prometheus(cfg)

    try:
        with console.status("[bold]Querying Prometheus…[/]"):
            results = prom_query_range(pcfg, promql, minutes, step)
    except Exception as e:
        console.print(f"[red]❌  {e}[/]")
        raise typer.Exit(1)

    if not results:
        console.print("[dim](no results)[/]")
        return

    if output == _Out.json:
        console.print_json(_json.dumps([
            {"metric": r.metric, "values": r.values}
            for r in results
        ]))
        return

    for r in results:
        labels = ", ".join(f"{k}={v}" for k, v in r.metric.items() if k != "__name__")
        name = r.metric.get("__name__", "")
        label_str = f"{name}{{{labels}}}" if labels else (name or "(scalar)")
        console.print(f"[cyan]{label_str}[/]")
        if not r.values:
            console.print("  [dim](no samples)[/]")
            continue
        vals = [v for _, v in r.values]
        console.print(
            f"  [dim]{len(r.values)} samples · "
            f"min={min(vals):g} max={max(vals):g} last={vals[-1]:g}[/]"
        )
        t = Table(box=box.SIMPLE, pad_edge=False, show_header=True, header_style="dim")
        t.add_column("Time")
        t.add_column("Value", justify="right")
        for ts, val in r.values[-20:]:
            t.add_row(datetime.fromtimestamp(ts).strftime("%H:%M:%S"), f"{val:g}")
        console.print(t)


@metrics_app.command("names")
def metrics_names(
    match: str = typer.Argument("", help="Optional substring filter, e.g. 'cpu'"),
) -> None:
    """List available metric names (discovery — 'what can I query?')."""
    cfg = _load()
    pcfg = _require_prometheus(cfg)

    try:
        with console.status("[bold]Fetching metric names…[/]"):
            names = prom_list_names(pcfg, match)
    except Exception as e:
        console.print(f"[red]❌  {e}[/]")
        raise typer.Exit(1)

    if not names:
        console.print("[dim](no matching metric names)[/]")
        return
    for n in names:
        console.print(n)
    console.print(f"\n[dim]{len(names)} metric name(s)[/]")


# ── uptimekuma ────────────────────────────────────────────────────────────────

from lib.uptimekuma import fetch_monitors as _uk_fetch, status_label as _uk_status

uptimekuma_app = typer.Typer(
    help="Show Uptime Kuma monitor statuses. [dim](up)[/dim]",
    no_args_is_help=False,
    rich_markup_mode="rich",
)
app.add_typer(uptimekuma_app, name="uptimekuma")


def _uk_render(failed: bool, output: _Out) -> None:
    cfg = _load()
    if not cfg.uptimekuma:
        console.print(
            "[yellow]No Uptime Kuma configured. "
            "Add an 'uptimekuma:' section to config.yaml.[/]"
        )
        raise typer.Exit(0)

    with console.status("[bold]Fetching Uptime Kuma status…[/]"):
        try:
            monitors = _uk_fetch(cfg.uptimekuma)
        except Exception as e:
            console.print(f"[red]❌  {e}[/]")
            raise typer.Exit(1)

    if not monitors:
        console.print("[yellow]No monitors found.[/]")
        raise typer.Exit(0)

    down        = sum(1 for m in monitors if m.status == 0)
    up          = sum(1 for m in monitors if m.status == 1)
    pending     = sum(1 for m in monitors if m.status == 2)
    maintenance = sum(1 for m in monitors if m.status == 3)
    visible     = [m for m in monitors if m.status == 0] if failed else monitors

    if failed and not visible:
        console.print("[green]✓  All monitors up.[/]")
        return

    if output == _Out.json:
        console.print_json(_json.dumps([
            {
                "id":          m.id,
                "name":        m.name,
                "group":       m.group,
                "status":      m.status,
                "latency_ms":  m.latency_ms,
                "uptime_24h":  round(m.uptime_24h  * 100, 2),
                "uptime_720h": round(m.uptime_720h * 100, 2),
            }
            for m in visible
        ]))
        return

    def _lat_str(m) -> str:
        if m.status != 1 or not m.latency_ms:
            return "[dim]—[/]"
        ms = m.latency_ms
        color = "green" if ms < 100 else ("yellow" if ms < 500 else "red")
        return f"[{color}]{ms}ms[/]"

    def _uptime_str(pct: float) -> str:
        p = pct * 100
        color = "green" if p >= 99.0 else ("yellow" if p >= 95.0 else "red")
        return f"[{color}]{p:.1f}%[/]"

    def _bar(pct: float, width: int = 10) -> str:
        filled = round(pct * width)
        p = pct * 100
        color = "green" if p >= 99.0 else ("yellow" if p >= 95.0 else "red")
        return f"[{color}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"

    # Build ordered groups preserving encounter order
    groups: dict[str, list] = {}
    for m in visible:
        key = m.group or ""
        groups.setdefault(key, []).append(m)

    # If all monitors share one unnamed group, skip section headers
    show_groups = not (len(groups) == 1 and "" in groups)

    t = Table(box=box.SIMPLE, pad_edge=False, show_header=True, header_style="dim")
    t.add_column("", width=2)
    t.add_column("Monitor", style="bold", min_width=22)
    t.add_column("Latency", justify="right", min_width=7)
    t.add_column("24h",    justify="right", min_width=6)
    t.add_column("30d",    justify="right", min_width=6)
    t.add_column("",       min_width=11, no_wrap=True)   # bar
    t.add_column("Status", min_width=13)

    first = True
    for group_name, mons in groups.items():
        if show_groups and group_name:
            if not first:
                t.add_row("", "", "", "", "", "", "")
            t.add_row("", f"[dim italic]{group_name}[/]", "", "", "", "", "")
            first = False
        for m in mons:
            label, color = _uk_status(m.status)
            t.add_row(
                f"[{color}]●[/]",
                m.name,
                _lat_str(m),
                _uptime_str(m.uptime_24h),
                _uptime_str(m.uptime_720h),
                _bar(m.uptime_720h),
                f"[{color}]{label}[/]",
            )
            first = False

    console.print(t)

    parts: list[str] = []
    if up:          parts.append(f"[green]● {up} up[/]")
    if down:        parts.append(f"[red bold]● {down} down[/]")
    if pending:     parts.append(f"[yellow]● {pending} pending[/]")
    if maintenance: parts.append(f"[blue]● {maintenance} maintenance[/]")
    parts.append(f"[dim]{len(monitors)} total[/]")
    console.print("  " + "  [dim]·[/]  ".join(parts))


@uptimekuma_app.callback(invoke_without_command=True)
def cmd_uptimekuma(
    ctx: typer.Context,
    failed: bool = typer.Option(False, "-f", "--failed", is_flag=True, help="Show only down monitors."),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | json"),
) -> None:
    """Show all monitor statuses."""
    if ctx.invoked_subcommand is not None:
        return
    _uk_render(failed=failed, output=output)


@uptimekuma_app.command("not")
def cmd_uptimekuma_not(
    output: _Out = typer.Option(_Out.table, "-o", help="Output format: table | json"),
) -> None:
    """Show only down monitors."""
    _uk_render(failed=True, output=output)


# ── acars ────────────────────────────────────────────────────────────────────

def _acars_fetch_prometheus(cfg) -> list[dict]:
    """Fetch firing/pending Prometheus alerts. Returns [{text, severity}], raises on error."""
    import httpx
    _SKIP = {"Watchdog", "NoAlertsFiring", "DeadMansSwitch"}
    result = []
    with httpx.Client(verify=cfg.verify_ssl, timeout=10) as client:
        r = client.get(f"{cfg.base_url}/api/v1/alerts")
        r.raise_for_status()
        for alert in r.json().get("data", {}).get("alerts", []):
            if alert.get("state") not in ("firing", "pending"):
                continue
            labels = alert.get("labels", {})
            name = labels.get("alertname", "?")
            if name in _SKIP:
                continue
            annotations = alert.get("annotations", {})
            severity = labels.get("severity", "")
            summary = annotations.get("summary", annotations.get("description", ""))
            state = alert.get("state", "")
            text = name
            if severity:
                text += f" ({severity})"
            if state == "pending":
                text += " [pending]"
            if summary:
                text += f" — {summary}"
            result.append({"text": text, "severity": severity})
    return result


_ACARS_LOG_WINDOW_MINUTES = 15  # "current", not historical — see atc blackbox for the deep dump


def _acars_fetch_unifi(cfg) -> list[dict]:
    """Devices offline, or rebooted/firmware-updated in the last _ACARS_LOG_WINDOW_MINUTES.
    Returns [{text, severity}], raises on error."""
    import time
    from lib.unifi import fetch_devices

    result = []
    now = int(time.time())
    since = now - (_ACARS_LOG_WINDOW_MINUTES * 60)

    for d in fetch_devices(cfg, site=cfg.site):
        if d.state != 1:
            result.append({"text": f"{d.name} ({d.model}) — offline (state={d.state})", "severity": "CRIT"})
            continue

        boot_time = now - d.uptime if d.uptime else d.startup_timestamp
        if not boot_time or boot_time < since:
            continue
        mins_ago = max(0, (now - boot_time) // 60)
        if d.previous_firmware_version and d.previous_firmware_version != d.version:
            text = f"{d.name} ({d.model}) firmware {d.previous_firmware_version} → {d.version}, rebooted {mins_ago}m ago"
        else:
            text = f"{d.name} ({d.model}) rebooted {mins_ago}m ago"
        result.append({"text": text, "severity": "WARN"})

    return result


def _acars_fetch_loki(cfg) -> list[dict]:
    """Flagged syslog/pihole lines in the last _ACARS_LOG_WINDOW_MINUTES.
    Returns [{text, severity}], raises on error. Always WARN — these are
    heuristic log-line matches, not a clean up/down signal like the other
    sources, so they shouldn't trip acars' CRIT/exit-1 behavior."""
    from lib.loki import fetch_network_events, fetch_pihole_events

    result = []
    for ts, host, line in fetch_network_events(cfg, _ACARS_LOG_WINDOW_MINUTES):
        prefix = f"{host}: " if host else ""
        result.append({"text": f"{prefix}{line.strip()}", "severity": "WARN"})
    for ts, host, line in fetch_pihole_events(cfg, _ACARS_LOG_WINDOW_MINUTES):
        prefix = f"{host}: " if host else ""
        result.append({"text": f"{prefix}{line.strip()}", "severity": "WARN"})
    return result


@app.command("acars")
def cmd_acars(
    certs_warn: int = typer.Option(30, "--certs-warn", help="Warn when cert expires within N days"),
    certs_crit: int = typer.Option(14, "--certs-crit", help="Critical when cert expires within N days"),
) -> None:
    """Active alerts across all configured infrastructure.

    Checks: Uptime Kuma monitors · TLS cert expiry · Proxmox nodes
            · Prometheus · Pi-hole instances · UniFi device state/firmware
            · Loki syslog/pihole log events (last 15 min — live, not historical;
            see `atc blackbox` for a deep historical dump).

    Exit code 1 when any CRIT alerts are present.
    """
    cfg = _load()

    alerts: list[tuple[str, str, str]] = []   # (sev, source, message)
    source_errors: list[str] = []

    async def _collect_all():
        async def _sync(fn, *args):
            return await asyncio.get_event_loop().run_in_executor(None, fn, *args)

        coros: dict = {}
        if cfg.uptimekuma:
            coros["uk"] = _sync(_uk_fetch, cfg.uptimekuma)
        if cfg.tls_checks:
            coros["certs"] = check_all_certs(cfg.tls_checks)
        coros["proxmox"] = fetch_cluster_status(cfg.proxmox)
        if cfg.prometheus:
            coros["prometheus"] = _sync(_acars_fetch_prometheus, cfg.prometheus)
        if cfg.pihole_instances:
            coros["pihole"] = pihole_fetch_all(cfg.pihole_instances)
        if cfg.unifi:
            coros["unifi"] = _sync(_acars_fetch_unifi, cfg.unifi)
        if cfg.loki:
            coros["loki"] = _sync(_acars_fetch_loki, cfg.loki)

        results = await asyncio.gather(*coros.values(), return_exceptions=True)
        return dict(zip(coros.keys(), results))

    with console.status("[bold]Checking all sources…[/]"):
        result_map = asyncio.run(_collect_all())

    # ── Uptime Kuma ──────────────────────────────────────────────────────────
    if "uk" in result_map:
        v = result_map["uk"]
        if isinstance(v, Exception):
            source_errors.append(f"Uptime Kuma: {v}")
        else:
            for m in v:
                if m.status != 1:
                    label, _ = _uk_status(m.status)
                    sev = "WARN" if m.status == 3 else "CRIT"  # maintenance=WARN, down/pending=CRIT
                    alerts.append((sev, "Uptime Kuma", f"{m.name} — {label} (24h: {m.uptime_24h*100:.0f}%)"))

    # ── TLS certs ────────────────────────────────────────────────────────────
    if "certs" in result_map:
        v = result_map["certs"]
        if isinstance(v, Exception):
            source_errors.append(f"TLS certs: {v}")
        else:
            for c in v:
                if not c.ok:
                    alerts.append(("CRIT", "TLS Certs", f"{c.name} ({c.host}:{c.port}) — {c.error[:60]}"))
                elif c.days_left <= certs_crit:
                    alerts.append(("CRIT", "TLS Certs", f"{c.name} — {c.days_left}d remaining"))
                elif c.days_left <= certs_warn:
                    alerts.append(("WARN", "TLS Certs", f"{c.name} — {c.days_left}d remaining"))

    # ── Proxmox ──────────────────────────────────────────────────────────────
    if "proxmox" in result_map:
        v = result_map["proxmox"]
        if isinstance(v, Exception):
            source_errors.append(f"Proxmox: {v}")
        else:
            for node in v.nodes:
                if not node.online:
                    alerts.append(("CRIT", "Proxmox", f"Node {node.name} — offline"))

    # ── Prometheus ───────────────────────────────────────────────────────────
    if "prometheus" in result_map:
        v = result_map["prometheus"]
        if isinstance(v, Exception):
            source_errors.append(f"Prometheus: {v}")
        else:
            for a in v:
                sev = "CRIT" if a["severity"] in ("critical", "error") else "WARN"
                alerts.append((sev, "Prometheus", a["text"]))

    # ── Pi-hole ──────────────────────────────────────────────────────────────
    if "pihole" in result_map:
        v = result_map["pihole"]
        if isinstance(v, Exception):
            source_errors.append(f"Pi-hole: {v}")
        else:
            for s in v:
                if not s.online:
                    alerts.append(("CRIT", "Pi-hole", f"{s.name} ({s.host}) — offline"))

    # ── UniFi ────────────────────────────────────────────────────────────────
    if "unifi" in result_map:
        v = result_map["unifi"]
        if isinstance(v, Exception):
            source_errors.append(f"UniFi: {v}")
        else:
            for e in v:
                alerts.append((e["severity"], "UniFi", e["text"]))

    # ── Loki (syslog/pihole, live) ───────────────────────────────────────────
    if "loki" in result_map:
        v = result_map["loki"]
        if isinstance(v, Exception):
            source_errors.append(f"Loki: {v}")
        else:
            for e in v:
                alerts.append((e["severity"], "Loki", e["text"]))

    # ── Render ────────────────────────────────────────────────────────────────

    n_sources = len(result_map)
    n_crits = sum(1 for sev, _, _ in alerts if sev == "CRIT")

    if not alerts and not source_errors:
        console.print(f"[green]✓[/]  ACARS — all clear  [dim]({n_sources} source(s) checked)[/]")
        return

    if alerts:
        console.print()
        t = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
        t.add_column("Sev", width=6)
        t.add_column("Source", min_width=14, style="dim")
        t.add_column("Message")
        for sev, source, msg in sorted(alerts, key=lambda x: (0 if x[0] == "CRIT" else 1, x[1])):
            sev_str = f"[red bold]{sev}[/]" if sev == "CRIT" else f"[yellow]{sev}[/]"
            t.add_row(sev_str, source, rich_escape(msg))
        console.print(t)
        n_issues = len(alerts)
        crits_str = f"  [red bold]{n_crits} critical[/]" if n_crits else ""
        console.print(f"\n[dim]{n_issues} issue(s) · {n_sources} source(s) checked{crits_str}[/]\n")

    if source_errors:
        for e in source_errors:
            console.print(f"  [yellow]⚠[/]  Source error — [dim]{e}[/]")
        console.print()

    if n_crits:
        raise typer.Exit(1)


# ── blackbox ──────────────────────────────────────────────────────────────────

from lib.blackbox import collect_all as _bb_collect, render_dump as _bb_render


@app.command("blackbox")
def cmd_blackbox(
    hours: int = typer.Option(2, "--hours", help="Lookback window in hours."),
    all_containers: bool = typer.Option(
        False,
        "--all-containers",
        help="Capture logs for ALL containers, not just unhealthy/exited ones.",
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        help="Output file path. Default: ./blackbox-YYYYMMDD-HHMMSS.txt in cwd.",
    ),
) -> None:
    """Collect a full-stack diagnostic snapshot and write it to a file.

    Gathers data from Uptime Kuma (outage history), Proxmox, SSH host
    journals, Portainer Docker environments, Home Assistant error log,
    Prometheus firing alerts, Loki syslog/pihole log events, and UniFi
    device reboots/firmware updates — all in parallel.
    """
    cfg = _load()

    if out is None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = Path.cwd() / f"blackbox-{ts}.txt"

    sources: list[str] = ["Proxmox", "SSH hosts"]
    if cfg.uptimekuma:
        sources.append("Uptime Kuma")
    if cfg.portainer:
        sources.append("Portainer (Docker)")
    if cfg.ha:
        sources.append("Home Assistant")
    if cfg.prometheus:
        sources.append("Prometheus")
    if cfg.loki:
        sources.append("Loki (syslog/pihole)")
    if cfg.unifi:
        sources.append("UniFi Network")

    console.print(f"[bold]Collecting blackbox dump[/] (window: {hours}h)")
    console.print(f"Sources: {', '.join(sources)}")
    console.print("")

    with console.status("[bold]Collecting from all sources…[/]"):
        try:
            dump = asyncio.run(
                _bb_collect(
                    cfg,
                    window_hours=hours,
                    all_containers=all_containers,
                    prometheus_url=cfg.prometheus.base_url if cfg.prometheus else "",
                )
            )
        except Exception as e:
            console.print(f"[red]❌  Collection failed: {e}[/]")
            raise typer.Exit(1)

    text = _bb_render(dump)

    try:
        out.write_text(text, encoding="utf-8")
    except Exception as e:
        console.print(f"[red]❌  Could not write file: {e}[/]")
        raise typer.Exit(1)

    # Print a brief summary to the terminal
    console.print(f"[green]✔[/]  Dump written → [bold]{out}[/]")
    console.print("")

    if dump.uptime_kuma:
        down_count = len(dump.uptime_kuma.down_monitors)
        outage_count = len(dump.uptime_kuma.outages)
        color = "red" if down_count else "green"
        console.print(
            f"  Uptime Kuma  [{color}]{down_count} monitor(s) DOWN[/], "
            f"{outage_count} recent outage(s)"
        )

    if dump.proxmox:
        if dump.proxmox.error:
            console.print(f"  Proxmox      [yellow]ERROR: {dump.proxmox.error}[/]")
        else:
            n_off = len(dump.proxmox.offline_nodes)
            n_fail = len(dump.proxmox.failed_tasks)
            color = "red" if n_off or n_fail else "green"
            console.print(
                f"  Proxmox      [{color}]{n_off} offline node(s), {n_fail} failed task(s)[/]"
            )

    for host in dump.ssh_hosts:
        if host.error:
            console.print(f"  SSH {host.name:<15} [yellow]ERROR: {host.error}[/]")
        else:
            line_count = host.journal_errors.count("\n") + 1 if host.journal_errors.strip() else 0
            color = "yellow" if line_count else "green"
            console.print(f"  SSH {host.name:<15} [{color}]{line_count} error line(s)[/]")

    for env in dump.docker_envs:
        if env.error:
            console.print(f"  Docker {env.name:<12} [yellow]ERROR: {env.error}[/]")
        else:
            attn = sum(1 for c in env.containers if "unhealthy" in c or "exited" in c)
            color = "red" if attn else "green"
            console.print(
                f"  Docker {env.name:<12} [{color}]{len(env.containers)} container(s),"
                f" {len(env.logs)} log(s) captured[/]"
            )

    if dump.homeassistant:
        if dump.homeassistant.error:
            console.print(f"  HA           [yellow]ERROR: {dump.homeassistant.error}[/]")
        else:
            console.print("  HA           [green]error log captured[/]")

    if dump.prometheus:
        n = len(dump.prometheus.firing_alerts)
        color = "red" if n else "green"
        console.print(f"  Prometheus   [{color}]{n} firing alert(s)[/]")

    if dump.loki:
        if dump.loki.error:
            console.print(f"  Loki         [yellow]ERROR: {dump.loki.error}[/]")
        else:
            n_syslog = len(dump.loki.syslog_events)
            n_pihole = len(dump.loki.pihole_events)
            color = "yellow" if n_syslog or n_pihole else "green"
            console.print(
                f"  Loki         [{color}]{n_syslog} syslog event(s),"
                f" {n_pihole} pihole event(s)[/]"
            )

    if dump.unifi:
        if dump.unifi.error:
            console.print(f"  UniFi        [yellow]ERROR: {dump.unifi.error}[/]")
        else:
            n_reboot = len(dump.unifi.recent_reboots)
            n_fw = len(dump.unifi.firmware_updates)
            n_off = len(dump.unifi.offline_devices)
            color = "red" if n_off else ("yellow" if n_reboot or n_fw else "green")
            console.print(
                f"  UniFi        [{color}]{len(dump.unifi.devices)} device(s),"
                f" {n_reboot} reboot(s), {n_fw} firmware update(s), {n_off} offline[/]"
            )

    console.print("")
    console.print(f"Open: [dim]{out}[/]")


# ── docker ────────────────────────────────────────────────────────────────────

docker_app = typer.Typer(
    help="Manage Docker containers and Compose stacks across all Portainer environments",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(docker_app, name="docker")


def _require_portainer(cfg):
    if not cfg.portainer:
        console.print("[red]❌  No Portainer configured (missing [portainer] in config)[/]")
        raise typer.Exit(1)
    return cfg.portainer


def _fetch_all_containers(pt_cfg):
    """Return (envs, containers) across all active Portainer environments."""
    from lib.portainer import fetch_environments, fetch_containers
    envs = fetch_environments(pt_cfg)
    containers = []
    for env in envs:
        if env.status != 1:
            continue
        containers.extend(fetch_containers(pt_cfg, env.id, env.name, all_containers=True))
    return envs, containers


def _match_containers(containers, selector: str, env_filter: str | None):
    sel = selector.lower()
    matches = [c for c in containers if sel in c.name.lower()]
    if env_filter:
        matches = [c for c in matches if env_filter.lower() in c.env_name.lower()]
    return matches


def _match_stacks(stacks, selector: str, env_filter: str | None):
    sel = selector.lower()
    matches = [s for s in stacks if sel in s.name.lower()]
    if env_filter:
        matches = [s for s in matches if env_filter.lower() in s.env_name.lower()]
    return matches


@docker_app.command("ls")
def docker_ls(
    env: str | None = typer.Option(None, "--env", "-e", help="Filter by environment name"),
    all_: bool = typer.Option(False, "--all", "-a", help="Include stopped containers"),
) -> None:
    """List containers across all Portainer environments."""
    cfg = _load()
    pt_cfg = _require_portainer(cfg)

    from lib.portainer import fetch_environments, fetch_containers
    with console.status("[bold]Fetching containers…[/]"):
        envs = fetch_environments(pt_cfg)
        rows = []
        for e in envs:
            if env and env.lower() not in e.name.lower():
                continue
            if e.status != 1:
                continue
            for c in fetch_containers(pt_cfg, e.id, e.name, all_containers=True):
                if not all_ and c.state not in ("running", "restarting"):
                    continue
                rows.append(c)

    from rich.table import Table
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Environment", style="dim")
    table.add_column("Container")
    table.add_column("State")
    table.add_column("Health")
    table.add_column("Status", style="dim")

    _STATE_ICON = {
        "running": "🟢", "restarting": "🟡", "paused": "⚫",
        "exited": "🔴", "dead": "🔴",
    }
    for c in sorted(rows, key=lambda x: (x.env_name, x.name)):
        icon = _STATE_ICON.get(c.state, "⚫")
        health = c.health or "—"
        table.add_row(c.env_name, c.name, f"{icon} {c.state}", health, c.status)

    console.print(table)


@docker_app.command("logs")
def docker_logs(
    selector: str = typer.Argument(..., help="Container name (substring match)"),
    env: str | None = typer.Option(None, "--env", "-e", help="Scope to a specific environment"),
    tail: int = typer.Option(200, "--tail", "-n", help="Number of lines to show"),
    since: int = typer.Option(7200, "--since", help="Look back this many seconds"),
) -> None:
    """Show recent logs for a container."""
    cfg = _load()
    pt_cfg = _require_portainer(cfg)

    from lib.portainer import fetch_container_logs
    with console.status("[bold]Fetching containers…[/]"):
        _, containers = _fetch_all_containers(pt_cfg)
        matches = _match_containers(containers, selector, env)

    if not matches:
        console.print(f"[yellow]No container found matching '{selector}'[/]")
        raise typer.Exit(1)
    if len(matches) > 1:
        names = ", ".join(f"{c.name} ({c.env_name})" for c in matches)
        console.print(f"[red]❌  '{selector}' matches multiple containers: {names}[/]")
        raise typer.Exit(1)
    c = matches[0]

    with console.status(f"[bold]Fetching logs for {c.name}…[/]"):
        logs = fetch_container_logs(pt_cfg, c.env_id, c.id, since_seconds=since, tail=tail)

    console.print(f"[dim]── {c.name} ({c.env_name}) — last {tail} line(s) ──[/]")
    console.print(logs)


@docker_app.command("stacks")
def docker_stacks(
    env: str | None = typer.Option(None, "--env", "-e", help="Filter by environment name"),
) -> None:
    """List Compose stacks across all Portainer environments.

    Shows both stacks managed by Portainer and projects inferred from
    docker-compose labels on containers (e.g. stacks deployed via
    Synology Container Manager or plain docker compose).
    """
    cfg = _load()
    pt_cfg = _require_portainer(cfg)

    from lib.portainer import (
        fetch_environments, fetch_containers, fetch_stacks, infer_compose_stacks,
    )
    with console.status("[bold]Fetching stacks…[/]"):
        envs = fetch_environments(pt_cfg)
        portainer_stacks = fetch_stacks(pt_cfg, envs)
        all_containers = []
        for e in envs:
            if e.status != 1:
                continue
            all_containers.extend(fetch_containers(pt_cfg, e.id, e.name, all_containers=True))
        known_names = {s.name for s in portainer_stacks}
        inferred = infer_compose_stacks(all_containers, portainer_stack_names=known_names)

    if env:
        portainer_stacks = [s for s in portainer_stacks if env.lower() in s.env_name.lower()]
        inferred = [s for s in inferred if env.lower() in s.env_name.lower()]

    from rich.table import Table
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Environment", style="dim")
    table.add_column("Stack")
    table.add_column("Status")
    table.add_column("Source", style="dim")

    for s in sorted(portainer_stacks, key=lambda x: (x.env_name, x.name)):
        status = "🟢 active" if s.status == 1 else "⚫ inactive"
        table.add_row(s.env_name, s.name, status, "portainer")

    for s in sorted(inferred, key=lambda x: (x.env_name, x.name)):
        status = f"🟢 {s.running}/{s.total} running" if s.running else f"⚫ 0/{s.total} running"
        table.add_row(s.env_name, s.name, status, "compose labels")

    console.print(table)


def _parse_env_file(path: Path) -> list[dict]:
    """Parse a .env file into Portainer's [{name, value}] stack-env format."""
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        entries.append({"name": key, "value": value})
    return entries


def _resolve_single_env(envs, target: str):
    """Match a single active environment by substring; exit with an error if 0 or 2+ match."""
    matches = [e for e in envs if target.lower() in e.name.lower()]
    if not matches:
        console.print(f"[red]❌  No active environment matching '{target}'[/]")
        raise typer.Exit(1)
    if len(matches) > 1:
        names = ", ".join(e.name for e in matches)
        console.print(f"[red]❌  '{target}' matches multiple environments: {names}[/]")
        raise typer.Exit(1)
    return matches[0]


compose_app = typer.Typer(
    help="Deploy, update, or remove Compose stacks via Portainer",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
docker_app.add_typer(compose_app, name="compose")


@compose_app.command("up")
def docker_compose_up(
    compose_file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Path to a docker-compose.yml file",
    ),
    target: str = typer.Option(
        "docker-1", "--target", "-t", help="Environment name to deploy to (substring match)",
    ),
    name: str | None = typer.Option(
        None, "--name", "-n", help="Stack name (defaults to the compose file's parent directory name)",
    ),
    env_file: Path | None = typer.Option(
        None, "--env-file", "-f",
        help="Path to a .env file (default: a .env next to the compose file, if it exists)",
    ),
    pull: bool = typer.Option(
        False, "--pull", help="Force a fresh image pull before redeploying an existing stack",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change without deploying",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Deploy or update a Compose stack on a Portainer environment.

    Creates a new stack if one with this name doesn't yet exist on the target
    environment (default: docker-1, the local node), otherwise redeploys it
    with the new file content. Variables from .env are passed through to
    Portainer for ${VAR} substitution in the compose file, same as `docker compose`.
    """
    cfg = _load()
    pt_cfg = _require_portainer(cfg)

    compose_content = compose_file.read_text()
    stack_name = name or compose_file.resolve().parent.name

    if env_file is not None:
        if not env_file.exists():
            console.print(f"[red]❌  {env_file} not found[/]")
            raise typer.Exit(1)
        resolved_env_file = env_file
    else:
        resolved_env_file = compose_file.resolve().parent / ".env"

    # None (not []) when no .env file exists — [] is reserved to mean "explicitly
    # clear all env vars" per update_stack's safety guard (see portainer.py). A
    # missing .env means "no local vars to apply", not "wipe whatever's already
    # deployed" — None tells update_stack to auto-fetch and preserve the stack's
    # current Env array instead.
    env_vars = _parse_env_file(resolved_env_file) if resolved_env_file.exists() else None

    from lib.portainer import (
        fetch_environments, fetch_containers, fetch_stacks, fetch_stack_file,
        infer_compose_stacks, create_stack, update_stack,
    )
    with console.status("[bold]Resolving target environment…[/]"):
        envs = [e for e in fetch_environments(pt_cfg) if e.status == 1]
        env = _resolve_single_env(envs, target)

    with console.status(f"[bold]Checking existing stacks on {env.name}…[/]"):
        portainer_stacks = fetch_stacks(pt_cfg, envs)
        existing = [s for s in portainer_stacks if s.env_id == env.id and s.name == stack_name]
        if len(existing) > 1:
            # Two stacks with this name means an earlier deploy already went
            # sideways (e.g. a client-side timeout on `up` that actually
            # succeeded server-side, retried into a duplicate create).
            # Silently updating existing[0] would leave the true duplicate
            # in place and risks touching the wrong one. Refuse and point at
            # manual cleanup instead of compounding the ambiguity.
            ids = ", ".join(str(s.id) for s in existing)
            console.print(
                f"[red]❌  {len(existing)} stacks named '{stack_name}' already exist "
                f"on {env.name} (ids: {ids}) — refusing to guess which one to update. "
                f"This usually means a prior deploy timed out client-side but actually "
                f"succeeded server-side, and a retry created a duplicate. Resolve by id "
                f"via the Portainer UI/API before deploying again.[/]"
            )
            raise typer.Exit(1)
        if not existing:
            # Not Portainer-managed by this name — but it might still be running
            # under the same compose project name via a route Portainer doesn't
            # track (e.g. Synology Container Manager on Globemaster/Greyhound).
            # Creating a stack on top of that would either collide on container
            # names or, worse, silently split-brain the same project name across
            # two management systems. Refuse rather than risk either.
            env_containers = fetch_containers(pt_cfg, env.id, env.name, all_containers=True)
            known_names = {s.name for s in portainer_stacks}
            inferred = infer_compose_stacks(env_containers, portainer_stack_names=known_names)
            if any(s.name == stack_name for s in inferred):
                console.print(
                    f"[red]❌  '{stack_name}' already exists as an unmanaged Compose project "
                    f"on {env.name} (not registered in Portainer — likely deployed via Synology "
                    f"Container Manager or plain `docker compose`). Deploying through atc would "
                    f"either collide on container names or split-brain the project across two "
                    f"management systems. Use a different --name, or manage this one outside atc.[/]"
                )
                raise typer.Exit(1)

    if dry_run:
        if existing:
            try:
                current = fetch_stack_file(pt_cfg, existing[0].id)
            except Exception as e:
                console.print(f"[red]❌  Could not fetch current stack file: {e}[/]")
                raise typer.Exit(1)
            diff = list(difflib.unified_diff(
                current.splitlines(keepends=True),
                compose_content.splitlines(keepends=True),
                fromfile=f"{stack_name} (deployed)",
                tofile=f"{compose_file} (local)",
            ))
            if diff:
                console.print("".join(diff), end="")
            else:
                console.print(f"[dim]No changes — {stack_name} is already up to date on {env.name}[/]")
        else:
            console.print(f"[bold]Would create[/] new stack [cyan]{stack_name}[/] on [cyan]{env.name}[/]:\n")
            console.print(compose_content)
        raise typer.Exit(0)

    action = "Update" if existing else "Create"
    console.print(f"\n[bold]{action}[/] stack [cyan]{stack_name}[/] on [cyan]{env.name}[/] from {compose_file}")
    if env_vars:
        console.print(f"[dim]Loaded {len(env_vars)} var(s) from {resolved_env_file}[/]")
    if pull and existing:
        console.print("[dim]Will force a fresh image pull[/]")
    if not yes and not Confirm.ask(f"\n{action} stack '{stack_name}'?"):
        raise typer.Exit(0)

    try:
        with console.status(f"[bold]{action.rstrip('e')}ing stack {stack_name}…[/]"):
            if existing:
                update_stack(pt_cfg, existing[0].id, env.id, compose_content, env_vars, pull_image=pull)
            else:
                create_stack(pt_cfg, env.id, stack_name, compose_content, env_vars)
        console.print(f"[green]✓[/] {stack_name} — {action.lower()}d on {env.name}")
    except Exception as e:
        console.print(f"[red]✗[/] {stack_name} — {e}")
        if not existing:
            # A create call can time out client-side (slow image pull) while
            # still completing server-side — Portainer doesn't cancel the
            # deploy just because our connection dropped. Retrying blind
            # risks creating a second stack of the same name sharing the
            # same underlying Compose project, which silently collides.
            # Always re-check before retrying a failed create.
            console.print(
                f"[yellow]⚠  This was a CREATE — before retrying, run "
                f"`atc docker stacks --env {env.name}` to check whether "
                f"'{stack_name}' actually landed despite the error "
                f"(common with slow first-time image pulls). If it did, "
                f"re-run this command — it will update the existing stack "
                f"instead of creating a duplicate.[/]"
            )
        raise typer.Exit(1)


@compose_app.command("down")
def docker_compose_down(
    name: str = typer.Argument(..., help="Stack name"),
    target: str = typer.Option(
        "docker-1", "--target", "-t", help="Environment the stack lives on (substring match)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Remove a Portainer-managed Compose stack and its containers."""
    cfg = _load()
    pt_cfg = _require_portainer(cfg)

    from lib.portainer import fetch_environments, fetch_stacks, delete_stack
    with console.status("[bold]Resolving target environment…[/]"):
        envs = [e for e in fetch_environments(pt_cfg) if e.status == 1]
        env = _resolve_single_env(envs, target)

    with console.status(f"[bold]Looking up stack '{name}' on {env.name}…[/]"):
        found = [s for s in fetch_stacks(pt_cfg, envs) if s.env_id == env.id and s.name == name]

    if not found:
        console.print(f"[yellow]No stack named '{name}' found on {env.name}[/]")
        raise typer.Exit(1)
    if len(found) > 1:
        # Two stacks can share a name (e.g. a client-side timeout on `up`
        # that actually succeeded server-side, retried into a duplicate
        # create). Silently taking found[0] risks deleting the wrong one —
        # including one with live, in-use containers. Force the caller to
        # disambiguate by id via the raw Portainer API instead.
        ids = ", ".join(str(s.id) for s in found)
        console.print(
            f"[red]❌  {len(found)} stacks named '{name}' exist on {env.name} "
            f"(ids: {ids}) — refusing to guess which one to remove. Inspect "
            f"each with `atc docker compose up ... --dry-run` (diffs against "
            f"a given stack id isn't exposed here) or delete the unwanted "
            f"one directly via the Portainer UI/API by id.[/]"
        )
        raise typer.Exit(1)
    stack = found[0]

    console.print(
        f"\n[bold red]Remove[/] stack [cyan]{stack.name}[/] on [cyan]{env.name}[/] "
        f"(stops and deletes all its containers)"
    )
    if not yes and not Confirm.ask(f"\nRemove stack '{stack.name}'?"):
        raise typer.Exit(0)

    try:
        with console.status(f"[bold]Removing stack {stack.name}…[/]"):
            delete_stack(pt_cfg, stack.id, env.id)
        console.print(f"[green]✓[/] {stack.name} — removed from {env.name}")
    except Exception as e:
        console.print(f"[red]✗[/] {stack.name} — {e}")
        raise typer.Exit(1)


def _docker_action(
    selector: str,
    env_filter: str | None,
    yes: bool,
    verb: str,
) -> None:
    """Shared logic for start/stop/restart across containers, Portainer stacks, and inferred stacks."""
    cfg = _load()
    pt_cfg = _require_portainer(cfg)

    from lib.portainer import (
        fetch_environments, fetch_containers, fetch_stacks, infer_compose_stacks,
        container_action, stack_action,
    )
    with console.status("[bold]Fetching Portainer state…[/]"):
        envs = fetch_environments(pt_cfg)
        all_containers = []
        for e in envs:
            if e.status != 1:
                continue
            all_containers.extend(fetch_containers(pt_cfg, e.id, e.name, all_containers=True))
        portainer_stacks = fetch_stacks(pt_cfg, envs)
        known_names = {s.name for s in portainer_stacks}
        inferred_stacks = infer_compose_stacks(all_containers, portainer_stack_names=known_names)

    matched_containers = _match_containers(all_containers, selector, env_filter)
    matched_stacks = _match_stacks(portainer_stacks, selector, env_filter)
    matched_inferred = [s for s in inferred_stacks
                        if selector.lower() in s.name.lower()
                        and (not env_filter or env_filter.lower() in s.env_name.lower())]

    # If the selector matches a stack (portainer or inferred), don't also act on its
    # individual containers — that would double-up the action.
    stack_project_names = {s.name.lower() for s in matched_stacks}
    stack_project_names |= {s.name.lower() for s in matched_inferred}
    matched_containers = [
        c for c in matched_containers
        if c.compose_project.lower() not in stack_project_names
    ]

    if not matched_containers and not matched_stacks and not matched_inferred:
        console.print(f"[yellow]No container or stack found matching '{selector}'[/]")
        raise typer.Exit(1)

    console.print(f"\nTargets to [bold]{verb}[/]:")
    for c in matched_containers:
        console.print(f"  [cyan]•[/] [dim][container][/]      {c.name} [dim](env: {c.env_name}, {c.state})[/]")
    for s in matched_stacks:
        state = "active" if s.status == 1 else "inactive"
        console.print(f"  [cyan]•[/] [dim][portainer stack][/] {s.name} [dim](env: {s.env_name}, {state})[/]")
    for s in matched_inferred:
        console.print(
            f"  [cyan]•[/] [dim][compose project][/]  {s.name} "
            f"[dim](env: {s.env_name}, {s.running}/{s.total} running)[/]"
        )

    total = len(matched_containers) + len(matched_stacks) + len(matched_inferred)
    if not yes and not Confirm.ask(f"\n{verb.capitalize()} {total} target(s)?"):
        raise typer.Exit(0)

    for c in matched_containers:
        with console.status(f"[bold]{verb.capitalize()}ing container {c.name}…[/]"):
            try:
                container_action(pt_cfg, c.env_id, c.id, verb)
                console.print(f"[green]✓[/] {c.name} — {verb} issued")
            except Exception as e:
                console.print(f"[red]✗[/] {c.name} — {e}")

    for s in matched_stacks:
        with console.status(f"[bold]{verb.capitalize()}ing stack {s.name}…[/]"):
            try:
                if verb == "restart":
                    stack_action(pt_cfg, s.id, "stop", s.env_id)
                    stack_action(pt_cfg, s.id, "start", s.env_id)
                else:
                    stack_action(pt_cfg, s.id, verb, s.env_id)
                console.print(f"[green]✓[/] {s.name} — {verb} issued")
            except Exception as e:
                console.print(f"[red]✗[/] {s.name} — {e}")

    for s in matched_inferred:
        errors = []
        with console.status(f"[bold]{verb.capitalize()}ing compose project {s.name}…[/]"):
            for c in s.containers:
                try:
                    container_action(pt_cfg, c.env_id, c.id, verb)
                except Exception as e:
                    errors.append(f"{c.name}: {e}")
        if errors:
            console.print(f"[red]✗[/] {s.name} — partial failure:")
            for err in errors:
                console.print(f"    [dim]{err}[/]")
        else:
            console.print(f"[green]✓[/] {s.name} — {verb} issued ({s.total} container(s))")


@docker_app.command("start")
def docker_start(
    selector: str = typer.Argument(..., help="Container or stack name (substring match)"),
    env: str | None = typer.Option(None, "--env", "-e", help="Scope to a specific environment"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Start a container or Compose stack (matched by name)."""
    _docker_action(selector, env, yes, "start")


@docker_app.command("stop")
def docker_stop(
    selector: str = typer.Argument(..., help="Container or stack name (substring match)"),
    env: str | None = typer.Option(None, "--env", "-e", help="Scope to a specific environment"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Stop a container or Compose stack (matched by name)."""
    _docker_action(selector, env, yes, "stop")


@docker_app.command("restart")
def docker_restart(
    selector: str = typer.Argument(..., help="Container or stack name (substring match)"),
    env: str | None = typer.Option(None, "--env", "-e", help="Scope to a specific environment"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Restart a container or Compose stack (matched by name).

    Stacks are restarted via stop → start. Containers use the Docker restart API.
    """
    _docker_action(selector, env, yes, "restart")


@docker_app.command("images")
def docker_images(
    env: str | None = typer.Option(None, "--env", "-e", help="Filter by environment name"),
    dangling: bool = typer.Option(False, "--dangling", "-d", help="Show only dangling (untagged) images"),
) -> None:
    """List images across all Portainer environments."""
    cfg = _load()
    pt_cfg = _require_portainer(cfg)

    from lib.portainer import fetch_environments, fetch_images
    with console.status("[bold]Fetching images…[/]"):
        envs = fetch_environments(pt_cfg)
        rows = []
        for e in envs:
            if env and env.lower() not in e.name.lower():
                continue
            if e.status != 1:
                continue
            rows.extend(fetch_images(pt_cfg, e.id, e.name))

    if dangling:
        rows = [img for img in rows if img.dangling]

    from rich.table import Table
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Environment", style="dim")
    table.add_column("Image")
    table.add_column("Size", justify="right")
    table.add_column("Created", style="dim")

    for img in sorted(rows, key=lambda x: (x.env_name, x.display_name)):
        size_str = _fmt_bytes(img.size) if img.size else "—"
        created_str = datetime.fromtimestamp(img.created).strftime("%Y-%m-%d") if img.created else "—"
        name = f"[yellow]{img.display_name}[/]" if img.dangling else img.display_name
        table.add_row(img.env_name, name, size_str, created_str)

    console.print(table)
    total = sum(img.size for img in rows)
    console.print(f"\n[dim]{len(rows)} image(s), {_fmt_bytes(total)} total[/]")


@docker_app.command("volumes")
def docker_volumes(
    env: str | None = typer.Option(None, "--env", "-e", help="Filter by environment name"),
) -> None:
    """List volumes across all Portainer environments."""
    cfg = _load()
    pt_cfg = _require_portainer(cfg)

    from lib.portainer import fetch_environments, fetch_volumes
    with console.status("[bold]Fetching volumes…[/]"):
        envs = fetch_environments(pt_cfg)
        rows = []
        for e in envs:
            if env and env.lower() not in e.name.lower():
                continue
            if e.status != 1:
                continue
            rows.extend(fetch_volumes(pt_cfg, e.id, e.name))

    from rich.table import Table
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Environment", style="dim")
    table.add_column("Volume")
    table.add_column("Driver", style="dim")
    table.add_column("Created", style="dim")

    for v in sorted(rows, key=lambda x: (x.env_name, x.name)):
        created_str = v.created[:10] if v.created else "—"
        table.add_row(v.env_name, v.name, v.driver, created_str)

    console.print(table)
    console.print(f"\n[dim]{len(rows)} volume(s)[/]")


@docker_app.command("prune")
def docker_prune(
    env: str | None = typer.Option(None, "--env", "-e", help="Scope to a specific environment"),
    volumes: bool = typer.Option(True, "--volumes/--no-volumes", help="Prune orphaned volumes"),
    images: bool = typer.Option(True, "--images/--no-images", help="Prune dangling images"),
    all_images: bool = typer.Option(False, "--all-images", help="Also prune unused (not just dangling) images"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Remove orphaned volumes and dangling images across all Portainer environments.

    By default prunes both unused volumes and dangling (untagged) images.
    Use [bold]--all-images[/] to also remove images not referenced by any container.
    """
    cfg = _load()
    pt_cfg = _require_portainer(cfg)

    from lib.portainer import fetch_environments, prune_volumes, prune_images
    with console.status("[bold]Fetching environments…[/]"):
        envs = fetch_environments(pt_cfg)

    target_envs = [e for e in envs if e.status == 1]
    if env:
        target_envs = [e for e in target_envs if env.lower() in e.name.lower()]

    if not target_envs:
        console.print("[yellow]No active environments matched.[/]")
        raise typer.Exit(1)

    # Summarise what will happen
    actions = []
    if volumes:
        actions.append("orphaned volumes")
    if images:
        label = "all unused images" if all_images else "dangling (untagged) images"
        actions.append(label)

    if not actions:
        console.print("[yellow]Nothing to prune (both --no-volumes and --no-images were set).[/]")
        raise typer.Exit(0)

    console.print(f"\nWill prune [bold]{', '.join(actions)}[/] on:")
    for e in target_envs:
        console.print(f"  [cyan]•[/] {e.name}")

    if not yes and not Confirm.ask("\nProceed?"):
        raise typer.Exit(0)

    console.print("")
    total_vols = total_imgs = 0
    total_bytes = 0

    for e in target_envs:
        if volumes:
            try:
                with console.status(f"Pruning volumes on [bold]{e.name}[/]…"):
                    n, b = prune_volumes(pt_cfg, e.id)
                console.print(f"[green]✓[/] {e.name} volumes  — {n} removed, {_fmt_bytes(b)} reclaimed")
                total_vols += n
                total_bytes += b
            except Exception as ex:
                console.print(f"[red]✗[/] {e.name} volumes  — {ex}")

        if images:
            try:
                with console.status(f"Pruning images on [bold]{e.name}[/]…"):
                    n, b = prune_images(pt_cfg, e.id, all_unused=all_images)
                console.print(f"[green]✓[/] {e.name} images   — {n} removed, {_fmt_bytes(b)} reclaimed")
                total_imgs += n
                total_bytes += b
            except Exception as ex:
                console.print(f"[red]✗[/] {e.name} images   — {ex}")

    console.print(
        f"\n[bold]Total:[/] {total_vols} volume(s) + {total_imgs} image(s) removed, "
        f"[green]{_fmt_bytes(total_bytes)}[/] reclaimed"
    )


# ── tree ──────────────────────────────────────────────────────────────────────

# Aliases shown inline in tree: {group_name: {canonical_cmd: alias}}
_GROUP_ALIASES: dict[str, dict[str, str]] = {
    "get":     {"hosts": "ls"},
    "secrets": {"list": "ls", "delete": "rm"},
    "backup":  {"list": "ls"},
    "pihole/dns": {"list": "ls"},
    "seerr":   {"list": "ls"},
}


@app.command("tree")
def show_tree() -> None:
    """Show all commands and their arguments as a tree."""
    from rich.tree import Tree as RichTree

    click_app = typer.main.get_command(app)

    def _param_label(param: click.Parameter) -> str:
        if isinstance(param, click.Argument):
            name = param.name or ""
            return f"[yellow]\\[{name}][/]" if not param.required else f"[yellow]<{name}>[/]"
        if isinstance(param, click.Option):
            opts = "/".join(param.opts)
            if param.is_flag:
                return f"[dim]{opts}[/]"
            type_name = param.type.name if hasattr(param.type, "name") else "value"
            return f"[dim]{opts} <{type_name}>[/]"
        return ""

    def _add_commands(node: RichTree, group: click.Group, group_name: str = "") -> None:
        # alias_for: canonical_name -> alias_name (e.g. "hosts" -> "ls")
        alias_for = _GROUP_ALIASES.get(group_name, {})

        for name, cmd in sorted(group.commands.items()):  # type: ignore[union-attr]
            if cmd.hidden:
                continue  # hidden = alias, shown inline on its canonical command
            help_text = (cmd.help or "").split("\n")[0]
            alias_str = f" [dim]({alias_for[name]})[/dim]" if name in alias_for else ""

            if isinstance(cmd, click.Group):
                branch = node.add(f"[bold cyan]{name}[/]{alias_str}  [dim]{help_text}[/]")
                _add_commands(branch, cmd, name)
            else:
                args = [_param_label(p) for p in cmd.params if isinstance(p, click.Argument)]
                opts = [_param_label(p) for p in cmd.params if isinstance(p, click.Option) and "--help" not in p.opts]
                parts = " ".join(args + opts)
                label = f"[bold cyan]{name}[/]{alias_str}"
                if parts:
                    label += f"  {parts}"
                if help_text:
                    label += f"  [dim]{help_text}[/]"
                node.add(label)

    root = RichTree("[bold]atc[/]  [dim]Air Traffic Control — homelab management CLI[/]")
    _add_commands(root, click_app)  # type: ignore[arg-type]

    console.print(root)


# ── media ─────────────────────────────────────────────────────────────────────

media_app = typer.Typer(
    help="Media pipeline: Seerr · Radarr · Sonarr · Plex · Streams",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(media_app, name="media")


# ── pipeline ──────────────────────────────────────────────────────────────────

from lib.arr import (
    get_movie_by_id, get_movie_by_tmdb, get_series_by_id, get_series_by_title,
    get_queue as arr_get_queue, get_movie_import_date,
    ArrMovieRecord, ArrSeriesRecord,
)
from lib.plex import (
    find_media as plex_find_media,
    get_server_info as plex_get_server_info,
    list_libraries as plex_list_libraries,
    scan_library as plex_scan_library,
    analyze_library as plex_analyze_library,
    empty_trash as plex_empty_trash,
    clean_bundles as plex_clean_bundles,
    _section_key as plex_section_key,
    get_activities as plex_get_activities,
    get_butler_tasks as plex_get_butler_tasks,
    list_sessions as plex_list_sessions,
    terminate_session as plex_terminate_session,
)


def _fmt_bytes(b: int) -> str:
    if b == 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def _mini_bar(pct: float, width: int = 12) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _pipeline_row(
    step: int, label: str, icon: str, style: str, summary: str, detail: str = ""
) -> None:
    detail_str = f" [dim]· {detail}[/]" if detail else ""
    console.print(
        f"  [dim]{step}.[/] [dim]{label:<17}[/] [{style}]{icon}[/]  [{style}]{summary}[/]{detail_str}"
    )


def _stage_seerr(r) -> tuple[str, str, str, str]:
    """Returns (icon, style, summary, detail) for the Seerr stage."""
    code = r.media_status_code
    req = r.request_status_code
    by_date = " · ".join(p for p in [r.request_by, r.request_date] if p)

    def _season_str(progress: bool = False) -> str:
        """Season count label for TV shows.

        progress=True  → "avail/total seasons · avail_eps/req_eps eps" (zeros suppressed)
        progress=False → "req seasons · req_eps eps" (what entered the pipeline)
        Falls back gracefully when counts aren't populated yet.
        """
        if r.media_type != "tv":
            return ""
        parts: list[str] = []
        if progress:
            if r.seasons_requested:
                # We know exactly which seasons were requested
                if r.seasons_available > 0:
                    parts.append(f"{r.seasons_available}/{r.seasons_requested} seasons")
                else:
                    n = r.seasons_requested
                    parts.append(f"{n} season{'s' if n != 1 else ''} requested")
            elif r.seasons_total:
                # Only total is known — show it without implying it was all requested
                parts.append(f"{r.seasons_total} seasons tracked")
            req_eps = r.episodes_requested or r.episodes_total
            if req_eps and r.episodes_available > 0:
                parts.append(f"{r.episodes_available}/{req_eps} eps")
        else:
            n_seasons = r.seasons_requested or r.seasons_total
            if n_seasons:
                parts.append(f"{n_seasons} seasons")
            n_eps = r.episodes_requested or r.episodes_total
            if n_eps:
                parts.append(f"{n_eps} eps")
        return " · ".join(parts)

    if r.seerr_media_id == 0:
        return "·", "dim", "Not requested", ""
    if code == 5:
        detail = " · ".join(p for p in [_season_str(progress=True), by_date] if p)
        return "✓", "green", "Available", detail
    if code == 4:
        detail = " · ".join(p for p in [_season_str(progress=True), by_date] if p)
        return "✓", "cyan", "Partially available", detail
    if req == 3:
        detail = " · ".join(p for p in [_season_str(), by_date] if p)
        return "✗", "red", "Declined", detail
    if req == 2:
        detail = " · ".join(p for p in [_season_str(), by_date] if p)
        return "⟳", "cyan", "Approved — processing", detail
    if req == 1:
        detail = " · ".join(p for p in [_season_str(), by_date] if p)
        return "·", "yellow", "Pending approval", detail
    if code == 3:
        detail = " · ".join(p for p in [_season_str(), by_date] if p)
        return "⟳", "cyan", "Requested — processing", detail
    if r.seerr_media_id != 0:
        detail = " · ".join(p for p in [_season_str(), by_date] if p)
        return "⟳", "dim", "Requested", detail
    return "?", "dim", "Unknown", ""


def _stage_arr(record) -> tuple[str, str, str, str]:
    """Returns (icon, style, summary, detail) for the Radarr/Sonarr stage."""
    if isinstance(record, ArrMovieRecord):
        if record.has_file:
            parts = [p for p in [record.quality, _fmt_bytes(record.size_bytes) if record.size_bytes else ""] if p]
            return "✓", "green", "Has file", " · ".join(parts)
        if record.monitored:
            return "⟳", "yellow", "Monitored — missing", ""
        return "·", "dim", "Not monitored", ""
    # ArrSeriesRecord
    if record.episode_file_count > 0:
        eps = f"{record.episode_file_count}/{record.episode_count} eps"
        pct = f"{record.percent:.0f}%"
        style = "green" if record.percent >= 100 else "cyan"
        return "✓", style, eps, pct
    if record.monitored:
        return "⟳", "yellow", "Monitored — no files", ""
    return "·", "dim", "Not monitored", ""


def _stage_download(queue_items, arr_record, import_date: str) -> tuple[str, str, str, str]:
    """Returns (icon, style, summary, detail) for the download stage."""
    if queue_items:
        item = queue_items[0]
        state = (item.tracked_state or "").lower()
        if "import" in state:
            return "⟳", "cyan", "Importing…", ""
        if "failed" in state or item.status in ("warning", "error"):
            return "✗", "red", "Failed", (item.error_message or "")[:80]
        # Seeding: torrent client still uploading after import
        # Detected when: explicit completed status, size_left==0, or files already
        # exist in the arr library (most reliable — sizeleft stays full during seeding)
        already_imported = (
            (isinstance(arr_record, ArrMovieRecord) and arr_record.has_file)
            or (isinstance(arr_record, ArrSeriesRecord) and arr_record.episode_file_count > 0)
            or bool(import_date)
        )
        if item.status == "completed" or item.size_left == 0 or already_imported:
            size_str = _fmt_bytes(item.size_bytes) if item.size_bytes else ""
            return "⬆", "yellow", f"Seeding{' · ' + size_str if size_str else ''}", ""
        pct = item.progress_pct
        bar = _mini_bar(pct)
        parts: list[str] = [f"{pct:.1f}% {bar}"]
        if item.size_bytes:
            parts.append(f"{_fmt_bytes(item.size_bytes - item.size_left)} / {_fmt_bytes(item.size_bytes)}")
        if item.time_left:
            parts.append(f"ETA {item.time_left}")
        return "⟳", "cyan", " · ".join(parts), ""

    if import_date:
        return "✓", "green", "Imported", import_date
    if isinstance(arr_record, ArrMovieRecord) and arr_record.has_file:
        return "✓", "green", "Imported", arr_record.date_added or ""
    if isinstance(arr_record, ArrSeriesRecord) and arr_record.episode_file_count > 0:
        return "✓", "green", "Files imported", ""
    return "·", "dim", "—", ""


def _render_plex_row(plex_match, plex_err: str = "") -> None:
    """Render stage 4 Plex row from a PlexMatch (or error string)."""
    if plex_err:
        _pipeline_row(4, "Plex", "?", "yellow", "Error", plex_err)
        return
    if plex_match is None:
        _pipeline_row(4, "Plex", "·", "dim", "Not checked", "")
        return
    if plex_match.error:
        _pipeline_row(4, "Plex", "?", "yellow", "Error", plex_match.error)
        return
    if not plex_match.found:
        _pipeline_row(4, "Plex", "✗", "red", "Not in Plex", "")
        return
    parts = [f'library "{plex_match.library}"']
    if plex_match.episode_count:
        parts.append(f"{plex_match.episode_count} eps")
    if plex_match.added_at:
        parts.append(f"added {plex_match.added_at}")
    _pipeline_row(4, "Plex", "✓", "green", "Available", " · ".join(parts))


def _seerr_pick(cfg, query: str):
    """Search Seerr and return the chosen SeerrSearchResult, or None on cancel."""
    if cfg.seerr is None:
        console.print("[red]No seerr section in config.yaml[/]")
        return None
    with console.status("[bold]Searching Seerr…[/]"):
        try:
            results = seerr_search(cfg.seerr, query)
        except Exception as e:
            console.print(f"[red]Search error:[/] {e}")
            return None
    if not results:
        console.print("[dim]No results.[/]")
        return None
    if len(results) == 1:
        return results[0]
    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    t.add_column("#", justify="right", style="dim", width=3)
    t.add_column("Type", style="dim")
    t.add_column("Title", style="bold")
    t.add_column("Year")
    t.add_column("Status")
    for i, r in enumerate(results[:10], 1):
        t.add_row(str(i), r.media_type, r.title, r.year, r.status)
    console.print(t)
    n = typer.prompt("Pick a number", type=int, default=1)
    if not 1 <= n <= min(len(results), 10):
        console.print("[red]Invalid choice[/]")
        return None
    return results[n - 1]


def _display_pipeline(cfg, pick) -> tuple:
    """Render all 4 pipeline stages for a SeerrSearchResult.

    Returns (arr_record, plex_match, is_movie).
    """
    is_movie = pick.media_type == "movie"
    arr_cfg  = cfg.radarr if is_movie else cfg.sonarr
    arr_label = "Radarr" if is_movie else "Sonarr"

    console.print()
    console.print(f"  [bold]{pick.title}[/] [dim]({pick.year}) · {pick.media_type} · TMDB {pick.id}[/]")
    console.print()

    # ── Stage 1: Seerr ────────────────────────────────────────────────────────
    if not is_movie and cfg.seerr is not None and pick.seerr_media_id != 0:
        with console.status("[bold]Fetching season details…[/]"):
            seerr_enrich_tv_seasons(cfg.seerr, pick)
    icon, style, summary, detail = _stage_seerr(pick)
    _pipeline_row(1, "Seerr Request", icon, style, summary, detail)

    # ── Stage 2: Radarr / Sonarr ──────────────────────────────────────────────
    arr_record = None
    arr_queue: list = []
    import_date = ""

    if arr_cfg is None:
        _pipeline_row(2, arr_label, "·", "dim", "Not configured", "add to config.yaml")
    else:
        with console.status(f"[bold]Querying {arr_label}…[/]"):
            try:
                if is_movie:
                    arr_record = get_movie_by_tmdb(arr_cfg, pick.id)
                else:
                    if pick.external_service_id:
                        arr_record = get_series_by_id(arr_cfg, pick.external_service_id)
                    if arr_record is None:
                        arr_record = get_series_by_title(arr_cfg, pick.title)
                arr_queue = arr_get_queue(arr_cfg)
                if arr_record and is_movie:
                    import_date = get_movie_import_date(arr_cfg, arr_record.arr_id)
            except Exception as e:
                console.print(f"  [dim]  ({arr_label} error: {e})[/]")

        if arr_record is None:
            _pipeline_row(2, arr_label, "✗", "dim", "Not in library", "")
        else:
            icon, style, summary, detail = _stage_arr(arr_record)
            _pipeline_row(2, arr_label, icon, style, summary, detail)

    # ── Stage 3: Download ─────────────────────────────────────────────────────
    queue_items = []
    if arr_record and arr_queue:
        if is_movie and isinstance(arr_record, ArrMovieRecord):
            queue_items = [q for q in arr_queue if q.movie_id == arr_record.arr_id]
        elif not is_movie and isinstance(arr_record, ArrSeriesRecord):
            queue_items = [q for q in arr_queue if q.series_id == arr_record.arr_id]

    icon, style, summary, detail = _stage_download(queue_items, arr_record, import_date)
    _pipeline_row(3, "Download", icon, style, summary, detail)

    # ── Stage 4: Plex ─────────────────────────────────────────────────────────
    plex_match = None
    plex_err = ""
    if cfg.plex is None:
        _pipeline_row(4, "Plex", "·", "dim", "Not configured", "add plex: to config.yaml")
    else:
        with console.status("[bold]Checking Plex…[/]"):
            try:
                plex_year = int(pick.year or 0)
                is_tv = pick.media_type == "tv"
                plex_match = plex_find_media(
                    cfg.plex, pick.title, pick.media_type,
                    year=plex_year,
                    tvdb_id=pick.tvdb_id if is_tv else 0,
                    tmdb_id=pick.id if not is_tv else 0,
                )
                if not plex_match.found and plex_year:
                    plex_match = plex_find_media(
                        cfg.plex, pick.title, pick.media_type,
                        year=0,
                        tvdb_id=pick.tvdb_id if is_tv else 0,
                        tmdb_id=pick.id if not is_tv else 0,
                    )
            except Exception as e:
                plex_match = None
                plex_err = str(e)

        _render_plex_row(plex_match, plex_err if plex_match is None else "")

    console.print()
    return arr_record, plex_match, is_movie


def _pipeline_interactive_menu(cfg, arr_record, plex_match, is_movie) -> None:
    """Show a post-pipeline action menu and execute the chosen action."""
    arr_cfg = cfg.radarr if is_movie else cfg.sonarr
    actions: list[tuple[str, str]] = []

    if ENABLE_ARR_SEARCH and arr_record is not None and arr_cfg is not None:
        if isinstance(arr_record, ArrMovieRecord) and not arr_record.has_file and arr_record.monitored:
            actions.append(("search", "Search for missing movie in Radarr"))
        elif (
            isinstance(arr_record, ArrSeriesRecord)
            and arr_record.monitored
            and arr_record.episode_count > 0
            and arr_record.episode_file_count < arr_record.episode_count
        ):
            missing = arr_record.episode_count - arr_record.episode_file_count
            actions.append(("search", f"Search for {missing} missing episode(s) in Sonarr"))

    if (
        cfg.plex is not None
        and plex_match is not None
        and not plex_match.found
        and not plex_match.error
    ):
        actions.append(("plex_scan", "Scan Plex libraries"))

    if not actions:
        return

    action_keys = {key: key for key, _ in actions}
    parts = []
    if "search" in action_keys:
        parts.append("Search(s)")
    if "plex_scan" in action_keys:
        parts.append("Plex Scan(p)")
    parts.append("Nothing(n)")

    console.print()
    raw = typer.prompt(", ".join(parts), default="n").strip().lower()

    if raw in ("n", ""):
        return
    action = "search" if raw == "s" else "plex_scan" if raw == "p" else ""
    if not action or action not in action_keys:
        return

    if action == "search":
        if is_movie and isinstance(arr_record, ArrMovieRecord):
            with console.status("[bold]Triggering Radarr search…[/]"):
                try:
                    _arr_result(radarr_search_movies(arr_cfg, [arr_record.arr_id]))
                except Exception as e:
                    console.print(f"[red]Error:[/] {e}")
        elif not is_movie and isinstance(arr_record, ArrSeriesRecord):
            with console.status("[bold]Triggering Sonarr search…[/]"):
                try:
                    _arr_result(sonarr_search_series(arr_cfg, arr_record.arr_id))
                except Exception as e:
                    console.print(f"[red]Error:[/] {e}")

    elif action == "plex_scan":
        libs = plex_list_libraries(cfg.plex)
        if isinstance(libs, str):
            console.print(f"[red]Error listing libraries:[/] {libs}")
            return
        lib_type = "movie" if is_movie else "show"
        to_scan = [lib for lib in libs if lib.lib_type == lib_type]
        if not to_scan:
            console.print(f"[red]No {lib_type} libraries found in Plex[/]")
            return
        for lib in to_scan:
            with console.status(f"[bold]Scanning '{lib.title}'…[/]"):
                err = plex_scan_library(cfg.plex, lib.key)
            if err:
                console.print(f"[red]✗[/] {lib.title}: {err}")
            else:
                console.print(f"[green]✓[/] Scan queued for '{lib.title}'")


def _display_pipeline_from_calendar(cfg, item) -> tuple:
    """Render the pipeline for a CalendarMovie or CalendarEpisode.

    Bypasses Seerr search disambiguation — arr item is already known.
    Seerr is queried silently (exact title match) only for request status.
    Returns (arr_record, plex_match, is_movie).
    """
    from lib.arr import CalendarMovie as _CM, CalendarEpisode as _CE
    is_movie   = isinstance(item, _CM)
    arr_cfg    = cfg.radarr if is_movie else cfg.sonarr
    arr_label  = "Radarr" if is_movie else "Sonarr"
    media_type = "movie"  if is_movie else "tv"
    title      = item.title if is_movie else item.series_title

    console.print()
    if is_movie:
        console.print(f"  [bold]{item.title}[/] [dim]({item.year}) · movie[/]")
    else:
        ep_tag = f"S{item.season:02d}E{item.episode:02d}"
        console.print(f"  [bold]{item.series_title}[/] [dim]· tv · {ep_tag} {item.title}[/]")
    console.print()

    # ── Stage 1: Seerr (silent — no disambiguation prompt) ────────────────────
    if cfg.seerr is None:
        _pipeline_row(1, "Seerr Request", "·", "dim", "Not configured", "")
        seerr_result = None
    else:
        seerr_result = None
        try:
            with console.status("[bold]Checking Seerr…[/]"):
                results = seerr_search(cfg.seerr, title)
            # Prefer exact title match of correct type; fall back to first of correct type
            seerr_result = next(
                (r for r in results if r.media_type == media_type and r.title.lower() == title.lower()),
                next((r for r in results if r.media_type == media_type), None),
            )
        except Exception:
            pass
        if seerr_result is None:
            _pipeline_row(1, "Seerr Request", "·", "dim", "Not requested", "")
        else:
            if media_type == "tv" and seerr_result.seerr_media_id != 0:
                with console.status("[bold]Fetching season details…[/]"):
                    seerr_enrich_tv_seasons(cfg.seerr, seerr_result)
            icon, style, summary, detail = _stage_seerr(seerr_result)
            _pipeline_row(1, "Seerr Request", icon, style, summary, detail)

    # ── Stage 2: Arr (use known IDs — no extra search needed) ─────────────────
    arr_record = None
    arr_queue: list = []
    import_date = ""

    if arr_cfg is None:
        _pipeline_row(2, arr_label, "·", "dim", "Not configured", "add to config.yaml")
    else:
        with console.status(f"[bold]Querying {arr_label}…[/]"):
            try:
                if is_movie:
                    arr_record = get_movie_by_id(arr_cfg, item.arr_id) if item.arr_id else None
                    if arr_record is None and item.tmdb_id:
                        arr_record = get_movie_by_tmdb(arr_cfg, item.tmdb_id)
                else:
                    arr_record = get_series_by_id(arr_cfg, item.series_id) if item.series_id else None
                    if arr_record is None:
                        arr_record = get_series_by_title(arr_cfg, title)
                arr_queue = arr_get_queue(arr_cfg)
                if arr_record and is_movie:
                    import_date = get_movie_import_date(arr_cfg, arr_record.arr_id)
            except Exception as e:
                console.print(f"  [dim]  ({arr_label} error: {e})[/]")

        if arr_record is None:
            _pipeline_row(2, arr_label, "✗", "dim", "Not in library", "")
        else:
            icon, style, summary, detail = _stage_arr(arr_record)
            _pipeline_row(2, arr_label, icon, style, summary, detail)

    # ── Stage 3: Download ─────────────────────────────────────────────────────
    queue_items = []
    if arr_record and arr_queue:
        if is_movie and isinstance(arr_record, ArrMovieRecord):
            queue_items = [q for q in arr_queue if q.movie_id == arr_record.arr_id]
        elif not is_movie and isinstance(arr_record, ArrSeriesRecord):
            queue_items = [q for q in arr_queue if q.series_id == arr_record.arr_id]

    icon, style, summary, detail = _stage_download(queue_items, arr_record, import_date)
    _pipeline_row(3, "Download", icon, style, summary, detail)

    # ── Stage 4: Plex ─────────────────────────────────────────────────────────
    plex_match = None
    plex_err = ""
    if cfg.plex is None:
        _pipeline_row(4, "Plex", "·", "dim", "Not configured", "add plex: to config.yaml")
    else:
        with console.status("[bold]Checking Plex…[/]"):
            try:
                plex_match = plex_find_media(cfg.plex, title, media_type)
            except Exception as e:
                plex_match = None
                plex_err = str(e)

        _render_plex_row(plex_match, plex_err if plex_match is None else "")

    console.print()
    return arr_record, plex_match, is_movie


@media_app.command("pipeline")
def pipeline_cmd(
    query: str = typer.Argument(..., help="Movie or TV show title to look up"),
) -> None:
    """Show the complete media pipeline: request → *arr → download → Plex."""
    cfg  = _load()
    pick = _seerr_pick(cfg, query)
    if pick is None:
        raise typer.Exit(1)
    arr_record, plex_match, is_movie = _display_pipeline(cfg, pick)
    _pipeline_interactive_menu(cfg, arr_record, plex_match, is_movie)


# ── radarr / sonarr ───────────────────────────────────────────────────────────

from lib.search_state import load_state as _load_search_state
from lib.arr import (
    get_status as arr_get_status,
    radarr_scan, radarr_search_missing, radarr_search_movies,
    sonarr_scan, sonarr_search_missing, sonarr_search_episodes, sonarr_search_series,
    get_episode_id,
    get_today_imports as arr_get_today_imports,
)


def _arr_result(cmd) -> None:
    """Print the result of a dispatched Servarr command."""
    console.print(
        f"[green]✓[/] Command [bold]{cmd.name}[/] queued "
        f"(id={cmd.command_id}, status={cmd.status})"
        + (f" — {cmd.message}" if cmd.message else "")
    )


# ── radarr ────────────────────────────────────────────────────────────────────

radarr_app = typer.Typer(
    help="Manage Radarr (movies)",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
media_app.add_typer(radarr_app, name="radarr")


def _radarr(cfg: Config):
    if cfg.radarr is None:
        console.print("[red]No radarr section in config.yaml[/]")
        raise typer.Exit(1)
    return cfg.radarr


@radarr_app.command("status")
def radarr_status_cmd() -> None:
    """Show Radarr version and connectivity."""
    cfg = _load()
    with console.status("[bold]Querying Radarr…[/]"):
        s = arr_get_status(_radarr(cfg))
    if s.online:
        console.print(f"[green]✓[/] {s.app_name} {s.version} online")
    else:
        console.print(f"[red]✗[/] Radarr offline: {s.error}")


@radarr_app.command("scan")
def radarr_scan_cmd() -> None:
    """Trigger a full library rescan."""
    cfg = _load()
    with console.status("[bold]Triggering Radarr scan…[/]"):
        try:
            _arr_result(radarr_scan(_radarr(cfg)))
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")
            raise typer.Exit(1)


@radarr_app.command("search-missing")
def radarr_search_missing_cmd(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Search for all missing movies."""
    if not ENABLE_ARR_SEARCH:
        console.print("[yellow]⚑[/] Arr search is disabled (ENABLE_ARR_SEARCH=False).")
        raise typer.Exit(0)
    if not yes:
        if not Confirm.ask("Trigger missing movies search? (may be slow)"):
            raise typer.Exit(0)
    cfg = _load()
    state = _load_search_state(cfg.config_path)
    allowed, reason = state.can_search("missing_movies", broad=True)
    if not allowed:
        console.print(f"[yellow]⏸[/] Search throttled — {reason}")
        raise typer.Exit(0)
    with console.status("[bold]Triggering Radarr missing search…[/]"):
        try:
            _arr_result(radarr_search_missing(_radarr(cfg)))
            state.record_search("missing_movies", broad=True)
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")
            raise typer.Exit(1)


# ── sonarr ────────────────────────────────────────────────────────────────────

sonarr_app = typer.Typer(
    help="Manage Sonarr (TV shows)",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
media_app.add_typer(sonarr_app, name="sonarr")


def _sonarr(cfg: Config):
    if cfg.sonarr is None:
        console.print("[red]No sonarr section in config.yaml[/]")
        raise typer.Exit(1)
    return cfg.sonarr


@sonarr_app.command("status")
def sonarr_status_cmd() -> None:
    """Show Sonarr version and connectivity."""
    cfg = _load()
    with console.status("[bold]Querying Sonarr…[/]"):
        s = arr_get_status(_sonarr(cfg))
    if s.online:
        console.print(f"[green]✓[/] {s.app_name} {s.version} online")
    else:
        console.print(f"[red]✗[/] Sonarr offline: {s.error}")


@sonarr_app.command("scan")
def sonarr_scan_cmd() -> None:
    """Trigger a full library rescan."""
    cfg = _load()
    with console.status("[bold]Triggering Sonarr scan…[/]"):
        try:
            _arr_result(sonarr_scan(_sonarr(cfg)))
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")
            raise typer.Exit(1)


@sonarr_app.command("search-missing")
def sonarr_search_missing_cmd(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Search for all missing monitored episodes."""
    if not ENABLE_ARR_SEARCH:
        console.print("[yellow]⚑[/] Arr search is disabled (ENABLE_ARR_SEARCH=False).")
        raise typer.Exit(0)
    if not yes:
        if not Confirm.ask("Trigger missing episodes search? (may be slow)"):
            raise typer.Exit(0)
    cfg = _load()
    state = _load_search_state(cfg.config_path)
    allowed, reason = state.can_search("missing_episodes", broad=True)
    if not allowed:
        console.print(f"[yellow]⏸[/] Search throttled — {reason}")
        raise typer.Exit(0)
    with console.status("[bold]Triggering Sonarr missing search…[/]"):
        try:
            _arr_result(sonarr_search_missing(_sonarr(cfg)))
            state.record_search("missing_episodes", broad=True)
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")
            raise typer.Exit(1)


# ── seerr ─────────────────────────────────────────────────────────────────────

from lib.seerr import (
    search as seerr_search,
    request_media as seerr_request_media,
    list_requests as seerr_list_requests,
    enrich_tv_seasons as seerr_enrich_tv_seasons,
    MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV,
)

seerr_app = typer.Typer(
    help="Search and request media via Seerr",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
media_app.add_typer(seerr_app, name="seerr")


def _seerr(cfg: Config):
    if cfg.seerr is None:
        console.print("[red]No seerr section in config.yaml[/]")
        raise typer.Exit(1)
    return cfg.seerr


@seerr_app.command("search")
def seerr_search_cmd(
    query: str = typer.Argument(..., help="Movie or TV show title to search for"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """Search for movies and TV shows."""
    if json_flag:
        output = _Out.json
    cfg = _load()
    sc = _seerr(cfg)

    with console.status("[bold]Searching…[/]"):
        try:
            results = seerr_search(sc, query)
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")
            raise typer.Exit(1)

    if not results:
        console.print("[dim]No results.[/]")
        return

    if output == _Out.json:
        console.print_json(_json.dumps([
            {"id": r.id, "type": r.media_type, "title": r.title, "year": r.year,
             "overview": r.overview, "status": r.status}
            for r in results
        ]))
        return

    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    t.add_column("ID", justify="right", style="dim")
    t.add_column("Type", style="dim")
    t.add_column("Title", style="bold")
    t.add_column("Year")
    t.add_column("Status")
    if output == _Out.wide:
        t.add_column("Overview")

    for r in results:
        status_style = {
            "available": "green",
            "pending": "yellow",
            "processing": "cyan",
            "partially available": "cyan",
        }.get(r.status, "dim")
        row = [str(r.id), r.media_type, r.title, r.year,
               f"[{status_style}]{r.status}[/]"]
        if output == _Out.wide:
            row.append(r.overview)
        t.add_row(*row)

    console.print(t)
    console.print(f"[dim]{len(results)} result(s) — use ID with [bold]atc media seerr request <id> --type movie|tv[/][/]")


@seerr_app.command("request")
def seerr_request_cmd(
    tmdb_id: int = typer.Argument(..., help="TMDB ID from search results"),
    media_type: str = typer.Option(..., "--type", "-t", help="'movie' or 'tv'"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Request a movie or TV show by TMDB ID."""
    if media_type not in (MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV):
        console.print(f"[red]--type must be 'movie' or 'tv'[/]")
        raise typer.Exit(1)

    cfg = _load()
    sc = _seerr(cfg)

    if not yes:
        confirmed = Confirm.ask(f"Request [bold]{media_type}[/] with TMDB ID [bold]{tmdb_id}[/]?")
        if not confirmed:
            raise typer.Exit(0)

    with console.status("[bold]Submitting request…[/]"):
        try:
            result = seerr_request_media(sc, tmdb_id, media_type)
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")
            raise typer.Exit(1)

    console.print(
        f"[green]✓[/] Requested [bold]{result.title}[/] "
        f"(#{result.request_id}) — status: [bold]{result.status}[/]"
    )


def _seerr_list_body(
    show_all: bool,
    tv_only: bool,
    movies_only: bool,
    processing_only: bool,
    output: _Out,
) -> None:
    cfg = _load()
    sc = _seerr(cfg)

    with console.status("[bold]Fetching requests…[/]"):
        try:
            reqs = seerr_list_requests(sc)
        except Exception as e:
            console.print(f"[red]Error:[/] {e}")
            raise typer.Exit(1)

    if not show_all:
        reqs = [r for r in reqs if r.media_status_code != 5]
    if tv_only:
        reqs = [r for r in reqs if r.media_type == "tv"]
    if movies_only:
        reqs = [r for r in reqs if r.media_type == "movie"]
    if processing_only:
        reqs = [r for r in reqs if r.request_status_code == 2 and r.media_status_code != 5]

    if not reqs:
        console.print("[dim]No requests.[/]")
        return

    if output == _Out.json:
        console.print_json(_json.dumps([
            {
                "id": r.request_id, "type": r.media_type, "title": r.title,
                "tmdb_id": r.tmdb_id, "request_status": r.request_status,
                "media_status": r.media_status, "requested_by": r.requested_by,
                "created_at": r.created_at,
            }
            for r in reqs
        ]))
        return

    _REQ_STYLE = {"pending": "yellow", "approved": "green", "declined": "red", "failed": "red"}
    _MED_STYLE = {
        "available": "green", "partially available": "cyan",
        "processing": "cyan", "pending": "yellow",
    }

    t = Table(box=box.SIMPLE, show_header=True, pad_edge=False)
    t.add_column("#", justify="right", style="dim")
    t.add_column("Type", style="dim")
    t.add_column("Title", style="bold")
    t.add_column("Req'd By")
    t.add_column("Date")
    t.add_column("Request")
    t.add_column("Media")

    for r in reqs:
        req_s = _REQ_STYLE.get(r.request_status, "dim")
        med_s = _MED_STYLE.get(r.media_status, "dim")
        t.add_row(
            str(r.request_id),
            r.media_type,
            r.title,
            r.requested_by,
            r.created_at,
            f"[{req_s}]{r.request_status}[/]",
            f"[{med_s}]{r.media_status}[/]",
        )

    console.print(t)
    console.print(f"[dim]{len(reqs)} request(s)[/]")


@seerr_app.command("list")
def seerr_list_cmd(
    show_all: bool = typer.Option(False, "--all", "-a", help="Include fulfilled (available) requests"),
    tv_only: bool = typer.Option(False, "--tv", "-t", help="Series only"),
    movies_only: bool = typer.Option(False, "--movies", "-m", help="Movies only"),
    processing_only: bool = typer.Option(False, "--processing", "-p", help="Approved and in-flight (not yet available)"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """List media requests. Unfulfilled only by default.

    [dim]Filters: [bold]-t[/] tv  [bold]-m[/] movies  [bold]-p[/] processing  [bold]-a[/] all[/]
    """
    if json_flag:
        output = _Out.json
    _seerr_list_body(show_all, tv_only, movies_only, processing_only, output)


@seerr_app.command("ls", hidden=True)
def seerr_ls_cmd(
    show_all: bool = typer.Option(False, "--all", "-a", help="Include fulfilled (available) requests"),
    tv_only: bool = typer.Option(False, "--tv", "-t", help="Series only"),
    movies_only: bool = typer.Option(False, "--movies", "-m", help="Movies only"),
    processing_only: bool = typer.Option(False, "--processing", "-p", help="Approved and in-flight (not yet available)"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """Alias for [bold]list[/]."""
    if json_flag:
        output = _Out.json
    _seerr_list_body(show_all, tv_only, movies_only, processing_only, output)


# ── media calendar ────────────────────────────────────────────────────────────

from datetime import date as _date, timedelta as _timedelta
from lib.arr import (
    get_radarr_calendar, get_sonarr_calendar,
    CalendarMovie, CalendarEpisode,
)


@media_app.command("calendar")
def media_calendar_cmd(
    days: int = typer.Argument(7, help="Days to show: positive = ahead, negative = past (default 7)"),
    radarr: bool = typer.Option(False, "--radarr", help="Show only movies"),
    sonarr: bool = typer.Option(False, "--sonarr", help="Show only TV episodes"),
) -> None:
    """Show Radarr and Sonarr releases. Positive days = upcoming, negative = past."""
    cfg = _load()
    show_all = not any([radarr, sonarr])

    today = _date.today()
    if days >= 0:
        start, end = today, today + _timedelta(days=days)
    else:
        start, end = today + _timedelta(days=days), today

    movies:   list[CalendarMovie]   = []
    episodes: list[CalendarEpisode] = []

    if (show_all or radarr) and cfg.radarr:
        with console.status("[bold]Fetching Radarr calendar…[/]"):
            movies = get_radarr_calendar(cfg.radarr, start, end)
    elif show_all or radarr:
        console.print("[dim]Radarr not configured — skipping[/]")

    if (show_all or sonarr) and cfg.sonarr:
        with console.status("[bold]Fetching Sonarr calendar…[/]"):
            episodes = get_sonarr_calendar(cfg.sonarr, start, end)
    elif show_all or sonarr:
        console.print("[dim]Sonarr not configured — skipping[/]")

    if not movies and not episodes:
        if days >= 0:
            console.print(f"[dim]Nothing scheduled in the next {days} day(s).[/]")
        else:
            console.print(f"[dim]Nothing found in the past {abs(days)} day(s).[/]")
        return

    # Merge and group by date; keep a flat numbered list for selection
    from collections import defaultdict
    by_date: dict[str, list] = defaultdict(list)
    for m in movies:
        by_date[m.release_date].append(m)
    for e in episodes:
        by_date[e.air_date].append(e)

    if days >= 0:
        console.rule(f"[bold]Upcoming[/]  [dim]next {days} days · {start} → {end}[/]")
    else:
        console.rule(f"[bold]Past releases[/]  [dim]last {abs(days)} days · {start} → {end}[/]")
    console.print()

    numbered: list = []   # flat ordered list matching displayed numbers
    for day in sorted(by_date):
        d = _date.fromisoformat(day)
        label = d.strftime("%a %b %d")
        if d == today:
            label += " [bold yellow](today)[/]"
        elif d == today + _timedelta(days=1):
            label += " [dim](tomorrow)[/]"
        elif d == today - _timedelta(days=1):
            label += " [dim](yesterday)[/]"
        console.print(f"  [bold cyan]{label}[/]")
        for item in by_date[day]:
            numbered.append(item)
            n = len(numbered)
            if isinstance(item, CalendarMovie):
                icon  = "[green]✓[/]" if item.has_file else "[dim]·[/]"
                rtype = f"[dim]{item.release_type}[/]"
                console.print(f"    [dim]{n:>2}.[/] {icon}  [bold]{item.title}[/] [dim]({item.year})[/]  {rtype}  [dim]movie[/]")
            else:
                icon  = "[green]✓[/]" if item.has_file else "[dim]·[/]"
                ep    = f"[dim]S{item.season:02d}E{item.episode:02d}[/]"
                console.print(f"    [dim]{n:>2}.[/] {icon}  [bold]{item.series_title}[/]  {ep}  [dim]{item.title}[/]")

    raw = typer.prompt(
        "Enter # for pipeline details (or Enter to skip)",
        default="",
        show_default=False,
    ).strip()
    if not raw:
        return
    try:
        choice = int(raw)
    except ValueError:
        console.print("[red]Not a number — skipping.[/]")
        return
    if not 1 <= choice <= len(numbered):
        console.print("[red]Out of range — skipping.[/]")
        return

    arr_record, plex_match, is_movie = _display_pipeline_from_calendar(cfg, numbered[choice - 1])
    _pipeline_interactive_menu(cfg, arr_record, plex_match, is_movie)


# ── media status ──────────────────────────────────────────────────────────────

import httpx as _httpx


@media_app.command("status")
def media_status_cmd(
    radarr:    bool = typer.Option(False, "--radarr",    help="Show only Radarr"),
    sonarr:    bool = typer.Option(False, "--sonarr",    help="Show only Sonarr"),
    seerr:     bool = typer.Option(False, "--seerr",     help="Show only Seerr"),
    plex:      bool = typer.Option(False, "--plex",      help="Show only Plex"),
    tautulli:  bool = typer.Option(False, "--tautulli",  help="Show only Tautulli"),
) -> None:
    """Show connectivity status for all media services."""
    cfg = _load()
    # No flags = show all
    show_all = not any([radarr, sonarr, seerr, plex, tautulli])

    console.print()
    console.rule("[bold]🎬  Media Status[/]")
    console.print()

    def _row(name: str, ok: bool, detail: str) -> None:
        icon  = "[green]●[/]" if ok else "[red]●[/]"
        style = "green" if ok else "red"
        console.print(f"   {icon}  [bold]{name:<10}[/]  [{style}]{detail}[/]")

    if show_all or radarr:
        if cfg.radarr is None:
            _row("Radarr", False, "not configured")
        else:
            with console.status("Querying Radarr…"):
                s = arr_get_status(cfg.radarr)
            _row("Radarr", s.online, f"{s.app_name} {s.version}" if s.online else s.error)

    if show_all or sonarr:
        if cfg.sonarr is None:
            _row("Sonarr", False, "not configured")
        else:
            with console.status("Querying Sonarr…"):
                s = arr_get_status(cfg.sonarr)
            _row("Sonarr", s.online, f"{s.app_name} {s.version}" if s.online else s.error)

    if show_all or seerr:
        if cfg.seerr is None:
            _row("Seerr", False, "not configured")
        else:
            try:
                with console.status("Querying Seerr…"):
                    r = _httpx.get(
                        f"{cfg.seerr.base_url}/api/v1/settings/public",
                        verify=cfg.seerr.verify_ssl, timeout=10,
                    )
                if r.is_success:
                    version = r.json().get("applicationVersion", "")
                    _row("Seerr", True, f"v{version}" if version else "online")
                else:
                    _row("Seerr", False, f"HTTP {r.status_code}")
            except Exception as e:
                _row("Seerr", False, str(e))

    if show_all or plex:
        if cfg.plex is None:
            _row("Plex", False, "not configured")
        else:
            try:
                with console.status("Querying Plex…"):
                    r = _httpx.get(
                        f"{cfg.plex.base_url}/identity",
                        headers={"Accept": "application/json"},
                        verify=cfg.plex.verify_ssl, timeout=10,
                    )
                if r.is_success:
                    version = (r.json().get("MediaContainer") or {}).get("version", "")
                    _row("Plex", True, f"v{version}" if version else "online")
                else:
                    _row("Plex", False, f"HTTP {r.status_code}")
            except Exception as e:
                _row("Plex", False, str(e))

    if show_all or tautulli:
        if cfg.tautulli is None:
            _row("Tautulli", False, "not configured")
        else:
            from lib.tautulli import get_status as _tautulli_get_status
            with console.status("Querying Tautulli…"):
                st = _tautulli_get_status(cfg.tautulli)
            if st.online:
                n = st.stream_count
                streams_str = f"{n} active stream{'s' if n != 1 else ''}"
                _row("Tautulli", True, streams_str)
            else:
                _row("Tautulli", False, st.error)

    console.print()
    console.rule()
    console.print()


# ── media today ───────────────────────────────────────────────────────────────


@media_app.command("today")
def media_today_cmd() -> None:
    """Today's media — scheduled releases, requests, and downloads with file details."""
    cfg = _load()
    today = _date.today()

    import re as _re
    _year_sfx = _re.compile(r"\s*\((\d{4})\)$")

    def _strip_year(raw: str) -> tuple[str, int]:
        m = _year_sfx.search(raw)
        return (raw[:m.start()], int(m.group(1))) if m else (raw, 0)

    # ── 1. Fetch ────────────────────────────────────────────────────────────────
    cal_movies: list[CalendarMovie] = []
    cal_episodes: list[CalendarEpisode] = []
    radarr_queue: list = []
    sonarr_queue: list = []
    radarr_imports: list = []
    sonarr_imports: list = []
    seerr_reqs: list = []

    if cfg.radarr:
        with console.status("[bold]Fetching Radarr…[/]"):
            cal_movies = get_radarr_calendar(cfg.radarr, today, today)
            radarr_queue = arr_get_queue(cfg.radarr)
            radarr_imports = arr_get_today_imports(cfg.radarr, today)
    if cfg.sonarr:
        with console.status("[bold]Fetching Sonarr…[/]"):
            cal_episodes = get_sonarr_calendar(cfg.sonarr, today, today)
            sonarr_queue = arr_get_queue(cfg.sonarr)
            sonarr_imports = arr_get_today_imports(cfg.sonarr, today)
    if cfg.seerr:
        try:
            with console.status("[bold]Fetching Seerr…[/]"):
                all_seerr = seerr_list_requests(cfg.seerr)
            seerr_reqs = [r for r in all_seerr if r.created_at == today.isoformat()]
        except Exception:
            pass

    all_imports = radarr_imports + sonarr_imports

    # ── 2. Header ───────────────────────────────────────────────────────────────
    console.rule(f"[bold]📅  Today  ·  {today}[/]")

    # ── 3. Releases ─────────────────────────────────────────────────────────────
    console.print()
    console.print("  [bold cyan]Releases[/]")
    if cal_movies:
        console.print("  [cyan]Movies[/]")
        for m in cal_movies:
            in_q = bool(m.arr_id and any(q.movie_id == m.arr_id for q in radarr_queue))
            if m.has_file:
                status = "[green]downloaded[/]"
            elif in_q:
                q_item = next((q for q in radarr_queue if q.movie_id == m.arr_id), None)
                pct = f" {q_item.progress_pct:.0f}%" if q_item else ""
                status = f"[cyan]downloading{pct}[/]"
            else:
                status = "[dim]not found[/]"
            console.print(f"    [bold]{m.title}[/] [dim]({m.year}) · {m.release_type}[/]  [dim]—[/] {status}")
    if cal_episodes:
        console.print("  [cyan]TV[/]")
        for e in cal_episodes:
            title, _ = _strip_year(e.series_title)
            ep = f"S{e.season:02d}E{e.episode:02d}"
            in_q = bool(e.series_id and any(q.series_id == e.series_id for q in sonarr_queue))
            if e.has_file:
                status = "[green]downloaded[/]"
            elif in_q:
                status = "[cyan]downloading[/]"
            else:
                status = "[dim]not found[/]"
            console.print(f"    [bold]{title}[/]  [dim]{ep} {e.title}[/]  [dim]—[/] {status}")
    if not cal_movies and not cal_episodes:
        console.print("  [dim]Nothing scheduled for today.[/]")

    # ── 4. Requests ─────────────────────────────────────────────────────────────
    console.print()
    console.print("  [bold cyan]Requests[/]")
    req_movies = [r for r in seerr_reqs if r.media_type == "movie"]
    req_tv     = [r for r in seerr_reqs if r.media_type != "movie"]

    def _req_line(req) -> str:
        # Media moving past "unknown" means the request was acted on, even if
        # Jellyseerr left the approval status as "pending" (auto-approve quirk).
        approved = req.request_status_code >= 2 or req.media_status_code > 1
        rs_color = "green" if approved else "yellow"
        rs_label = "approved" if approved else req.request_status
        ms_color = {5: "green", 4: "yellow", 3: "cyan"}.get(req.media_status_code, "dim")
        by = f"  [dim]by {req.requested_by}[/]" if req.requested_by else ""
        return f"[{rs_color}]{rs_label}[/] [dim]·[/] [{ms_color}]{req.media_status}[/]{by}"

    if req_movies:
        console.print("  [cyan]Movies[/]")
        for r in req_movies:
            console.print(f"    [bold]{r.title}[/]  {_req_line(r)}")
    if req_tv:
        console.print("  [cyan]TV[/]")
        for r in req_tv:
            console.print(f"    [bold]{r.title}[/]  {_req_line(r)}")
    if not seerr_reqs:
        console.print("  [dim]No requests today.[/]")

    # ── 5. Downloads ─────────────────────────────────────────────────────────────
    console.print()
    console.print("  [bold cyan]Downloads[/]")
    dl_movies = [imp for imp in all_imports if imp.media_type == "movie"]
    dl_tv     = [imp for imp in all_imports if imp.media_type == "episode"]

    # Plex — batch unique titles to avoid N+1
    plex_cache: dict[str, "PlexMatch"] = {}
    if cfg.plex and (dl_movies or dl_tv):
        seen: set[str] = set()
        to_check: list[tuple[str, str, int]] = []
        for imp in dl_movies:
            clean, yr = _strip_year(imp.title)
            if clean.lower() not in seen:
                seen.add(clean.lower())
                to_check.append((clean, "movie", yr or imp.year))
        for imp in dl_tv:
            clean, yr = _strip_year(imp.title)
            if clean.lower() not in seen:
                seen.add(clean.lower())
                to_check.append((clean, "tv", yr or imp.year))
        for title, mtype, year in to_check:
            with console.status(f"[bold]Checking Plex for [cyan]{title}[/]…[/]"):
                match = plex_find_media(cfg.plex, title, mtype, year)
                if not match.found and year:
                    match = plex_find_media(cfg.plex, title, mtype, 0)
            plex_cache[title.lower()] = match

    def _plex_tag(raw_title: str) -> str:
        if not cfg.plex:
            return ""
        clean, _ = _strip_year(raw_title)
        m = plex_cache.get(clean.lower())
        if m is None:
            return ""
        return "  [green]✓ Plex[/]" if m.found else "  [dim]· not in Plex[/]"

    def _file_meta(imp) -> str:
        parts = [p for p in [imp.quality, _fmt_bytes(imp.size_bytes) if imp.size_bytes else ""] if p]
        return f"  [dim]{' · '.join(parts)}[/]" if parts else ""

    if dl_movies:
        console.print("  [cyan]Movies[/]")
        for imp in dl_movies:
            clean, yr = _strip_year(imp.title)
            year_str = f" ({yr or imp.year})" if (yr or imp.year) else ""
            console.print(f"    [bold]{clean}[/][dim]{year_str}[/]{_file_meta(imp)}{_plex_tag(imp.title)}")
    if dl_tv:
        console.print("  [cyan]TV[/]")
        for imp in dl_tv:
            clean, _ = _strip_year(imp.title)
            ep = f"S{imp.season:02d}E{imp.episode:02d}"
            ep_title = f" · {imp.episode_title}" if imp.episode_title else ""
            console.print(f"    [bold]{clean}[/]  [dim]{ep}{ep_title}[/]{_file_meta(imp)}{_plex_tag(imp.title)}")
    if not dl_movies and not dl_tv:
        console.print("  [dim]Nothing downloaded today.[/]")

    console.print()
    console.rule()
    console.print()


@media_app.command("search-today")
def media_search_today_cmd(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be searched without triggering"),
) -> None:
    """Search for today's calendar releases that haven't been downloaded yet.

    Fetches today's Radarr/Sonarr calendar, skips items already downloaded or
    actively queued, then fires a single batch search per service — one
    MoviesSearch command and one EpisodeSearch command.  Uses the search-state
    cooldown so re-running within the cooldown window won't hammer indexers.
    """
    if not ENABLE_ARR_SEARCH:
        console.print("[yellow]⚑[/] Arr search is disabled (ENABLE_ARR_SEARCH=False).")
        raise typer.Exit(0)
    cfg = _load()
    today = _date.today()
    search_state = _load_search_state(cfg.config_path)

    import re as _re
    _year_sfx = _re.compile(r"\s*\((\d{4})\)$")

    def _strip_year(raw: str) -> tuple[str, int]:
        m = _year_sfx.search(raw)
        return (raw[:m.start()], int(m.group(1))) if m else (raw, 0)

    # ── Fetch ─────────────────────────────────────────────────────────────────
    cal_movies: list[CalendarMovie] = []
    cal_episodes: list[CalendarEpisode] = []
    radarr_queue: list = []
    sonarr_queue: list = []

    if cfg.radarr:
        with console.status("[bold]Fetching Radarr calendar…[/]"):
            cal_movies = get_radarr_calendar(cfg.radarr, today, today)
            radarr_queue = arr_get_queue(cfg.radarr)
    if cfg.sonarr:
        with console.status("[bold]Fetching Sonarr calendar…[/]"):
            cal_episodes = get_sonarr_calendar(cfg.sonarr, today, today)
            sonarr_queue = arr_get_queue(cfg.sonarr)

    # ── Filter to actionable items ────────────────────────────────────────────
    # movie: (arr_id, title, year, release_type, skip_reason)
    movie_rows: list[tuple[int, str, int, str, str]] = []
    for m in cal_movies:
        if not m.monitored:
            movie_rows.append((m.arr_id, m.title, m.year, m.release_type, "unmonitored"))
        elif m.has_file:
            movie_rows.append((m.arr_id, m.title, m.year, m.release_type, "downloaded"))
        elif m.arr_id and any(q.movie_id == m.arr_id for q in radarr_queue):
            movie_rows.append((m.arr_id, m.title, m.year, m.release_type, "in queue"))
        else:
            ok, reason = search_state.can_search(f"movie:{m.arr_id}")
            skip = "" if ok else f"cooldown ({reason})"
            movie_rows.append((m.arr_id, m.title, m.year, m.release_type, skip))

    # episode: (episode_id, series_title, season, ep_num, ep_title, skip_reason)
    episode_rows: list[tuple[int, str, int, int, str, str]] = []
    for e in cal_episodes:
        title, _ = _strip_year(e.series_title)
        if not e.monitored:
            episode_rows.append((e.episode_id, title, e.season, e.episode, e.title, "unmonitored"))
        elif e.has_file:
            episode_rows.append((e.episode_id, title, e.season, e.episode, e.title, "downloaded"))
        elif e.series_id and any(q.series_id == e.series_id for q in sonarr_queue):
            episode_rows.append((e.episode_id, title, e.season, e.episode, e.title, "in queue"))
        else:
            ok, reason = (search_state.can_search(f"episode:{e.episode_id}") if e.episode_id
                          else (True, ""))
            skip = "" if ok else f"cooldown ({reason})"
            episode_rows.append((e.episode_id, title, e.season, e.episode, e.title, skip))

    movies_to_search = [arr_id for arr_id, *_, skip in movie_rows if not skip and arr_id]
    episodes_to_search = [ep_id for ep_id, *_, skip in episode_rows if not skip and ep_id]

    # ── Display plan ──────────────────────────────────────────────────────────
    console.print()
    console.rule(f"[bold]🔍  Search Today  ·  {today}[/]")
    console.print()

    if not cal_movies and not cal_episodes:
        console.print("  [dim]Nothing on the calendar for today.[/]")
        console.print()
        console.rule()
        console.print()
        return

    if movie_rows:
        console.print("  [cyan]Movies[/]")
        for arr_id, title, year, release_type, skip in movie_rows:
            label = f"[dim]{release_type}[/]" if release_type else ""
            if skip:
                mark = "[dim]·[/]"
                note = f"  [dim]— {skip}[/]"
            else:
                mark = "[yellow]→[/]"
                note = ""
            console.print(f"    {mark} [bold]{title}[/] [dim]({year})[/]  {label}{note}")

    if episode_rows:
        console.print("  [cyan]TV[/]")
        for ep_id, title, season, ep_num, ep_title, skip in episode_rows:
            ep = f"S{season:02d}E{ep_num:02d}"
            ep_str = f" [dim]· {ep_title}[/]" if ep_title else ""
            if skip:
                mark = "[dim]·[/]"
                note = f"  [dim]— {skip}[/]"
            else:
                mark = "[yellow]→[/]"
                note = ""
            console.print(f"    {mark} [bold]{title}[/]  [dim]{ep}[/]{ep_str}{note}")

    console.print()

    if not movies_to_search and not episodes_to_search:
        console.print("  [dim]Nothing to search — all items are downloaded, queued, or on cooldown.[/]")
        console.print()
        console.rule()
        console.print()
        return

    nm = len(movies_to_search)
    ne = len(episodes_to_search)
    parts = []
    if nm:
        parts.append(f"{nm} movie{'s' if nm != 1 else ''}")
    if ne:
        parts.append(f"{ne} episode{'s' if ne != 1 else ''}")
    summary = " and ".join(parts)

    if dry_run:
        console.print(f"  [dim]Dry run — would search {summary}.[/]")
        console.print()
        return

    if not yes:
        confirm = typer.confirm(f"  Trigger search for {summary}?", default=False)
        if not confirm:
            console.print("  [dim]Aborted.[/]")
            console.print()
            return

    # ── Fire searches ─────────────────────────────────────────────────────────
    if movies_to_search and cfg.radarr:
        try:
            radarr_search_movies(cfg.radarr, movies_to_search)
            for mid in movies_to_search:
                search_state.record_search(f"movie:{mid}")
            console.print(f"  [green]✓[/] Radarr: search triggered for {nm} movie{'s' if nm != 1 else ''}.")
        except Exception as exc:
            console.print(f"  [red]✗[/] Radarr search failed: {exc}")

    if episodes_to_search and cfg.sonarr:
        try:
            sonarr_search_episodes(cfg.sonarr, episodes_to_search)
            for eid in episodes_to_search:
                search_state.record_search(f"episode:{eid}")
            console.print(f"  [green]✓[/] Sonarr: search triggered for {ne} episode{'s' if ne != 1 else ''}.")
        except Exception as exc:
            console.print(f"  [red]✗[/] Sonarr search failed: {exc}")

    console.print()
    console.rule()
    console.print()


# ── plex ──────────────────────────────────────────────────────────────────────

plex_app = typer.Typer(
    help="Manage Plex Media Server, plus stream monitoring via Tautulli",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(plex_app, name="plex")


def _plex(cfg: Config):
    if cfg.plex is None:
        console.print("[red]No plex section in config.yaml[/]")
        raise typer.Exit(1)
    return cfg.plex


def _tautulli(cfg: Config):
    if cfg.tautulli is None:
        console.print("[red]No tautulli section in config.yaml[/]")
        raise typer.Exit(1)
    return cfg.tautulli


@plex_app.command("status")
def plex_status_cmd() -> None:
    """Show Plex server version and connectivity."""
    cfg = _load()
    with console.status("[bold]Querying Plex…[/]"):
        info = plex_get_server_info(_plex(cfg))
    if info.error:
        console.print(f"[red]✗[/] Plex offline: {info.error}")
    else:
        name = f" ({info.friendly_name})" if info.friendly_name else ""
        console.print(f"[green]✓[/] Plex v{info.version}{name}")
        if info.machine_id:
            console.print(f"  [dim]machine ID: {info.machine_id}[/]")


@plex_app.command("libraries")
def plex_libraries_cmd() -> None:
    """List all Plex library sections."""
    cfg = _load()
    with console.status("[bold]Fetching Plex libraries…[/]"):
        libs = plex_list_libraries(_plex(cfg))
    if isinstance(libs, str):
        console.print(f"[red]Error:[/] {libs}")
        raise typer.Exit(1)
    t = Table(box=box.SIMPLE, show_edge=False, pad_edge=False)
    t.add_column("ID",    style="dim",   no_wrap=True)
    t.add_column("Title", style="bold")
    t.add_column("Type")
    t.add_column("Items", justify="right")
    for lib in libs:
        t.add_row(str(lib.key), lib.title, lib.lib_type, str(lib.count))
    console.print(t)


@plex_app.command("clients")
def plex_clients_cmd(
    user: str = typer.Option("", "--user", "-u", help="Filter by last user (substring match)"),
    platform: str = typer.Option("", "--platform", "-p", help="Filter by platform (substring match)"),
    search: str = typer.Option("", "--search", "-s", help="Filter by player/platform/product/user (substring match)"),
) -> None:
    """List distinct devices/players seen in Plex play history, most recent first."""
    cfg = _load()
    from lib.tautulli import list_devices as _tt_list_devices
    with console.status("[bold]Fetching client history…[/]"):
        devices = _tt_list_devices(_tautulli(cfg))

    if user:
        devices = [d for d in devices if user.lower() in d.user.lower()]
    if platform:
        devices = [d for d in devices if platform.lower() in d.platform.lower()]
    if search:
        needle = search.lower()
        devices = [
            d for d in devices
            if needle in d.player.lower() or needle in d.platform.lower()
            or needle in d.product.lower() or needle in d.user.lower()
        ]

    if not devices:
        msg = "No devices match the given filters." if (user or platform or search) else "No devices found in play history."
        console.print(f"[dim]{msg}[/]")
        return

    t = Table(box=box.SIMPLE, show_edge=False, pad_edge=False)
    t.add_column("Player",    style="bold")
    t.add_column("Platform")
    t.add_column("Product")
    t.add_column("Last User")
    t.add_column("Last Seen")
    for d in devices:
        t.add_row(d.player or "—", d.platform or "—", d.product or "—", d.user or "—", d.last_seen or "—")
    console.print(t)


@plex_app.command("scan")
def plex_scan_cmd(
    library: Optional[str] = typer.Argument(
        None, help="Library name or ID to scan (omit for all libraries)"
    ),
) -> None:
    """Trigger a filesystem scan on one or all Plex libraries."""
    cfg = _load()
    pc = _plex(cfg)

    if library is None:
        with console.status("[bold]Fetching libraries…[/]"):
            libs = plex_list_libraries(pc)
        if isinstance(libs, str):
            console.print(f"[red]Error:[/] {libs}")
            raise typer.Exit(1)
        for lib in libs:
            with console.status(f"[bold]Scanning [cyan]{lib.title}[/]…[/]"):
                err = plex_scan_library(pc, lib.key)
            if err:
                console.print(f"[red]✗[/] {lib.title}: {err}")
            else:
                console.print(f"[green]✓[/] Scan queued for [bold]{lib.title}[/] (id={lib.key})")
    else:
        key = plex_section_key(pc, library)
        if isinstance(key, str):
            console.print(f"[red]Error:[/] {key}")
            raise typer.Exit(1)
        with console.status(f"[bold]Scanning library {key}…[/]"):
            err = plex_scan_library(pc, key)
        if err:
            console.print(f"[red]✗[/] {err}")
            raise typer.Exit(1)
        console.print(f"[green]✓[/] Scan queued for library id={key}")


@plex_app.command("analyze")
def plex_analyze_cmd(
    library: Optional[str] = typer.Argument(
        None, help="Library name or ID to analyze (omit for all libraries)"
    ),
) -> None:
    """Trigger media analysis (thumbnails, chapter images) on one or all libraries."""
    cfg = _load()
    pc = _plex(cfg)

    if library is None:
        with console.status("[bold]Fetching libraries…[/]"):
            libs = plex_list_libraries(pc)
        if isinstance(libs, str):
            console.print(f"[red]Error:[/] {libs}")
            raise typer.Exit(1)
        for lib in libs:
            with console.status(f"[bold]Analyzing [cyan]{lib.title}[/]…[/]"):
                err = plex_analyze_library(pc, lib.key)
            if err:
                console.print(f"[red]✗[/] {lib.title}: {err}")
            else:
                console.print(f"[green]✓[/] Analysis queued for [bold]{lib.title}[/] (id={lib.key})")
    else:
        key = plex_section_key(pc, library)
        if isinstance(key, str):
            console.print(f"[red]Error:[/] {key}")
            raise typer.Exit(1)
        with console.status(f"[bold]Analyzing library {key}…[/]"):
            err = plex_analyze_library(pc, key)
        if err:
            console.print(f"[red]✗[/] {err}")
            raise typer.Exit(1)
        console.print(f"[green]✓[/] Analysis queued for library id={key}")


@plex_app.command("empty-trash")
def plex_empty_trash_cmd(
    library: Optional[str] = typer.Argument(
        None, help="Library name or ID (omit for all libraries)"
    ),
) -> None:
    """Empty the Plex trash for one or all libraries."""
    cfg = _load()
    pc = _plex(cfg)

    if library is None:
        with console.status("[bold]Fetching libraries…[/]"):
            libs = plex_list_libraries(pc)
        if isinstance(libs, str):
            console.print(f"[red]Error:[/] {libs}")
            raise typer.Exit(1)
        for lib in libs:
            with console.status(f"[bold]Emptying trash for [cyan]{lib.title}[/]…[/]"):
                err = plex_empty_trash(pc, lib.key)
            if err:
                console.print(f"[red]✗[/] {lib.title}: {err}")
            else:
                console.print(f"[green]✓[/] Trash emptied for [bold]{lib.title}[/]")
    else:
        key = plex_section_key(pc, library)
        if isinstance(key, str):
            console.print(f"[red]Error:[/] {key}")
            raise typer.Exit(1)
        with console.status(f"[bold]Emptying trash for library {key}…[/]"):
            err = plex_empty_trash(pc, key)
        if err:
            console.print(f"[red]✗[/] {err}")
            raise typer.Exit(1)
        console.print(f"[green]✓[/] Trash emptied for library id={key}")


@plex_app.command("clean-bundles")
def plex_clean_bundles_cmd() -> None:
    """Clean up orphaned metadata bundles (server-wide)."""
    cfg = _load()
    with console.status("[bold]Cleaning Plex bundles…[/]"):
        err = plex_clean_bundles(_plex(cfg))
    if err:
        console.print(f"[red]✗[/] {err}")
        raise typer.Exit(1)
    console.print("[green]✓[/] Bundle clean queued")


@plex_app.command("sessions")
def plex_sessions_cmd() -> None:
    """Show active Plex streams."""
    cfg = _load()
    from lib.tautulli import get_activity as _tt_activity
    with console.status("[bold]Fetching active streams…[/]"):
        streams = _tt_activity(_tautulli(cfg))

    if not streams:
        console.print("[dim]No active streams.[/]")
        return

    total_bw = sum(s.bandwidth for s in streams)
    bw_str = f"  ·  {total_bw / 1000:.1f} Mbps" if total_bw else ""
    n = len(streams)
    console.rule(f"[bold]{n} active stream{'s' if n != 1 else ''}[/][dim]{bw_str}[/]")
    console.print()

    t = Table(box=box.SIMPLE, show_edge=False, pad_edge=False)
    t.add_column("Session",   style="dim",  no_wrap=True)
    t.add_column("User",      style="bold", no_wrap=True)
    t.add_column("State",     no_wrap=True)
    t.add_column("Title",     no_wrap=True)
    t.add_column("Progress",  justify="right", no_wrap=True)
    t.add_column("Quality",   no_wrap=True)
    t.add_column("Transcode", no_wrap=True)
    t.add_column("Player",    no_wrap=True)

    _state_styles = {"playing": "green", "paused": "yellow", "buffering": "cyan"}
    for s in streams:
        if s.media_type == "episode" and s.grandparent_title:
            ep_tag = f"S{s.season:02d}E{s.episode:02d}" if s.season else ""
            display_title = s.grandparent_title + (f" · {ep_tag}" if ep_tag else "")
        else:
            display_title = s.title + (f" ({s.year})" if s.year else "")
        state_style = _state_styles.get(s.state, "dim")
        t.add_row(
            s.session_key or "—",
            s.user,
            f"[{state_style}]{s.state}[/]",
            display_title,
            f"{s.progress_pct:.1f}%",
            s.quality_profile or "—",
            s.transcode_decision or "—",
            s.player or "—",
        )

    console.print(t)
    console.print()
    console.rule(f"[dim]terminate: atc plex terminate <session_key>  ·  history: atc plex history[/]")
    console.print()


@plex_app.command("stats")
def plex_stats_cmd() -> None:
    """Show Plex server stats: library scan times, running tasks, and butler schedule."""
    cfg = _load()
    pc = _plex(cfg)

    with console.status("[bold]Fetching Plex stats…[/]"):
        libs       = plex_list_libraries(pc)
        activities = plex_get_activities(pc)
        butler     = plex_get_butler_tasks(pc)

    # ── Running activities ────────────────────────────────────────────────────
    if activities:
        console.print(f"\n[bold]Running ({len(activities)})[/]")
        for a in activities:
            pct_str = f" [{a.progress}%]" if a.progress > 0 else ""
            sub_str = f"  [dim]{a.subtitle}[/]" if a.subtitle else ""
            console.print(f"  [cyan]⟳[/]  {a.title}{pct_str}{sub_str}")
    else:
        console.print("\n[dim]No activities running.[/]")

    # ── Library scan times ────────────────────────────────────────────────────
    console.print()
    if isinstance(libs, str):
        console.print(f"[red]Library error:[/] {libs}")
    else:
        t = Table(title="Libraries", box=box.SIMPLE, show_header=True, pad_edge=False)
        t.add_column("ID",      style="dim",   justify="right", width=4)
        t.add_column("Library", style="bold")
        t.add_column("Type",    style="dim")
        t.add_column("Items",   justify="right")
        t.add_column("Last Scan")
        t.add_column("Updated")
        for lib in libs:
            t.add_row(
                str(lib.key),
                lib.title,
                lib.lib_type,
                str(lib.count),
                lib.scanned_at or "[dim]—[/]",
                lib.updated_at or "[dim]—[/]",
            )
        console.print(t)

    # ── Butler scheduled tasks ────────────────────────────────────────────────
    console.print()
    if isinstance(butler, str):
        console.print(f"[red]Butler error:[/] {butler}")
    elif butler:
        t2 = Table(title="Scheduled Tasks", box=box.SIMPLE, show_header=True, pad_edge=False)
        t2.add_column("",        width=2)
        t2.add_column("Task",    style="dim")
        t2.add_column("Schedule")
        t2.add_column("Last Result")
        t2.add_column("Last Run")
        for task in butler:
            dot = "[green]●[/]" if task.enabled else "[dim]●[/]"
            if task.last_result == "Success":
                result_str = "[green]Success[/]"
            elif task.last_result == "Error":
                result_str = "[red]Error[/]"
            else:
                result_str = "[dim]—[/]"
            schedule = task.schedule or (f"every {task.interval}h" if task.interval else "—")
            last_run = task.last_run or "[dim]—[/]"
            t2.add_row(dot, task.title or task.name, schedule, result_str, last_run)
        console.print(t2)


# ── tautulli (blended into `atc plex`) ────────────────────────────────────────

from lib.tautulli import (
    get_history  as tautulli_get_history,
    terminate_session as tautulli_terminate_session,
)


@plex_app.command("history")
def tautulli_history_cmd(
    count:  int = typer.Option(20, "--count", "-n", help="Number of records to show"),
    user:   str = typer.Option("", "--user", "-u", help="Filter by username"),
    output: _Out = typer.Option(_Out.table, "-o", help="Output format"),
    json_flag: bool = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """Show recent Plex play history."""
    if json_flag:
        output = _Out.json
    cfg = _load()
    with console.status("[bold]Fetching play history…[/]"):
        records = tautulli_get_history(_tautulli(cfg), length=count, user=user)

    if not records:
        console.print("[dim]No history found.[/]")
        return

    if output == _Out.json:
        console.print_json(_json.dumps([
            {
                "user": r.user, "title": r.title, "media_type": r.media_type,
                "show": r.grandparent_title, "year": r.year,
                "played_at": r.played_at, "duration_secs": r.duration_secs,
                "percent_complete": r.percent_complete,
                "player": r.player, "transcode": r.transcode_decision,
            }
            for r in records
        ]))
        return

    def _dur(secs: int) -> str:
        if secs <= 0:
            return "—"
        h, m = divmod(secs // 60, 60)
        return f"{h}h {m}m" if h else f"{m}m"

    t = Table(box=box.SIMPLE, show_edge=False, pad_edge=False)
    t.add_column("User",      style="bold", no_wrap=True)
    t.add_column("Played",    no_wrap=True)
    t.add_column("Title",     no_wrap=True)
    t.add_column("Type",      style="dim", no_wrap=True)
    t.add_column("Duration",  justify="right", no_wrap=True)
    t.add_column("%",         justify="right", no_wrap=True)
    t.add_column("Transcode", no_wrap=True, style="dim")

    for r in records:
        if r.media_type == "episode" and r.grandparent_title:
            display_title = f"{r.grandparent_title} · {r.title}"
        else:
            display_title = r.title + (f" ({r.year})" if r.year else "")
        pct_style = "green" if r.percent_complete >= 90 else "yellow" if r.percent_complete >= 50 else "dim"
        t.add_row(
            r.user,
            r.played_at,
            display_title,
            r.media_type or "—",
            _dur(r.duration_secs),
            f"[{pct_style}]{r.percent_complete}%[/]",
            r.transcode_decision or "—",
        )

    console.print(t)


@plex_app.command("terminate")
def tautulli_terminate_cmd(
    session_key: str = typer.Argument(..., help="Session key shown in 'atc plex sessions'"),
    message: str = typer.Option("", "--message", "-m", help="Message shown to the user"),
    yes: bool    = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Terminate an active Plex stream."""
    cfg = _load()
    tcfg = _tautulli(cfg)
    pc = _plex(cfg)
    if not yes:
        if not Confirm.ask(f"Terminate session [bold]{session_key}[/]?"):
            raise typer.Exit(0)
    with console.status(f"[bold]Terminating session {session_key}…[/]"):
        # Resolve the Plex session_id from current activity — Tautulli's own
        # session_key-only lookup often fails with "Missing session_id".
        from lib.tautulli import get_activity as _tt_activity
        session_id = next(
            (s.session_id for s in _tt_activity(tcfg) if s.session_key == session_key and s.session_id),
            "",
        )
        tautulli_error: str | None = None
        try:
            tautulli_terminate_session(tcfg, session_key=session_key, session_id=session_id, message=message)
        except Exception as e:
            tautulli_error = str(e)

        if tautulli_error:
            # Fall back to hitting the Plex API directly — Tautulli's terminate_session
            # depends on its own internal activity cache having a session_id recorded,
            # which isn't always the case even for a live, currently-playing session.
            sessions = plex_list_sessions(pc)
            plex_session_id = "" if isinstance(sessions, str) else next(
                (s.session_id for s in sessions if s.session_key == session_key and s.session_id),
                "",
            )
            if not plex_session_id:
                console.print(f"[red]✗[/] Failed: {tautulli_error}")
                if isinstance(sessions, str):
                    console.print(f"[dim]Plex fallback also failed: {sessions}[/]")
                else:
                    console.print(
                        "[dim]This session has no session_id in Plex or Tautulli — "
                        "some clients (often mobile apps on direct play) don't report one, "
                        "which makes it untargetable by Plex's terminate API.[/]"
                    )
                raise typer.Exit(1)
            err = plex_terminate_session(pc, plex_session_id, reason=message)
            if err:
                console.print(f"[red]✗[/] Failed: {tautulli_error} (Plex fallback also failed: {err})")
                raise typer.Exit(1)
    console.print(f"[green]✓[/] Session [bold]{session_key}[/] terminated")


# ── speedtest ─────────────────────────────────────────────────────────────────

@app.command("speedtest")
def speedtest_cmd(
    duration: int     = typer.Option(10,    "--duration",  "-d",  help="Seconds per phase (download + upload)"),
    parallel: int     = typer.Option(4,     "--parallel",  "-p",  help="Parallel download streams (upload uses half)"),
    ping_only: bool   = typer.Option(False, "--ping-only",        help="Measure latency only — skip download/upload"),
    ping_samples: int = typer.Option(10,    "--samples",   "-s",  help="Number of ping samples"),
    output: _Out      = typer.Option(_Out.table, "-o",            help="Output format: table or json"),
    json_flag: bool   = typer.Option(False, "-j", is_flag=True, hidden=True, help="JSON output (shorthand for -o json)"),
) -> None:
    """Run a speed test against your OpenSpeedTest server.\n\n[dim]Requires openspeedtest: section in config.yaml.[/]"""
    if json_flag:
        output = _Out.json
    cfg = _load()
    if cfg.openspeedtest is None:
        console.print("[red]No [bold]openspeedtest[/] section in config.yaml[/]")
        console.print("[dim]Add:[/]\n  openspeedtest:\n    host: <ip>\n    port: 3000")
        raise typer.Exit(1)
    asyncio.run(_speedtest_ui(cfg.openspeedtest, duration, parallel, ping_samples, ping_only, output))


async def _speedtest_ui(
    cfg,
    duration_s: int,
    parallel: int,
    ping_samples: int,
    ping_only: bool,
    output: _Out,
) -> None:
    from statistics import median as _median
    from rich.live import Live
    from rich.text import Text
    from lib.speedtest import ping_test, download_test, upload_test

    BAR_W   = 28
    SCALE   = 1000.0   # Mbps reference for bar fill (1 Gbps)

    # ── Visual helpers ────────────────────────────────────────────────────────

    def _bar(mbps: float, width: int = BAR_W) -> str:
        filled = round(min(mbps / SCALE, 1.0) * width)
        return "█" * filled + "░" * (width - filled)

    def _speed_color(mbps: float) -> str:
        if mbps >= 500: return "bold bright_green"
        if mbps >= 100: return "cyan"
        if mbps >= 25:  return "yellow"
        return "red"

    def _speed_label(mbps: float) -> tuple[str, str]:
        """Return (label, color)."""
        if mbps >= 900: return "Gigabit",   "bold bright_green"
        if mbps >= 500: return "Excellent",  "bright_green"
        if mbps >= 200: return "Fast",       "green"
        if mbps >= 100: return "Good",       "cyan"
        if mbps >= 25:  return "Basic",      "yellow"
        return             "Slow",       "red"

    def _ping_color(ms: float) -> str:
        if ms < 5:  return "bold bright_green"
        if ms < 20: return "green"
        if ms < 50: return "yellow"
        return "red"

    def _ping_label(ms: float) -> str:
        if ms < 5:  return "Excellent"
        if ms < 20: return "Good"
        if ms < 50: return "Fair"
        return "High"

    def _fmt_data(b: int) -> str:
        if b >= 1024 ** 3: return f"{b / 1024**3:.2f} GB"
        if b >= 1024 ** 2: return f"{b / 1024**2:.0f} MB"
        return f"{b / 1024:.0f} KB"

    # ── Header ────────────────────────────────────────────────────────────────

    if output != _Out.json:
        console.print()
        console.rule(f"[bold cyan]⚡  OpenSpeedTest  ·  {cfg.host}:{cfg.port}[/]")
        console.print()

    # ── Ping ──────────────────────────────────────────────────────────────────

    ping_samples_collected: list[float] = []

    if output != _Out.json:
        with Live(console=console, refresh_per_second=12, transient=True) as live:
            def _on_sample(_idx: int, rtt: float) -> None:
                ping_samples_collected.append(rtt)
                done    = "[green]●[/] " * len(ping_samples_collected)
                pending = "[dim]·[/] " * (ping_samples - len(ping_samples_collected))
                med_str = f"  [bold]{_median(ping_samples_collected):.1f}[/] ms" if ping_samples_collected else ""
                live.update(Text.from_markup(f"     Ping  {done}{pending}{med_str}"))

            ping_result = await ping_test(cfg, samples=ping_samples, on_sample=_on_sample)

        pc = _ping_color(ping_result.median_ms)
        pl = _ping_label(ping_result.median_ms)
        console.print(
            f"     Ping  [{pc}]{ping_result.median_ms:.1f} ms[/]"
            f"  [dim]±{ping_result.jitter_ms:.1f} ms jitter  ·  "
            f"min {ping_result.min_ms:.1f} ms  ·  {pl}[/]"
        )
        console.print()
    else:
        ping_result = await ping_test(cfg, samples=ping_samples)

    if ping_only:
        if output == _Out.json:
            console.print_json(_json.dumps({
                "server":     f"{cfg.host}:{cfg.port}",
                "ping_ms":    round(ping_result.median_ms, 2),
                "jitter_ms":  round(ping_result.jitter_ms, 2),
                "min_ms":     round(ping_result.min_ms, 2),
            }))
        else:
            console.rule()
            console.print()
        return

    # ── Download ──────────────────────────────────────────────────────────────

    dl_counter   = [0]
    dl_mbps_fin  = 0.0
    dl_bytes_fin = 0

    if output != _Out.json:
        dl_t0 = time.perf_counter()

        async def _monitor_dl(live: Live) -> None:
            while True:
                await asyncio.sleep(0.15)
                elapsed = max(time.perf_counter() - dl_t0, 0.001)
                mbps    = (dl_counter[0] * 8) / (elapsed * 1_000_000)
                color   = _speed_color(mbps)
                live.update(Text.from_markup(
                    f"   ↓  Download  [{color}]{_bar(mbps)}[/]"
                    f"  [{color}]{mbps:>7.1f} Mbps[/]"
                    f"  [dim]{_fmt_data(dl_counter[0])}[/]"
                ))

        with Live(console=console, refresh_per_second=8, transient=True) as live:
            live.update(Text.from_markup(
                f"   ↓  Download  [dim]{_bar(0)}[/]  [dim]  0.0 Mbps[/]"
            ))
            monitor = asyncio.create_task(_monitor_dl(live))
            dl_mbps_fin, dl_bytes_fin = await download_test(cfg, duration_s, parallel, dl_counter)
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

        dc = _speed_color(dl_mbps_fin)
        dl_lbl, dl_lc = _speed_label(dl_mbps_fin)
        console.print(
            f"   ↓  Download  [{dc}]{_bar(dl_mbps_fin)}[/]"
            f"  [{dc}]{dl_mbps_fin:>7.1f} Mbps[/]"
            f"  [{dl_lc}]● {dl_lbl}[/]"
            f"  [dim]{_fmt_data(dl_bytes_fin)}[/]"
        )
    else:
        dl_mbps_fin, dl_bytes_fin = await download_test(cfg, duration_s, parallel, dl_counter)

    # ── Upload ────────────────────────────────────────────────────────────────

    ul_counter   = [0]
    ul_parallel  = max(1, parallel // 2)
    ul_mbps_fin  = 0.0
    ul_bytes_fin = 0

    if output != _Out.json:
        ul_t0 = time.perf_counter()

        async def _monitor_ul(live: Live) -> None:
            while True:
                await asyncio.sleep(0.15)
                elapsed = max(time.perf_counter() - ul_t0, 0.001)
                mbps    = (ul_counter[0] * 8) / (elapsed * 1_000_000)
                color   = _speed_color(mbps)
                live.update(Text.from_markup(
                    f"   ↑  Upload    [{color}]{_bar(mbps)}[/]"
                    f"  [{color}]{mbps:>7.1f} Mbps[/]"
                    f"  [dim]{_fmt_data(ul_counter[0])}[/]"
                ))

        with Live(console=console, refresh_per_second=8, transient=True) as live:
            live.update(Text.from_markup(
                f"   ↑  Upload    [dim]{_bar(0)}[/]  [dim]  0.0 Mbps[/]"
            ))
            monitor = asyncio.create_task(_monitor_ul(live))
            ul_mbps_fin, ul_bytes_fin = await upload_test(cfg, duration_s, ul_parallel, ul_counter)
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

        uc = _speed_color(ul_mbps_fin)
        ul_lbl, ul_lc = _speed_label(ul_mbps_fin)
        console.print(
            f"   ↑  Upload    [{uc}]{_bar(ul_mbps_fin)}[/]"
            f"  [{uc}]{ul_mbps_fin:>7.1f} Mbps[/]"
            f"  [{ul_lc}]● {ul_lbl}[/]"
            f"  [dim]{_fmt_data(ul_bytes_fin)}[/]"
        )
    else:
        ul_mbps_fin, ul_bytes_fin = await upload_test(cfg, duration_s, ul_parallel, ul_counter)

    # ── Summary ───────────────────────────────────────────────────────────────

    if output == _Out.json:
        console.print_json(_json.dumps({
            "server":          f"{cfg.host}:{cfg.port}",
            "ping_ms":         round(ping_result.median_ms, 2),
            "jitter_ms":       round(ping_result.jitter_ms, 2),
            "min_ms":          round(ping_result.min_ms, 2),
            "download_mbps":   round(dl_mbps_fin, 2),
            "download_bytes":  dl_bytes_fin,
            "upload_mbps":     round(ul_mbps_fin, 2),
            "upload_bytes":    ul_bytes_fin,
            "duration_s":      duration_s,
            "parallel_dl":     parallel,
            "parallel_ul":     ul_parallel,
        }))
        return

    console.print()
    console.rule()
    console.print()

    t = Table(box=box.SIMPLE, show_header=False, pad_edge=False, show_edge=False)
    t.add_column(width=11, justify="right", style="dim")
    t.add_column(min_width=14)
    t.add_column()

    # Ping row
    pc  = _ping_color(ping_result.median_ms)
    pl  = _ping_label(ping_result.median_ms)
    t.add_row(
        "Ping",
        f"[{pc}]{ping_result.median_ms:.1f} ms[/]",
        f"[dim]±{ping_result.jitter_ms:.1f} ms jitter  ·  min {ping_result.min_ms:.1f} ms  ·  {pl}[/]",
    )
    t.add_row("", "", "")

    # Download row
    dc = _speed_color(dl_mbps_fin)
    dl_lbl, dl_lc = _speed_label(dl_mbps_fin)
    t.add_row(
        "Download",
        f"[{dc}]{dl_mbps_fin:.1f} Mbps[/]",
        f"[{dl_lc}]● {dl_lbl}[/]  [dim]{_fmt_data(dl_bytes_fin)}  ·  {parallel} streams[/]",
    )

    # Upload row
    uc = _speed_color(ul_mbps_fin)
    ul_lbl, ul_lc = _speed_label(ul_mbps_fin)
    t.add_row(
        "Upload",
        f"[{uc}]{ul_mbps_fin:.1f} Mbps[/]",
        f"[{ul_lc}]● {ul_lbl}[/]  [dim]{_fmt_data(ul_bytes_fin)}  ·  {ul_parallel} streams[/]",
    )

    t.add_row("", "", "")
    t.add_row(
        "Test",
        "",
        f"[dim]{duration_s}s per phase  ·  {cfg.host}:{cfg.port}[/]",
    )

    console.print(t)
    console.print()
    console.rule()
    console.print()

    if not ping_only:
        from lib.speedtest import pick_fact
        fact = pick_fact(ping_result.median_ms, dl_mbps_fin, ul_mbps_fin)
        console.print(f"  [bold yellow]💡[/]  [italic dim]{fact}[/]")
        console.print()


# ── versions ──────────────────────────────────────────────────────────────────

versions_app = typer.Typer(
    help="Docker image version tracking — n-2 supply-chain policy.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(versions_app, name="versions")


@versions_app.command("check")
def versions_check(
    image: str = typer.Argument(..., help="Image ref — e.g. 'n8nio/n8n', 'couchdb:3', 'docker.n8n.io/n8nio/n8n'"),
    n: int = typer.Option(2, "--lag", "-n", help="How many minor versions behind latest to recommend (default 2)"),
) -> None:
    """Show available semver tags and recommend the n-2 pin for an image."""
    from lib.registry import check_image, DockerHubCreds

    cfg = _load()
    dh_creds = (
        DockerHubCreds(cfg.docker_hub.username, cfg.docker_hub.token)
        if cfg.docker_hub else None
    )

    console.print(f"[dim]Querying registry for [bold]{image}[/bold]…[/dim]")

    info = check_image(image, docker_hub_creds=dh_creds)

    if info.fetch_error:
        console.print(f"[red]Registry error:[/] {info.fetch_error}")
        raise typer.Exit(1)

    if not info.semver_tags:
        console.print(f"[yellow]⚠[/]  No semver tags found for [bold]{image}[/bold].")
        console.print("    The image may use only non-semver tags (e.g. 'latest', 'stable', 'nightly').")
        raise typer.Exit(0)

    ref = info.ref
    full = f"{ref.full_name}:{ref.tag}"

    if info.is_floating:
        console.print(
            f"\n[yellow]⚠  Floating tag:[/] [bold]{full}[/bold] is not pinned to a specific version.\n"
            f"   Supply-chain risk: any push to this tag is auto-deployed by Watchtower.\n"
        )
    else:
        pos = info.current_position()
        pos_color = "green" if pos in ("n-2", "n-1") else ("yellow" if pos == "latest" else "dim")
        console.print(f"\n  Current:  [bold]{full}[/bold]  [[{pos_color}]{pos}[/{pos_color}]]\n")

    t = Table(title=f"Version ladder — {ref.full_name}", box=box.SIMPLE, show_header=True, header_style="bold")
    t.add_column("Position", style="dim", width=10)
    t.add_column("Tag", width=20)
    t.add_column("", width=28)

    latest = info.latest_tag
    if latest:
        t.add_row("latest (n)", latest, "[dim]newest available[/dim]")

    n1 = info.n_minus(1)
    if n1:
        t.add_row("n-1", n1, "")

    rec = info.n_minus(n)
    if rec:
        t.add_row(
            f"[green]n-{n}[/green]",
            f"[green bold]{rec}[/green bold]",
            f"[green]← recommended pin[/green]",
        )
    else:
        console.print(f"[yellow]Not enough release history to identify n-{n}.[/yellow]")

    console.print(t)

    if rec:
        pinned = f"{ref.full_name}:{rec}"
        console.print(f"  Compose image pin:  [bold green]{pinned}[/bold green]")
        console.print(
            f"  Add Watchtower label: [dim]com.centurylinklabs.watchtower.enable=false[/dim]\n"
        )


@versions_app.command("list")
def versions_list(
    n: int = typer.Option(2, "--lag", "-n", help="Minor version lag to recommend (default 2)"),
    floating_only: bool = typer.Option(False, "--floating", "-f", help="Show only containers with floating tags"),
) -> None:
    """List all Portainer-managed containers with their version status.

    Queries Docker Hub / registry for each unique image and shows whether
    each container is on latest, n-1, n-2, or a floating (unversioned) tag.
    """
    from lib.portainer import fetch_environments, fetch_containers
    from lib.registry import check_image, parse_image_ref, DockerHubCreds

    cfg = _load()
    dh_creds = (
        DockerHubCreds(cfg.docker_hub.username, cfg.docker_hub.token)
        if cfg.docker_hub else None
    )
    if not dh_creds:
        console.print(
            "[yellow]⚠[/]  No Docker Hub credentials configured — Docker Hub images will be rate-limited.\n"
            "   Add [bold]docker_hub:[/bold] (username + token) to config.yaml for reliable results.\n"
        )

    if not cfg.portainer:
        console.print("[red]Portainer not configured.[/] Add a [bold]portainer:[/] section to config.yaml.")
        raise typer.Exit(1)

    console.print("[dim]Fetching containers from Portainer…[/dim]")
    try:
        envs = fetch_environments(cfg.portainer)
        all_containers = []
        for env in envs:
            all_containers.extend(fetch_containers(cfg.portainer, env.id, env.name, all_containers=False))
    except Exception as e:
        console.print(f"[red]Portainer error:[/] {e}")
        raise typer.Exit(1)

    if not all_containers:
        console.print("[dim]No running containers found.[/dim]")
        raise typer.Exit(0)

    # Deduplicate images — query each unique image ref once
    unique_images: dict[str, str] = {}  # normalised image string → first container's raw image
    for c in all_containers:
        img = c.image.split("@")[0]  # strip digest suffix if present
        ref = parse_image_ref(img)
        key = ref.full_name  # registry+namespace+image without tag
        if key not in unique_images:
            unique_images[key] = img

    console.print(f"[dim]Checking {len(unique_images)} unique image(s) against registries…[/dim]")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    version_cache: dict[str, object] = {}
    lock = threading.Lock()
    done = 0

    def _check(key: str, img: str):
        result = check_image(img, docker_hub_creds=dh_creds)
        nonlocal done
        with lock:
            done += 1
            console.print(f"[dim]  [{done}/{len(unique_images)}] {key}[/dim]")
        return key, result

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_check, k, v): k for k, v in unique_images.items()}
        for fut in as_completed(futures):
            key, result = fut.result()
            version_cache[key] = result

    console.print()

    t = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    t.add_column("Container", min_width=22)
    t.add_column("Image", min_width=28)
    t.add_column("Tag", min_width=14)
    t.add_column("Status", min_width=10)
    t.add_column(f"n-{n} pin", min_width=14)

    shown = 0
    for c in sorted(all_containers, key=lambda x: x.name):
        img = c.image.split("@")[0]
        ref = parse_image_ref(img)
        key = ref.full_name
        info = version_cache.get(key)

        if info is None:
            continue

        is_float = info.is_floating  # type: ignore[union-attr]
        if floating_only and not is_float:
            continue

        pos = info.current_position()  # type: ignore[union-attr]
        rec = info.n_minus(n)          # type: ignore[union-attr]

        if is_float:
            status = "[red]floating[/red]"
        elif pos == "latest":
            status = "[yellow]latest[/yellow]"
        elif pos == f"n-{n}":
            status = f"[green]n-{n}[/green]"
        elif pos.startswith("n-"):
            status = f"[dim]{pos}[/dim]"
        else:
            status = f"[dim]{pos}[/dim]"

        fetch_err = getattr(info, "fetch_error", "")
        if fetch_err:
            status = "[dim]registry err[/dim]"
            rec = None

        pin_str = f":{rec}" if rec else "[dim]—[/dim]"
        t.add_row(c.name, ref.full_name, ref.tag, status, pin_str)
        shown += 1

    if shown == 0:
        console.print("[dim]No containers match the filter.[/dim]")
    else:
        console.print(t)
        console.print(
            f"[dim]Legend: [red]floating[/red]=unversioned tag  "
            f"[yellow]latest[/yellow]=on latest semver  "
            f"[green]n-{n}[/green]=at recommended lag  "
            f"[dim]n-X[/dim]=behind by X minor versions[/dim]"
        )


# ── radar ─────────────────────────────────────────────────────────────────────

@app.command("radar")
def launch_tui() -> None:
    """Launch the interactive TUI dashboard."""
    cfg = _load()
    from main import HomelabApp
    HomelabApp(cfg).run()


# ── quick (ad hoc file/page hosting) ────────────────────────────────────────
# One standing nginx stack ("quick") on docker-1 serving quick.local.damyanpetkov.com
# via NPM + the existing *.local.damyanpetkov.com wildcard cert. `atc quick publish`
# tars a local file/dir and PUTs it into the running container via the Docker
# Engine API (proxied through Portainer) — no SSH needed for routine use.

QUICK_STACK_NAME = "quick"
QUICK_DOMAIN = "quick.local.damyanpetkov.com"
QUICK_SERVE_ROOT = "/usr/share/nginx/html"

quick_app = typer.Typer(
    help="Publish quick throwaway files/pages to https://quick.local.damyanpetkov.com",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(quick_app, name="quick")


def _quick_compose() -> str:
    return (
        "services:\n"
        "  quick:\n"
        "    image: nginx:alpine\n"
        "    restart: unless-stopped\n"
        "    networks:\n"
        "      - npm_network\n"
        "    volumes:\n"
        "      - quick_data:/usr/share/nginx/html\n"
        "    command: [\"nginx\", \"-g\", \"daemon off;\"]\n"
        "networks:\n"
        "  npm_network:\n"
        "    external: true\n"
        "volumes:\n"
        "  quick_data:\n"
    )


@quick_app.command("bootstrap")
def quick_bootstrap(
    target: str = typer.Option("docker-1", "--target", help="Portainer environment name/substring"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be created without doing it"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """One-time setup: create the 'quick' stack, Pi-hole A record, and NPM proxy host.

    Safe to re-run — each step checks for an existing resource first and skips it.
    """
    cfg = _load()
    pt_cfg = _require_portainer(cfg)
    npm_cfg = _npm_cfg(cfg)
    primary_pihole = _pihole_primary(cfg)

    from lib.portainer import fetch_environments, fetch_stacks, create_stack

    with console.status("[bold]Resolving target environment…[/]"):
        envs = [e for e in fetch_environments(pt_cfg) if e.status == 1]
        env = _resolve_single_env(envs, target)

    stacks = fetch_stacks(pt_cfg, envs)
    existing_stack = next((s for s in stacks if s.env_id == env.id and s.name == QUICK_STACK_NAME), None)

    with console.status(f"[bold]Checking DNS on {primary_pihole.name}…[/]"):
        dns_records = asyncio.run(list_custom_dns(primary_pihole))
    existing_dns = next((d for d in dns_records if d.get("domain") == QUICK_DOMAIN), None)
    existing_npm = npm_find_proxy_host(npm_cfg, QUICK_DOMAIN)

    # docker-1's known LAN IP — reuse whatever its other DNS entries already point at,
    # falling back to the documented 192.168.1.78 if none found.
    docker1_ip = next(
        (d["ip"] for d in dns_records if "portainer" in d.get("domain", "") or d.get("ip") == "192.168.1.78"),
        "192.168.1.78",
    )

    console.print(f"[bold]Plan for '{QUICK_STACK_NAME}' on {env.name}:[/]")
    console.print(f"  1. Stack '{QUICK_STACK_NAME}' (nginx:alpine + named volume) — "
                   f"{'[dim]already exists, skip[/]' if existing_stack else '[green]create[/]'}")
    console.print(f"  2. Pi-hole A record {QUICK_DOMAIN} → {docker1_ip} — "
                   f"{'[dim]already exists, skip[/]' if existing_dns else '[green]create[/]'}")
    console.print(f"  3. NPM proxy host {QUICK_DOMAIN} → quick:80 (cert #12, ssl-forced) — "
                   f"{'[dim]already exists, skip[/]' if existing_npm else '[green]create[/]'}")

    if dry_run:
        return
    if not yes and not Confirm.ask("Proceed?"):
        raise typer.Exit(0)

    if not existing_stack:
        with console.status("[bold]Creating 'quick' stack…[/]"):
            stack_id = create_stack(pt_cfg, env.id, QUICK_STACK_NAME, _quick_compose())
        console.print(f"[green]✓[/] Created stack #{stack_id}")
    else:
        console.print(f"[dim]✓ Stack already exists (#{existing_stack.id})[/]")

    if not existing_dns:
        with console.status("[bold]Adding Pi-hole record…[/]"):
            ok, err = asyncio.run(add_custom_dns(primary_pihole, QUICK_DOMAIN, docker1_ip))
        if ok:
            console.print(f"[green]✓[/] Added Pi-hole record {QUICK_DOMAIN} → {docker1_ip}")
        else:
            console.print(f"[red]✗[/] Failed to add Pi-hole record — {err}")
            raise typer.Exit(1)
    else:
        console.print(f"[dim]✓ Pi-hole record already exists ({existing_dns.get('ip')})[/]")

    if not existing_npm:
        npm_create_proxy_host(
            npm_cfg, QUICK_DOMAIN, "quick", 80,
            forward_scheme="http", block_exploits=True, allow_websocket_upgrade=False,
            certificate_id=12, ssl_forced=True,
        )
        console.print(f"[green]✓[/] Created NPM proxy host for {QUICK_DOMAIN}")
    else:
        console.print(f"[dim]✓ NPM proxy host already exists (#{existing_npm.id})[/]")

    console.print(f"\n[bold green]Ready.[/] Publish with: [cyan]atc quick publish <file_or_dir>[/]")


@quick_app.command("publish")
def quick_publish(
    path: Path = typer.Argument(..., exists=True, help="Local file or directory to publish"),
    name: Optional[str] = typer.Option(None, "--name", help="Subpath under the site root (default: filename stem)"),
    target: str = typer.Option("docker-1", "--target", help="Portainer environment name/substring"),
) -> None:
    """Upload a file or directory into the 'quick' stack and print its live URL.

    Run 'atc quick bootstrap' once first if the stack doesn't exist yet.
    """
    import io
    import tarfile

    cfg = _load()
    pt_cfg = _require_portainer(cfg)

    from lib.portainer import fetch_environments, fetch_containers, upload_to_container

    slug = name or path.stem.replace(" ", "-").lower()

    with console.status("[bold]Finding the 'quick' container…[/]"):
        envs = [e for e in fetch_environments(pt_cfg) if e.status == 1]
        env = _resolve_single_env(envs, target)
        containers = fetch_containers(pt_cfg, env.id, env.name, all_containers=False)
        quick_container = next(
            (c for c in containers if c.name.startswith(QUICK_STACK_NAME)), None,
        )
    if not quick_container:
        console.print(
            f"[red]❌  No running '{QUICK_STACK_NAME}' container on {env.name}. "
            f"Run [cyan]atc quick bootstrap[/] first.[/]"
        )
        raise typer.Exit(1)

    # Build a tar stream rooted such that extracting into QUICK_SERVE_ROOT
    # places files at QUICK_SERVE_ROOT/<slug>/... (single file) or
    # QUICK_SERVE_ROOT/<slug> (single file at top, named to serve directly).
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        if path.is_file():
            tar.add(path, arcname=f"{slug}/index.html" if path.suffix == ".html" else f"{slug}/{path.name}")
        else:
            tar.add(path, arcname=slug)

    with console.status(f"[bold]Uploading {path.name} as '{slug}'…[/]"):
        upload_to_container(pt_cfg, env.id, quick_container.id, buf.getvalue(), QUICK_SERVE_ROOT)

    if path.is_file() and path.suffix == ".html":
        url = f"https://{QUICK_DOMAIN}/{slug}/"
    elif path.is_file():
        url = f"https://{QUICK_DOMAIN}/{slug}/{path.name}"
    else:
        url = f"https://{QUICK_DOMAIN}/{slug}/"

    console.print(f"[green]✓[/] Published. [bold cyan]{url}[/]")


@quick_app.command("list")
def quick_list(
    target: str = typer.Option("docker-1", "--target", help="Portainer environment name/substring"),
) -> None:
    """List what's currently published under the quick site root."""
    cfg = _load()
    pt_cfg = _require_portainer(cfg)

    from lib.portainer import fetch_environments, fetch_containers

    envs = [e for e in fetch_environments(pt_cfg) if e.status == 1]
    env = _resolve_single_env(envs, target)
    containers = fetch_containers(pt_cfg, env.id, env.name, all_containers=False)
    quick_container = next((c for c in containers if c.name.startswith(QUICK_STACK_NAME)), None)
    if not quick_container:
        console.print(f"[red]❌  No running '{QUICK_STACK_NAME}' container on {env.name}.[/]")
        raise typer.Exit(1)

    console.print(
        f"[dim]Listing requires container exec, not yet wired — for now check "
        f"https://{QUICK_DOMAIN}/ directly (autoindex is on), or `atc docker logs quick --env {target}`.[/]"
    )

