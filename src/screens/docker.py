"""Docker/Portainer screen — container list across all environments.

Select a row and press [s]/[x]/[r] to start/stop/restart, [l] to view logs.
"""
from __future__ import annotations

import sys
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Log, Static

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config

_STATE_ICON = {
    "running": "🟢", "restarting": "🟡", "paused": "⚫",
    "exited": "🔴", "dead": "🔴",
}


class DockerScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        Binding("l", "view_logs", "Logs", priority=True),
        Binding("s", "start_container", "Start", priority=True),
        Binding("x", "stop_container", "Stop", priority=True),
        Binding("r", "restart_container", "Restart", priority=True),
        Binding("R", "refresh_data", "Refresh", priority=True),
    ]

    DEFAULT_CSS = """
    DockerScreen { layout: vertical; }
    #status-bar { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #table-container { height: 1fr; border: round $accent; margin: 1; padding: 0 1; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._containers: list = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Loading…", id="status-bar")
        with Vertical(id="table-container"):
            yield DataTable(id="containers-table", show_cursor=True, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#containers-table", DataTable)
        t.add_columns("Env", "Container", "State", "Health", "Status")
        self.call_after_refresh(self._load)

    def action_refresh_data(self) -> None:
        self._load()

    @work(exclusive=True, thread=True)
    def _load(self) -> None:
        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update, "⏳ Fetching containers…"
        )
        try:
            from lib.portainer import fetch_containers, fetch_environments

            pt_cfg = self.config.portainer
            if not pt_cfg:
                self.app.call_from_thread(
                    self.query_one("#status-bar", Static).update,
                    "❌ Portainer not configured (see config.yaml)",
                )
                return

            envs = fetch_environments(pt_cfg)
            rows = []
            for e in envs:
                if e.status != 1:
                    continue
                rows.extend(fetch_containers(pt_cfg, e.id, e.name, all_containers=True))
            rows.sort(key=lambda c: (c.env_name, c.name))
            self._containers = rows

            t = self.query_one("#containers-table", DataTable)
            self.app.call_from_thread(t.clear)
            for c in rows:
                icon = _STATE_ICON.get(c.state, "⚫")
                self.app.call_from_thread(
                    t.add_row, c.env_name, c.name, f"{icon} {c.state}",
                    c.health or "—", c.status,
                )

            unhealthy = sum(1 for c in rows if c.needs_attention)
            summary = (
                f"{len(rows)} containers  |  "
                + (f"⚠️  {unhealthy} need attention  |  " if unhealthy else "")
                + "[l] Logs  [s] Start  [x] Stop  [r] Restart  [R] Refresh  |  Esc to go back"
            )
            self.app.call_from_thread(self.query_one("#status-bar", Static).update, summary)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )

    def _selected_container(self):
        t = self.query_one("#containers-table", DataTable)
        if t.cursor_row is None or not self._containers:
            return None
        try:
            return self._containers[t.cursor_row]
        except IndexError:
            return None

    def action_view_logs(self) -> None:
        c = self._selected_container()
        if c:
            self.app.push_screen(DockerLogsScreen(self.config, c.env_id, c.env_name, c.id, c.name))

    def action_start_container(self) -> None:
        self._do_action("start")

    def action_stop_container(self) -> None:
        self._do_action("stop")

    def action_restart_container(self) -> None:
        self._do_action("restart")

    def _do_action(self, action: str) -> None:
        c = self._selected_container()
        if not c:
            return
        self._run_action(c.env_id, c.id, c.name, action)

    @work(exclusive=True, thread=True)
    def _run_action(self, env_id: int, container_id: str, name: str, action: str) -> None:
        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update, f"⏳ {action}ing {name}…"
        )
        try:
            from lib.portainer import container_action
            pt_cfg = self.config.portainer
            container_action(pt_cfg, env_id, container_id, action)
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"✅ {name} {action} sent"
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {action} failed: {e}"
            )
        self._load()


class DockerLogsScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        Binding("R", "refresh_data", "Refresh", priority=True),
    ]

    DEFAULT_CSS = """
    DockerLogsScreen { layout: vertical; }
    #status-bar { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #log-container { height: 1fr; border: round $accent; margin: 1; padding: 0 1; }
    """

    def __init__(self, config: Config, env_id: int, env_name: str, container_id: str, name: str) -> None:
        super().__init__()
        self.config = config
        self.env_id = env_id
        self.env_name = env_name
        self.container_id = container_id
        self.container_name = name

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(f"Loading logs for {self.container_name}…", id="status-bar")
        with Vertical(id="log-container"):
            yield Log(id="log-view", highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self._load)

    def action_refresh_data(self) -> None:
        self._load()

    @work(exclusive=True, thread=True)
    def _load(self) -> None:
        try:
            from lib.portainer import fetch_container_logs
            pt_cfg = self.config.portainer
            text = fetch_container_logs(pt_cfg, self.env_id, self.container_id, tail=300)

            log = self.query_one("#log-view", Log)
            self.app.call_from_thread(log.clear)
            for line in text.splitlines():
                self.app.call_from_thread(log.write_line, line)

            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update,
                f"{self.env_name} / {self.container_name}  |  [R] Refresh  |  Esc to go back",
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )
