"""Script launcher screen — browse and run *.sh scripts from the repo."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button, Footer, Header, Label, ListItem, ListView, Log, Static
)

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import Config


def _get_script_description(path: Path) -> str:
    """Read the first comment line from a script as its description."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#!"):
                    continue
                if line.startswith("#"):
                    return line.lstrip("#").strip()
                if line:
                    break
    except Exception:
        pass
    return ""


def _find_scripts(scripts_dir: Path) -> list[Path]:
    """Recursively find all *.sh files, excluding .venv and hidden dirs."""
    found = []
    for p in sorted(scripts_dir.rglob("*.sh")):
        # Skip hidden dirs and virtual envs
        parts = p.parts
        if any(part.startswith(".") or part == "__pycache__" or part == ".venv" for part in parts):
            continue
        found.append(p)
    return found


class ScriptItem(ListItem):
    def __init__(self, path: Path, scripts_dir: Path) -> None:
        self.script_path = path
        rel = path.relative_to(scripts_dir) if path.is_relative_to(scripts_dir) else path
        desc = _get_script_description(path)
        label = f"  {rel}\n  [dim]{desc}[/]" if desc else f"  {rel}"
        super().__init__(Label(label, markup=True))


class ScriptsScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("enter", "run_selected", "Run"),
    ]

    DEFAULT_CSS = """
    ScriptsScreen {
        layout: horizontal;
    }
    #left-panel {
        width: 50;
        layout: vertical;
        border-right: solid $accent;
        padding: 1;
    }
    #right-panel {
        width: 1fr;
        layout: vertical;
        padding: 1;
    }
    #script-list {
        height: 1fr;
        border: round $accent;
    }
    #controls {
        height: auto;
        layout: horizontal;
        margin-top: 1;
    }
    #controls Button {
        margin-right: 1;
    }
    #output-log {
        height: 1fr;
        border: round $accent;
    }
    #script-info {
        height: 3;
        padding: 0 1;
        color: $text-muted;
    }
    .section-label {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._scripts: list[Path] = []
        self._running = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left-panel"):
                yield Static("Repo Scripts", classes="section-label")
                yield ListView(id="script-list")
                with Horizontal(id="controls"):
                    yield Button("▶  Run", id="run-btn", variant="success")
                    yield Button("✕ Back", id="back-btn", variant="default")
            with Vertical(id="right-panel"):
                yield Static("Output", classes="section-label")
                yield Static("Select a script and press Run (or Enter)", id="script-info")
                yield Log(id="output-log", highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self._load_scripts()

    def _load_scripts(self) -> None:
        scripts_dir = self.config.resolved_scripts_dir
        self._scripts = _find_scripts(scripts_dir)
        lv = self.query_one("#script-list", ListView)
        lv.clear()
        if not self._scripts:
            lv.append(ListItem(Label(f"  No scripts found in\n  {scripts_dir}")))
        else:
            for path in self._scripts:
                lv.append(ScriptItem(path, scripts_dir))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, ScriptItem):
            rel = event.item.script_path.relative_to(self.config.resolved_scripts_dir) \
                if event.item.script_path.is_relative_to(self.config.resolved_scripts_dir) \
                else event.item.script_path
            self.query_one("#script-info", Static).update(f"📄 {rel}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-btn":
            self.action_run_selected()
        elif event.button.id == "back-btn":
            self.app.pop_screen()

    def action_run_selected(self) -> None:
        lv = self.query_one("#script-list", ListView)
        if lv.highlighted_child and isinstance(lv.highlighted_child, ScriptItem):
            self._run_script(lv.highlighted_child.script_path)

    @work(exclusive=True)
    async def _run_script(self, path: Path) -> None:
        if self._running:
            return
        self._running = True
        log = self.query_one("#output-log", Log)
        log.clear()
        self.query_one("#run-btn", Button).disabled = True

        rel = path.relative_to(self.config.resolved_scripts_dir) \
            if path.is_relative_to(self.config.resolved_scripts_dir) else path
        log.write_line(f"▶  Running: {rel}")
        log.write_line("─" * 60)

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(path.parent),
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip("\n")
                log.write_line(line)

            await proc.wait()
            icon = "✅" if proc.returncode == 0 else "❌"
            log.write_line("─" * 60)
            log.write_line(f"{icon}  Exit code: {proc.returncode}")
        except Exception as e:
            log.write_line(f"❌  Failed to run script: {e}")
        finally:
            self._running = False
            self.query_one("#run-btn", Button).disabled = False
