"""Script launcher screen — browse and run *.sh scripts from the repo."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Log, Static

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
    """Recursively find all *.sh files, excluding hidden dirs, .venv, and symlinked dirs."""
    _SKIP = {"__pycache__", ".venv"}
    found = []
    for root, dirs, files in os.walk(scripts_dir, followlinks=False):
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".") and d not in _SKIP
        )
        root_path = Path(root)
        for fname in files:
            if fname.endswith(".sh"):
                found.append(root_path / fname)
    return sorted(found)


class ScriptItem(ListItem):
    def __init__(self, path: Path, scripts_dir: Path) -> None:
        self.script_path = path
        rel = path.relative_to(scripts_dir) if path.is_relative_to(scripts_dir) else path
        desc = _get_script_description(path)
        label = f"  {rel}\n  [dim]{desc}[/]" if desc else f"  {rel}"
        super().__init__(Label(label, markup=True))


class ScriptListView(ListView):
    """ListView that triggers script execution on Enter via key_enter.

    key_enter is invoked by dispatch_key directly on the focused widget,
    bypassing the binding chain entirely — the most reliable hook for this.
    """

    def key_enter(self) -> None:
        item = self.highlighted_child
        if item is None and self._nodes:
            self.index = 0
            item = self.highlighted_child
        if isinstance(item, ScriptItem):
            # Cast is safe: ScriptListView is only used inside ScriptsScreen
            screen = self.screen
            if isinstance(screen, ScriptsScreen):
                screen.run_script(item.script_path)


class ScriptsScreen(Screen):
    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left-panel"):
                yield Static("Repo Scripts", classes="section-label")
                yield ScriptListView(id="script-list")
            with Vertical(id="right-panel"):
                yield Static("Output", classes="section-label")
                yield Static("Select a script and press Enter to run", id="script-info")
                yield Log(id="output-log", highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self._load_scripts)

    def _load_scripts(self) -> None:
        try:
            scripts_dir = self.config.resolved_scripts_dir
            self._scripts = _find_scripts(scripts_dir)
            lv = self.query_one("#script-list", ScriptListView)
            lv.clear()
            if not self._scripts:
                lv.append(ListItem(Label(f"  No scripts found in\n  {scripts_dir}")))
            else:
                for path in self._scripts:
                    lv.append(ScriptItem(path, scripts_dir))
                lv.index = 0
            lv.focus()
        except Exception as e:
            try:
                lv = self.query_one("#script-list", ScriptListView)
                lv.clear()
                lv.append(ListItem(Label(f"  [red]Error loading scripts:[/]\n  {e}", markup=True)))
            except Exception:
                pass

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, ScriptItem):
            rel = event.item.script_path.relative_to(self.config.resolved_scripts_dir) \
                if event.item.script_path.is_relative_to(self.config.resolved_scripts_dir) \
                else event.item.script_path
            self.query_one("#script-info", Static).update(f"📄 {rel}")

    def run_script(self, path: Path) -> None:
        """Public entry point called by ScriptListView.key_enter."""
        self._run_script(path)

    @work(exclusive=True)
    async def _run_script(self, path: Path) -> None:
        import subprocess

        rel = path.relative_to(self.config.resolved_scripts_dir) \
            if path.is_relative_to(self.config.resolved_scripts_dir) else path

        # Suspend the TUI and hand the terminal to the script directly.
        # This gives scripts full stdin/stdout/stderr — interactive prompts work.
        # The TUI resumes automatically when the script exits.
        with self.app.suspend():
            result = subprocess.run(
                ["bash", str(path)],
                cwd=str(path.parent),
            )

        log = self.query_one("#output-log", Log)
        log.clear()
        log.write_line(f"▶  Ran: {rel}")
        log.write_line("─" * 60)
        icon = "✅" if result.returncode == 0 else "❌"
        log.write_line(f"{icon}  Exit code: {result.returncode}")
