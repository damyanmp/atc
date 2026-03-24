"""Dashboard screen — main view showing Proxmox nodes, VMs, LXCs, and DSM."""
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
from lib.dsm import DSMStatus, fetch_all_dsm
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
        ("u", "push_screen('update')", "Update"),
        ("r", "push_screen('scripts')", "Scripts"),
        ("c", "refresh_data", "Refresh"),
        ("q", "app.quit", "Quit"),
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
        self._dsm: list[DSMStatus] = []
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
                yield Static("💾 Synology DSM", classes="panel-title")
                with ScrollableContainer(classes="panel", id="dsm-panel"):
                    yield DataTable(id="dsm-table", show_cursor=False)
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
        vms_t.add_columns("", "Name", "Node", "CPU", "Memory", "Uptime")

        lxcs_t = self.query_one("#lxcs-table", DataTable)
        lxcs_t.add_columns("", "Name", "Node", "CPU", "Memory", "Uptime")

        dsm_t = self.query_one("#dsm-table", DataTable)
        dsm_t.add_columns("NAS", "Status", "CPU", "Memory", "Volumes")

        certs_t = self.query_one("#certs-table", DataTable)
        certs_t.add_columns("Name", "Expires", "Days")

    @work(exclusive=True)
    async def refresh_data(self) -> None:
        self.query_one("#status-bar", Static).update("⏳ Refreshing…")
        try:
            self._cluster = await fetch_cluster_status(self.config.proxmox)
            self._dsm = await fetch_all_dsm(self.config.dsm_instances, no_prompt=True)
            if self.config.tls_checks:
                self._certs = await check_all_certs(self.config.tls_checks)
            self._populate_tables()
            from datetime import datetime
            ts = datetime.now().strftime("%H:%M:%S")
            self.query_one("#status-bar", Static).update(
                f"✅ Last updated: {ts}  |  [u] Update  [r] Scripts  [c] Refresh  [q] Quit"
            )
        except Exception as e:
            self.query_one("#status-bar", Static).update(f"❌ Error refreshing: {e}")

    def _populate_tables(self) -> None:
        if not self._cluster:
            return

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
            icon = _status_icon(vm.status)
            cpu = f"{vm.cpu * 100:.0f}%" if vm.status == "running" else "—"
            mem = (
                f"{_fmt_bytes(vm.mem)}/{_fmt_bytes(vm.maxmem)}"
                if vm.status == "running" and vm.maxmem > 0 else "—"
            )
            uptime = _fmt_uptime(vm.uptime) if vm.status == "running" else vm.status
            vms_t.add_row(icon, vm.name, vm.node, cpu, mem, uptime)

        # ---- LXCs ----
        lxcs_t = self.query_one("#lxcs-table", DataTable)
        lxcs_t.clear()
        lxcs = sorted(
            [g for g in self._cluster.guests if g.type == "lxc"],
            key=lambda g: (g.status != "running", g.name)
        )
        for ct in lxcs:
            icon = _status_icon(ct.status)
            cpu = f"{ct.cpu * 100:.0f}%" if ct.status == "running" else "—"
            mem = (
                f"{_fmt_bytes(ct.mem)}/{_fmt_bytes(ct.maxmem)}"
                if ct.status == "running" and ct.maxmem > 0 else "—"
            )
            uptime = _fmt_uptime(ct.uptime) if ct.status == "running" else ct.status
            lxcs_t.add_row(icon, ct.name, ct.node, cpu, mem, uptime)

        # ---- DSM ----
        dsm_t = self.query_one("#dsm-table", DataTable)
        dsm_t.clear()
        for dsm in self._dsm:
            status = "🟢 Online" if dsm.online else f"🔴 {dsm.error[:20]}"
            cpu = f"{dsm.cpu_percent:.0f}%" if dsm.online else "—"
            mem = f"{dsm.mem_percent:.0f}%" if dsm.online else "—"
            vols = "  ".join(
                f"{v['name']}: {v['percent']:.0f}% ({v['used_gb']}/{v['total_gb']}G)"
                for v in dsm.volumes
            ) if dsm.online else "—"
            dsm_t.add_row(dsm.name, status, cpu, mem, vols or "—")

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
