"""Dashboard screen — main view showing Proxmox nodes, VMs, LXCs, and Beszel hosts."""
from __future__ import annotations

import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.screen import Screen
from textual.widgets import (
    DataTable, Footer, Header, Label, Static, LoadingIndicator
)
from textual import work

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config
from lib.proxmox import ClusterStatus, fetch_cluster_status
from lib.beszel import BeszelSystem, fetch_systems as fetch_beszel
from lib.hosts import normalize_name
from lib.tls import CertStatus, check_all_certs


def _fmt_bytes(b: int) -> str:
    if b >= 1024 ** 3:
        return f"{b / 1024**3:.1f}G"
    if b >= 1024 ** 2:
        return f"{b / 1024**2:.0f}M"
    return f"{b / 1024:.0f}K"


def _fmt_pct(val: float, warn: float = 0.75, crit: float = 0.90) -> str:
    pct = val * 100
    icon = "🔴" if val >= crit else ("🟡" if val >= warn else "🟢")
    return f"{icon} {pct:.0f}%"


def _status_icon(status: str) -> str:
    return "🟢" if status == "running" else "⚫"


class SectionHeader(Static):
    DEFAULT_CSS = """
    SectionHeader {
        background: $accent;
        color: $text;
        padding: 0 1;
        text-style: bold;
        width: 100%;
    }
    """


class DashboardScreen(Screen):
    BINDINGS = [
        ("c", "refresh_data", "Refresh"),
    ]

    DEFAULT_CSS = """
    DashboardScreen {
        layout: vertical;
    }
    #status-bar {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    #main-columns {
        layout: horizontal;
        height: 1fr;
    }
    #left-col {
        width: 1fr;
        layout: vertical;
    }
    #right-col {
        width: 1fr;
        layout: vertical;
    }
    .panel {
        border: round $accent;
        padding: 0 1;
        margin: 0 1 1 0;
        height: auto;
        min-height: 6;
    }
    .panel-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    LoadingIndicator {
        height: 3;
    }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._cluster: ClusterStatus | None = None
        self._beszel: list[BeszelSystem] = []
        self._certs: list[CertStatus] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Loading…", id="status-bar")
        with Horizontal(id="main-columns"):
            with Vertical(id="left-col"):
                yield Static("⬛ Proxmox Nodes", classes="panel-title")
                with ScrollableContainer(classes="panel", id="nodes-panel"):
                    yield DataTable(id="nodes-table", show_cursor=False)
                yield Static("🖥  Virtual Machines", classes="panel-title")
                with ScrollableContainer(classes="panel", id="vms-panel"):
                    yield DataTable(id="vms-table", show_cursor=True)
            with Vertical(id="right-col"):
                yield Static("📦 LXC Containers", classes="panel-title")
                with ScrollableContainer(classes="panel", id="lxcs-panel"):
                    yield DataTable(id="lxcs-table", show_cursor=True)
                yield Static("🖥  Standalone Hosts", classes="panel-title")
                with ScrollableContainer(classes="panel", id="beszel-panel"):
                    yield DataTable(id="beszel-table", show_cursor=False)
                yield Static("🔒 TLS Certificates", classes="panel-title")
                with ScrollableContainer(classes="panel", id="certs-panel"):
                    yield DataTable(id="certs-table", show_cursor=False)
        yield Footer()

    def on_mount(self) -> None:
        self._setup_tables()
        self.refresh_data()
        self.set_interval(self.config.refresh_interval, self.refresh_data)

    def _setup_tables(self) -> None:
        nodes_t = self.query_one("#nodes-table", DataTable)
        nodes_t.add_columns("Node", "Status", "CPU", "Memory", "VMs", "LXCs", "Uptime")

        vms_t = self.query_one("#vms-table", DataTable)
        vms_t.add_columns("", "Name", "Node", "CPU", "Memory", "Disk", "Uptime")

        lxcs_t = self.query_one("#lxcs-table", DataTable)
        lxcs_t.add_columns("", "Name", "Node", "CPU", "Memory", "Disk", "Uptime")

        beszel_t = self.query_one("#beszel-table", DataTable)
        beszel_t.add_columns("Host", "Status", "CPU", "Memory", "Disk")

        certs_t = self.query_one("#certs-table", DataTable)
        certs_t.add_columns("Name", "Expires", "Days")

    @work(exclusive=True)
    async def refresh_data(self) -> None:
        self.query_one("#status-bar", Static).update("⏳ Refreshing…")
        try:
            self._cluster = await fetch_cluster_status(self.config.proxmox)
            if self.config.beszel:
                self._beszel = await fetch_beszel(self.config.beszel)
            if self.config.tls_checks:
                self._certs = await check_all_certs(self.config.tls_checks)
            self._populate_tables()
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            self.query_one("#status-bar", Static).update(
                f"✅ {ts}  |  \[u]Update \[r]Scripts \[k]Kuma \[s]Seerr \[x]Plex \[p]Pi-hole \[t]Speedtest \[a]Pipeline \[c]Refresh \[q]Quit"
            )
        except Exception as e:
            self.query_one("#status-bar", Static).update(f"❌ Error refreshing: {e}")

    def _populate_tables(self) -> None:
        if not self._cluster:
            return

        # Build Beszel lookup by normalized name for metric enrichment
        beszel_map: dict[str, BeszelSystem] = {
            normalize_name(s.name): s for s in self._beszel
        }
        matched_beszel: set[str] = set()

        # ---- Nodes ----
        nodes_t = self.query_one("#nodes-table", DataTable)
        nodes_t.clear()
        guest_counts: dict[str, tuple[int, int]] = {}  # node -> (vms, lxcs)
        for g in self._cluster.guests:
            vm_c, lxc_c = guest_counts.get(g.node, (0, 0))
            if g.type == "qemu":
                guest_counts[g.node] = (vm_c + 1, lxc_c)
            else:
                guest_counts[g.node] = (vm_c, lxc_c + 1)

        for node in self._cluster.nodes:
            status = "🟢 Online" if node.online else f"🔴 Offline"
            cpu = _fmt_pct(node.cpu) if node.online else "—"
            mem = (
                f"{_fmt_pct(node.mem_used / node.mem_total if node.mem_total else 0)} "
                f"({_fmt_bytes(node.mem_used)}/{_fmt_bytes(node.mem_total)})"
                if node.online else "—"
            )
            vms, lxcs = guest_counts.get(node.name, (0, 0))
            uptime = _fmt_uptime(node.uptime) if node.online else node.error[:30]
            nodes_t.add_row(node.name, status, cpu, mem, str(vms), str(lxcs), uptime)

        # ---- VMs ----
        vms_t = self.query_one("#vms-table", DataTable)
        vms_t.clear()
        vms = sorted(
            [g for g in self._cluster.guests if g.type == "qemu"],
            key=lambda g: (g.status != "running", g.name)
        )
        for vm in vms:
            bz = beszel_map.get(normalize_name(vm.name))
            if bz:
                matched_beszel.add(normalize_name(vm.name))
            icon = _status_icon(vm.status)
            if bz and bz.online:
                cpu = f"{bz.cpu_percent:.0f}%"
                mem = f"{bz.mem_percent:.0f}%"
                disk = f"{bz.disk_percent:.0f}%"
            elif vm.status == "running":
                cpu = f"{vm.cpu * 100:.0f}%"
                mem = (
                    f"{_fmt_bytes(vm.mem)}/{_fmt_bytes(vm.maxmem)}"
                    if vm.maxmem > 0 else "—"
                )
                disk = "—"
            else:
                cpu = mem = disk = "—"
            uptime = _fmt_uptime(vm.uptime) if vm.status == "running" else vm.status
            vms_t.add_row(icon, vm.name, vm.node, cpu, mem, disk, uptime)

        # ---- LXCs ----
        lxcs_t = self.query_one("#lxcs-table", DataTable)
        lxcs_t.clear()
        lxcs = sorted(
            [g for g in self._cluster.guests if g.type == "lxc"],
            key=lambda g: (g.status != "running", g.name)
        )
        for ct in lxcs:
            bz = beszel_map.get(normalize_name(ct.name))
            if bz:
                matched_beszel.add(normalize_name(ct.name))
            icon = _status_icon(ct.status)
            if bz and bz.online:
                cpu = f"{bz.cpu_percent:.0f}%"
                mem = f"{bz.mem_percent:.0f}%"
                disk = f"{bz.disk_percent:.0f}%"
            elif ct.status == "running":
                cpu = f"{ct.cpu * 100:.0f}%"
                mem = (
                    f"{_fmt_bytes(ct.mem)}/{_fmt_bytes(ct.maxmem)}"
                    if ct.maxmem > 0 else "—"
                )
                disk = "—"
            else:
                cpu = mem = disk = "—"
            uptime = _fmt_uptime(ct.uptime) if ct.status == "running" else ct.status
            lxcs_t.add_row(icon, ct.name, ct.node, cpu, mem, disk, uptime)

        # ---- Standalone hosts (Beszel-only — not matched to any Proxmox guest) ----
        beszel_t = self.query_one("#beszel-table", DataTable)
        beszel_t.clear()
        standalone = [s for s in self._beszel if normalize_name(s.name) not in matched_beszel]
        for sys in standalone:
            status = "🟢 Online" if sys.online else f"🔴 {sys.error[:20] or sys.status}"
            cpu = f"{sys.cpu_percent:.0f}%" if sys.online else "—"
            mem = f"{sys.mem_percent:.0f}%" if sys.online else "—"
            disk = f"{sys.disk_percent:.0f}%" if sys.online else "—"
            beszel_t.add_row(sys.name, status, cpu, mem, disk)

        # ---- TLS Certs ----
        certs_t = self.query_one("#certs-table", DataTable)
        certs_t.clear()
        for cert in self._certs:
            if not cert.ok:
                certs_t.add_row(cert.name, "—", f"❌ {cert.error[:25]}")
                continue
            expiry_str = cert.expiry.strftime("%Y-%m-%d") if cert.expiry else "—"
            if cert.days_left <= 14:
                days_str = f"🔴 {cert.days_left}d"
            elif cert.days_left <= 30:
                days_str = f"🟡 {cert.days_left}d"
            else:
                days_str = f"🟢 {cert.days_left}d"
            certs_t.add_row(cert.name, expiry_str, days_str)

    def action_refresh_data(self) -> None:
        self.refresh_data()

    def action_open_update(self) -> None:
        self.app.push_screen("update")

    def action_open_scripts(self) -> None:
        self.app.push_screen("scripts")

    def action_open_kuma(self) -> None:
        self.app.push_screen("uptimekuma")

    def action_open_seerr(self) -> None:
        self.app.push_screen("seerr")

    def action_open_plex(self) -> None:
        self.app.push_screen("plex")

    def action_open_pihole(self) -> None:
        self.app.push_screen("pihole")

    def action_open_speedtest(self) -> None:
        self.app.push_screen("speedtest")


def _fmt_uptime(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"
