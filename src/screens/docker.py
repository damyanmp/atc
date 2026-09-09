"""Docker/Portainer screen — container list across all environments.

Select a row and press [s]/[x]/[r] to start/stop/restart, [l] to view logs,
[i] to inspect (image/env/labels), [S] to browse Compose stacks.
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
        Binding("i", "view_inspect", "Inspect", priority=True),
        Binding("s", "start_container", "Start", priority=True),
        Binding("x", "stop_container", "Stop", priority=True),
        Binding("r", "restart_container", "Restart", priority=True),
        Binding("S", "nav_stacks", "Stacks", priority=True),
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
        yield Static("Loading…", id="status-bar", markup=False)
        with Vertical(id="table-container"):
            yield DataTable(id="containers-table", show_cursor=True, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#containers-table", DataTable)
        t.add_columns("Env", "Container", "State", "Health", "Status")
        self.call_after_refresh(self._load)

    def action_refresh_data(self) -> None:
        self._load()

    def action_nav_stacks(self) -> None:
        self.app.push_screen(DockerStacksScreen(self.config))

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
                + "l Logs  i Inspect  s Start  x Stop  r Restart  S Stacks  R Refresh  |  Esc back"
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

    def action_view_inspect(self) -> None:
        c = self._selected_container()
        if c:
            self.app.push_screen(DockerInspectScreen(self.config, c))

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
        yield Static(f"Loading logs for {self.container_name}…", id="status-bar", markup=False)
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
                f"{self.env_name} / {self.container_name}  |  R Refresh  |  Esc back",
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )


class DockerInspectScreen(Screen):
    """Read-only detail view for a single container: image, labels, compose project."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        Binding("R", "refresh_data", "Refresh", priority=True),
    ]

    DEFAULT_CSS = """
    DockerInspectScreen { layout: vertical; }
    #status-bar { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #inspect-log { height: 1fr; border: round $accent; margin: 1; padding: 0 1; }
    """

    def __init__(self, config: Config, container) -> None:
        super().__init__()
        self.config = config
        self.container = container

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            f"Inspecting {self.container.name}…", id="status-bar", markup=False,
        )
        with Vertical(id="inspect-log"):
            yield Log(id="inspect-view", highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self._load)

    def action_refresh_data(self) -> None:
        self._load()

    @work(exclusive=True, thread=True)
    def _load(self) -> None:
        c = self.container
        log = self.query_one("#inspect-view", Log)
        self.app.call_from_thread(log.clear)

        lines = [
            f"Name:        {c.name}",
            f"Environment: {c.env_name}",
            f"Image:       {c.image}",
            f"State:       {c.state}",
            f"Health:      {c.health or '—'}",
            f"Status:      {c.status}",
            f"Compose project: {c.compose_project or '(none — not a Compose container)'}",
            "",
            "Labels:",
        ]
        for k, v in sorted((c.labels or {}).items()):
            lines.append(f"  {k} = {v}")

        for line in lines:
            self.app.call_from_thread(log.write_line, line)

        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update,
            f"{c.env_name} / {c.name}  |  R Refresh  |  Esc back",
        )


class DockerStacksScreen(Screen):
    """Compose stacks across all Portainer environments (read-only).

    Lists both Portainer-managed stacks and stacks inferred from Compose
    labels on containers (e.g. deployed outside atc/Portainer). Select a
    stack and press Enter to drill into its containers.
    """

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        Binding("enter", "view_stack", "Open", priority=True),
        Binding("R", "refresh_data", "Refresh", priority=True),
    ]

    DEFAULT_CSS = """
    DockerStacksScreen { layout: vertical; }
    #status-bar { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #table-container { height: 1fr; border: round $accent; margin: 1; padding: 0 1; }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._rows: list = []  # list of ("portainer"|"inferred", stack_obj)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Loading…", id="status-bar", markup=False)
        with Vertical(id="table-container"):
            yield DataTable(id="stacks-table", show_cursor=True, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#stacks-table", DataTable)
        t.add_columns("Environment", "Stack", "Status", "Source")
        self.call_after_refresh(self._load)

    def action_refresh_data(self) -> None:
        self._load()

    @work(exclusive=True, thread=True)
    def _load(self) -> None:
        self.app.call_from_thread(
            self.query_one("#status-bar", Static).update, "⏳ Fetching stacks…"
        )
        try:
            from lib.portainer import (
                fetch_environments, fetch_containers, fetch_stacks, infer_compose_stacks,
            )
            pt_cfg = self.config.portainer
            if not pt_cfg:
                self.app.call_from_thread(
                    self.query_one("#status-bar", Static).update,
                    "❌ Portainer not configured (see config.yaml)",
                )
                return

            envs = fetch_environments(pt_cfg)
            portainer_stacks = fetch_stacks(pt_cfg, envs)
            all_containers = []
            for e in envs:
                if e.status != 1:
                    continue
                all_containers.extend(fetch_containers(pt_cfg, e.id, e.name, all_containers=True))
            known_names = {s.name for s in portainer_stacks}
            inferred = infer_compose_stacks(all_containers, portainer_stack_names=known_names)

            rows = [("portainer", s) for s in sorted(portainer_stacks, key=lambda x: (x.env_name, x.name))]
            rows += [("inferred", s) for s in sorted(inferred, key=lambda x: (x.env_name, x.name))]
            self._rows = rows

            t = self.query_one("#stacks-table", DataTable)
            self.app.call_from_thread(t.clear)
            for kind, s in rows:
                if kind == "portainer":
                    status = "🟢 active" if s.status == 1 else "⚫ inactive"
                    self.app.call_from_thread(t.add_row, s.env_name, s.name, status, "portainer")
                else:
                    status = f"🟢 {s.running}/{s.total} running" if s.running else f"⚫ 0/{s.total} running"
                    self.app.call_from_thread(t.add_row, s.env_name, s.name, status, "compose labels")

            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update,
                f"{len(rows)} stack(s)  |  Enter to open  |  R Refresh  |  Esc back",
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )

    def _selected(self):
        t = self.query_one("#stacks-table", DataTable)
        if t.cursor_row is None or not self._rows:
            return None
        try:
            return self._rows[t.cursor_row]
        except IndexError:
            return None

    def action_view_stack(self) -> None:
        sel = self._selected()
        if not sel:
            return
        kind, s = sel
        if kind == "portainer":
            self.app.push_screen(DockerStackDetailScreen(self.config, s))
        else:
            # Inferred stacks have no Portainer stack id / compose file to fetch —
            # show what we already know (its member containers) instead.
            self.app.push_screen(DockerInferredStackScreen(self.config, s))


class DockerStackDetailScreen(Screen):
    """Read-only detail view for a Portainer-managed stack: compose file + env vars."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        Binding("R", "refresh_data", "Refresh", priority=True),
    ]

    DEFAULT_CSS = """
    DockerStackDetailScreen { layout: vertical; }
    #status-bar { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #detail-log { height: 1fr; border: round $accent; margin: 1; padding: 0 1; }
    """

    def __init__(self, config: Config, stack) -> None:
        super().__init__()
        self.config = config
        self.stack = stack

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(f"Loading {self.stack.name}…", id="status-bar", markup=False)
        with Vertical(id="detail-log"):
            yield Log(id="detail-view", highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self._load)

    def action_refresh_data(self) -> None:
        self._load()

    @work(exclusive=True, thread=True)
    def _load(self) -> None:
        s = self.stack
        log = self.query_one("#detail-view", Log)
        self.app.call_from_thread(log.clear)
        try:
            from lib.portainer import fetch_stack_file, fetch_stack_env
            pt_cfg = self.config.portainer

            env_vars = fetch_stack_env(pt_cfg, s.id)
            compose_content = fetch_stack_file(pt_cfg, s.id)

            lines = [
                f"Stack:       {s.name}  (id {s.id})",
                f"Environment: {s.env_name}",
                f"Status:      {'active' if s.status == 1 else 'inactive'}",
                "",
                f"Env vars ({len(env_vars)}) — values redacted:",
            ]
            for v in env_vars:
                lines.append(f"  {v.get('name', '?')} = ***")
            lines += ["", "── compose file ──"]
            lines += compose_content.splitlines()

            for line in lines:
                self.app.call_from_thread(log.write_line, line)

            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update,
                f"{s.env_name} / {s.name}  |  R Refresh  |  Esc back",
            )
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )


class DockerInferredStackScreen(Screen):
    """Read-only view of a Compose project inferred from container labels
    (not registered as a Portainer stack, so no compose file/env is available)."""

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
    ]

    DEFAULT_CSS = """
    DockerInferredStackScreen { layout: vertical; }
    #status-bar { height: 1; background: $surface; color: $text-muted; padding: 0 1; }
    #table-container { height: 1fr; border: round $accent; margin: 1; padding: 0 1; }
    """

    def __init__(self, config: Config, stack) -> None:
        super().__init__()
        self.config = config
        self.stack = stack

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            f"{self.stack.name}  |  not Portainer-managed — no compose file/env available  |  Esc back",
            id="status-bar",
            markup=False,
        )
        with Vertical(id="table-container"):
            yield DataTable(id="members-table", show_cursor=False)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#members-table", DataTable)
        t.add_columns("Container", "State", "Health", "Status")
        for c in self.stack.containers:
            icon = _STATE_ICON.get(c.state, "⚫")
            t.add_row(c.name, f"{icon} {c.state}", c.health or "—", c.status)
