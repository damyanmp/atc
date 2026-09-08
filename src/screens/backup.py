"""Proxmox backups screen — scheduled jobs + latest backup per guest.

Select a row in the jobs table and press Enter to trigger an immediate run.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config


def _fmt_bytes(b: int) -> str:
    if b >= 1024 ** 3:
        return f"{b / 1024**3:.1f}G"
    if b >= 1024 ** 2:
        return f"{b / 1024**2:.0f}M"
    return f"{b / 1024:.0f}K"


class BackupScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        Binding("enter", "trigger_job", "Trigger", priority=True),
        Binding("R", "refresh_data", "Refresh", priority=True),
    ]

    DEFAULT_CSS = """
    BackupScreen { layout: vertical; }
    #status-bar { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #jobs-section { height: auto; max-height: 12; margin: 1 1 0 1; border: round $accent; padding: 0 1; }
    #latest-section { height: 1fr; margin: 1; border: round $accent; padding: 0 1; }
    .section-label { text-style: bold; color: $accent; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._jobs: list = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Loading…", id="status-bar")
        with Vertical(id="jobs-section"):
            yield Static("Scheduled Jobs  (Enter to trigger)", classes="section-label")
            yield DataTable(id="jobs-table", show_cursor=True, cursor_type="row")
        with Vertical(id="latest-section"):
            yield Static("Latest Backup per Guest", classes="section-label")
            yield DataTable(id="latest-table", show_cursor=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#jobs-table", DataTable).add_columns(
            "", "ID", "Schedule", "Storage", "VMs", "Node", "Comment"
        )
        self.query_one("#latest-table", DataTable).add_columns(
            "ID", "Name", "Latest Backup", "Size", "Count", "Storage"
        )
        self.call_after_refresh(self._load)

    def action_refresh_data(self) -> None:
        self._load()

    @work(exclusive=True, thread=True)
    def _load(self) -> None:
        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update, "⏳ Fetching backup data…"
        )
        try:
            from lib.proxmox import fetch_cluster_status, list_backup_jobs, list_backups

            cfg = self.config.proxmox
            import asyncio
            cluster = asyncio.run(fetch_cluster_status(cfg))
            online_nodes = [n.name for n in cluster.nodes if n.online]
            name_map = {g.vmid: g.name for g in cluster.guests}

            jobs = list_backup_jobs(cfg)
            self._jobs = jobs
            jt = self.query_one("#jobs-table", DataTable)
            self.app.call_from_thread(jt.clear)
            for j in jobs:
                dot = "🟢" if j.enabled else "⚫"
                self.app.call_from_thread(
                    jt.add_row, dot, j.id, j.schedule, j.storage,
                    j.vmids, j.node or "all", j.comment or "—",
                )

            backups = list_backups(cfg, online_nodes)
            grouped: dict[int, list] = {}
            for b in backups:
                grouped.setdefault(b.vmid, []).append(b)

            lt = self.query_one("#latest-table", DataTable)
            self.app.call_from_thread(lt.clear)
            for vmid in sorted(grouped):
                files = grouped[vmid]
                latest = files[0]
                dt = (
                    datetime.fromtimestamp(latest.ctime).strftime("%Y-%m-%d %H:%M")
                    if latest.ctime else "—"
                )
                self.app.call_from_thread(
                    lt.add_row, str(vmid), name_map.get(vmid, f"vmid-{vmid}"),
                    dt, _fmt_bytes(latest.size), str(len(files)), latest.storage,
                )

            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update,
                f"{len(jobs)} scheduled job(s)  |  {len(grouped)} guest(s) backed up  |  "
                f"[Enter] Trigger  [R] Refresh  |  Esc to go back",
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )

    def action_trigger_job(self) -> None:
        jt = self.query_one("#jobs-table", DataTable)
        if jt.cursor_row is None or not self._jobs:
            return
        try:
            job = self._jobs[jt.cursor_row]
        except IndexError:
            return
        self._run_trigger(job.id)

    @work(exclusive=True, thread=True)
    def _run_trigger(self, job_id: str) -> None:
        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update, f"⏳ Triggering {job_id}…"
        )
        try:
            from lib.proxmox import run_backup_job
            ok = run_backup_job(self.config.proxmox, job_id)
            msg = f"✅ Job {job_id} started" if ok else f"❌ Failed to trigger {job_id}"
            self.app.call_from_thread(self.query_one("#status-bar", Static).update, msg)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )
