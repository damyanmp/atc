"""Plex screen — file-manager browser (left) + stats/history (right)."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from textwrap import shorten

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, Static

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from config import Config

_TYPE_ICON = {
    "movie": "🎬", "show": "📺", "season": "📅", "episode": "▶",
    "artist": "🎤", "album": "💿", "track": "🎵", "photo": "🖼",
}
_STATE_ICON = {"playing": "▶", "paused": "⏸", "buffering": "⏳"}


def _dur(ms: int) -> str:
    if not ms or ms <= 0:
        return "—"
    h, rem = divmod(ms // 1000, 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"


def _dur_s(secs: int) -> str:
    return _dur(secs * 1000)


def _stars(r: float) -> str:
    return f"★ {r:.1f}" if r else "—"


@dataclass
class NavLevel:
    level_type: str   # "root" | "library" | "show" | "season" | "artist" | "album"
    title: str
    items: list       # PlexLibrary list at root; PlexItem list elsewhere
    lib_type: str = ""


class PlexScreen(Screen):
    # Only bind keys that don't conflict with App-level nav bindings.
    # "r" is taken by App (Scripts), so we use F5 for reload.
    BINDINGS = [
        ("escape",    "back_or_pop",  "Back"),
        ("backspace", "go_up",        "Up"),
        ("slash",     "focus_filter", "Filter"),
        ("ctrl+r",    "reload",       "Reload"),
    ]

    DEFAULT_CSS = """
    PlexScreen { layout: vertical; }

    #breadcrumb {
        height: 1;
        background: $surface;
        color: $accent;
        padding: 0 1;
        text-style: bold;
    }
    #main { height: 1fr; layout: horizontal; }

    /* ── Left pane ── */
    #left-col {
        width: 2fr;
        min-width: 30;
        layout: vertical;
        border-right: solid $accent;
    }
    /* Filter is hidden by default; shown only when user presses / */
    #filter-input {
        height: 3;
        margin: 0;
        display: none;
    }
    #filter-input.visible { display: block; }
    #browser-table { height: 1fr; }

    /* ── Right pane ── */
    #right-col {
        width: 3fr;
        layout: vertical;
        padding: 0 1;
    }
    .section-title {
        height: 1;
        text-style: bold;
        color: $accent;
        margin-top: 1;
    }
    #streams-table { height: auto; max-height: 5; }
    #detail-panel {
        height: auto;
        min-height: 6;
        max-height: 12;
        border: round $surface;
        padding: 0 1;
        margin: 1 0 0 0;
        color: $text;
    }
    #history-table { height: 1fr; }

    #status-bar {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._nav_stack: list[NavLevel] = []
        self._displayed: list = []

    # ── Layout ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Libraries", id="breadcrumb")
        with Horizontal(id="main"):
            with Vertical(id="left-col"):
                # Hidden by default; revealed by action_focus_filter
                yield Input(placeholder="Filter…  (Enter or Esc to close)", id="filter-input")
                yield DataTable(id="browser-table", show_cursor=True, cursor_type="row")
            with Vertical(id="right-col"):
                yield Static("Active Streams", classes="section-title")
                yield DataTable(id="streams-table", show_cursor=False)
                yield Static("", id="detail-panel")
                yield Static("Playback History", classes="section-title")
                yield DataTable(id="history-table", show_cursor=True)
        yield Static(
            "↑↓ navigate  Enter open  Bksp up  / filter  ^R reload  Esc back",
            id="status-bar",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#streams-table", DataTable).add_columns(
            "", "User", "Title", "Player", "Quality", "%"
        )
        self.query_one("#history-table", DataTable).add_columns(
            "User", "Title", "Type", "Played", "Duration", "Done"
        )
        self.query_one("#browser-table", DataTable).add_columns(
            "", "Library", "Type", "Items"
        )
        # Give the browser table focus after the first frame so it receives
        # keyboard input immediately without the Input stealing it.
        self.set_timer(0.05, self._focus_browser)
        self.call_after_refresh(self._load_root)

    def _focus_browser(self) -> None:
        self.query_one("#browser-table", DataTable).focus()

    # ── Filter ───────────────────────────────────────────────────────────────

    def action_focus_filter(self) -> None:
        inp = self.query_one("#filter-input", Input)
        inp.add_class("visible")
        inp.focus()

    def _close_filter(self) -> None:
        inp = self.query_one("#filter-input", Input)
        inp.remove_class("visible")
        self._focus_browser()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "filter-input" or not self._nav_stack:
            return
        text = event.value.lower()
        level = self._nav_stack[-1]
        self._displayed = (
            [i for i in level.items
             if text in (i.title if hasattr(i, "title") else "").lower()]
            if text else list(level.items)
        )
        self._repopulate_browser(level)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._close_filter()

    def on_key(self, event) -> None:
        """Close filter on Escape when it has focus."""
        if event.key == "escape" and self.query_one("#filter-input", Input).has_focus:
            inp = self.query_one("#filter-input", Input)
            inp.clear()
            self._close_filter()
            event.stop()

    # ── Browser table events ──────────────────────────────────────────────────

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "browser-table":
            return
        idx = event.cursor_row
        if 0 <= idx < len(self._displayed):
            self._update_detail(self._displayed[idx])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "browser-table":
            return
        idx = event.cursor_row
        if 0 <= idx < len(self._displayed):
            self._navigate_into(self._displayed[idx])

    # ── Navigation ───────────────────────────────────────────────────────────

    def action_back_or_pop(self) -> None:
        if self.query_one("#filter-input", Input).has_focus:
            # handled by on_key
            return
        if len(self._nav_stack) > 1:
            self.action_go_up()
        else:
            self.app.pop_screen()

    def action_go_up(self) -> None:
        if len(self._nav_stack) <= 1:
            return
        self._nav_stack.pop()
        self.query_one("#filter-input", Input).clear()
        level = self._nav_stack[-1]
        self._displayed = list(level.items)
        self._update_breadcrumb()
        self._repopulate_browser(level)
        self._focus_browser()

    def action_reload(self) -> None:
        self._nav_stack.clear()
        self._displayed.clear()
        self.query_one("#filter-input", Input).clear()
        self.query_one("#detail-panel", Static).update("")
        self._load_root()

    def _navigate_into(self, item) -> None:
        # Identify by duck-typing rather than isinstance to avoid import-cache issues
        if hasattr(item, "key") and hasattr(item, "lib_type") and not hasattr(item, "rating_key"):
            # PlexLibrary — has .key (section ID) but not .rating_key
            self._load_library(item)
        elif hasattr(item, "rating_key"):
            # PlexItem
            if item.lib_type in ("show", "season", "artist", "album"):
                self._load_children(item)
            # movies / episodes / tracks: detail shown on right, Enter is a no-op

    def _update_breadcrumb(self) -> None:
        path = " › ".join(lvl.title for lvl in self._nav_stack)
        self.query_one("#breadcrumb", Static).update(path or "Libraries")

    # ── Detail panel ─────────────────────────────────────────────────────────

    def _update_detail(self, item) -> None:
        panel = self.query_one("#detail-panel", Static)

        # Library
        if hasattr(item, "key") and not hasattr(item, "rating_key"):
            panel.update(
                f"[bold]{item.title}[/bold]\n"
                f"Type: {item.lib_type}  |  Items: {item.count:,}"
            )
            return

        # PlexItem
        if not hasattr(item, "rating_key"):
            panel.update("")
            return

        icon = _TYPE_ICON.get(item.lib_type, "")
        lines: list[str] = []

        title_str = item.title
        if item.lib_type == "episode" and item.parent_title:
            ep_tag = (
                f"S{item.season:02d}E{item.episode:02d}"
                if item.season and item.episode else ""
            )
            title_str = f"{item.parent_title} {ep_tag} – {item.title}".strip()
        elif item.lib_type == "season" and item.parent_title:
            title_str = f"{item.parent_title}: {item.title}"

        year_str = f" ({item.year})" if item.year else ""
        lines.append(f"[bold]{icon} {title_str}{year_str}[/bold]")

        stats: list[str] = []
        if item.rating:
            stats.append(f"★ {item.rating:.1f}")
        if item.audience_rating:
            stats.append(f"👥 {item.audience_rating:.1f}")
        if item.duration_ms:
            stats.append(_dur(item.duration_ms))
        if item.lib_type == "show":
            if item.child_count:
                stats.append(f"{item.child_count} season{'s' if item.child_count != 1 else ''}")
            if item.leaf_count:
                stats.append(f"{item.leaf_count} ep")
        if item.lib_type == "season" and item.leaf_count:
            stats.append(f"{item.leaf_count} episodes")
        if stats:
            lines.append("  ".join(stats))

        meta: list[str] = []
        if item.studio:
            meta.append(item.studio)
        if item.genres:
            meta.append(item.genres[:60])
        if item.directors:
            meta.append(f"Dir: {item.directors[:50]}")
        if item.added_at:
            meta.append(f"Added {item.added_at}")
        if meta:
            lines.append("  |  ".join(meta))

        if item.summary:
            lines.append("")
            lines.append(shorten(item.summary, width=220, placeholder="…"))

        panel.update("\n".join(lines))

    # ── Table repopulation ────────────────────────────────────────────────────

    def _repopulate_browser(self, level: NavLevel) -> None:
        bt = self.query_one("#browser-table", DataTable)
        bt.clear(columns=True)
        lt = level.level_type

        if lt == "root":
            bt.add_columns("", "Library", "Type", "Items")
            for lib in self._displayed:
                icon = _TYPE_ICON.get(lib.lib_type, "📁")
                bt.add_row(icon, lib.title, lib.lib_type, f"{lib.count:,}")

        elif lt == "library":
            if level.lib_type == "show":
                bt.add_columns("", "Title", "Year", "Seasons", "Episodes")
                for item in self._displayed:
                    bt.add_row(
                        _TYPE_ICON.get(item.lib_type, "📺"),
                        item.title,
                        str(item.year) if item.year else "—",
                        str(item.child_count) if item.child_count else "—",
                        str(item.leaf_count) if item.leaf_count else "—",
                    )
            elif level.lib_type == "artist":
                bt.add_columns("", "Artist", "Albums", "Tracks")
                for item in self._displayed:
                    bt.add_row(
                        _TYPE_ICON.get(item.lib_type, "🎤"),
                        item.title,
                        str(item.child_count) if item.child_count else "—",
                        str(item.leaf_count) if item.leaf_count else "—",
                    )
            else:
                bt.add_columns("", "Title", "Year", "Rating", "Duration")
                for item in self._displayed:
                    bt.add_row(
                        _TYPE_ICON.get(item.lib_type, "🎬"),
                        item.title,
                        str(item.year) if item.year else "—",
                        _stars(item.rating),
                        _dur(item.duration_ms),
                    )

        elif lt == "show":
            bt.add_columns("", "Season", "Episodes", "Added")
            for item in self._displayed:
                label = item.title or f"Season {item.season}"
                bt.add_row(
                    _TYPE_ICON.get(item.lib_type, "📅"),
                    label,
                    str(item.leaf_count) if item.leaf_count else "—",
                    item.added_at or "—",
                )

        elif lt == "artist":
            bt.add_columns("", "Album", "Year", "Tracks")
            for item in self._displayed:
                bt.add_row(
                    _TYPE_ICON.get(item.lib_type, "💿"),
                    item.title,
                    str(item.year) if item.year else "—",
                    str(item.leaf_count) if item.leaf_count else "—",
                )

        elif lt in ("season", "album"):
            bt.add_columns("", "#", "Title", "Rating", "Duration")
            for item in self._displayed:
                num = str(item.episode) if item.episode else "—"
                bt.add_row(
                    _TYPE_ICON.get(item.lib_type, "▶"),
                    num, item.title,
                    _stars(item.rating),
                    _dur(item.duration_ms),
                )

    # ── Workers ───────────────────────────────────────────────────────────────

    @work(exclusive=True, thread=True)
    def _load_root(self) -> None:
        self.app.call_from_thread(
            self.query_one("#breadcrumb", Static).update, "⏳ Loading…"
        )

        if self.config.plex:
            try:
                from lib.plex import get_server_info, list_libraries
                info = get_server_info(self.config.plex)
                server_name = info.friendly_name or "Plex"
                libs = list_libraries(self.config.plex)
                if isinstance(libs, list):
                    root = NavLevel(level_type="root", title=server_name, items=libs)
                    self._nav_stack = [root]
                    self._displayed = list(libs)
                    self.app.call_from_thread(self._update_breadcrumb)
                    self.app.call_from_thread(self._repopulate_browser, root)
                    self.app.call_from_thread(self._focus_browser)
                else:
                    self.app.call_from_thread(
                        self.query_one("#breadcrumb", Static).update,
                        f"❌ {libs[:70]}"
                    )
            except Exception as e:
                self.app.call_from_thread(
                    self.query_one("#breadcrumb", Static).update, f"❌ {e}"
                )
        else:
            self.app.call_from_thread(
                self.query_one("#breadcrumb", Static).update, "Plex not configured"
            )

        if self.config.tautulli:
            try:
                from lib.tautulli import get_activity, get_history
                streams = get_activity(self.config.tautulli)
                st = self.query_one("#streams-table", DataTable)
                self.app.call_from_thread(st.clear)
                if streams:
                    for s in streams:
                        title = (
                            f"{s.grandparent_title} S{s.season:02d}E{s.episode:02d} – {s.title}"
                            if s.grandparent_title else s.title
                        )
                        self.app.call_from_thread(
                            st.add_row,
                            _STATE_ICON.get(s.state, "?"),
                            s.user, title[:38], s.player[:14],
                            (s.quality_profile or s.transcode_decision)[:12],
                            f"{s.progress_pct:.0f}%",
                        )
                else:
                    self.app.call_from_thread(
                        st.add_row, "—", "No active streams", "", "", "", ""
                    )

                history = get_history(self.config.tautulli, length=50)
                ht = self.query_one("#history-table", DataTable)
                self.app.call_from_thread(ht.clear)
                for rec in history:
                    title = (
                        f"{rec.grandparent_title} – {rec.title}"
                        if rec.grandparent_title else rec.title
                    )
                    media_icon = (
                        "🎬" if rec.media_type == "movie" else
                        ("📺" if rec.media_type == "episode" else "🎵")
                    )
                    self.app.call_from_thread(
                        ht.add_row,
                        rec.user, title[:40], media_icon,
                        rec.played_at, _dur_s(rec.duration_secs),
                        f"{rec.percent_complete}%",
                    )
            except Exception as e:
                self.app.call_from_thread(
                    self.query_one("#status-bar", Static).update, f"❌ Tautulli: {e}"
                )
        else:
            self.app.call_from_thread(
                self.query_one("#streams-table", DataTable).add_row,
                "—", "Tautulli not configured", "", "", "", "",
            )

    @work(thread=True)
    def _load_library(self, lib) -> None:
        self.app.call_from_thread(
            self.query_one("#breadcrumb", Static).update, f"⏳ Loading {lib.title}…"
        )
        if not self.config.plex:
            return
        try:
            from lib.plex import browse_library
            items = browse_library(self.config.plex, lib.key)
            if isinstance(items, str):
                self.app.call_from_thread(
                    self.query_one("#status-bar", Static).update, f"❌ {items}"
                )
                return
            level = NavLevel(
                level_type="library", title=lib.title, items=items, lib_type=lib.lib_type
            )
            self._nav_stack.append(level)
            self._displayed = list(items)
            self.app.call_from_thread(self._update_breadcrumb)
            self.app.call_from_thread(self._repopulate_browser, level)
            self.app.call_from_thread(self._focus_browser)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )

    @work(thread=True)
    def _load_children(self, item) -> None:
        self.app.call_from_thread(
            self.query_one("#breadcrumb", Static).update, f"⏳ Loading {item.title}…"
        )
        if not self.config.plex:
            return
        try:
            from lib.plex import get_children
            children = get_children(self.config.plex, item.rating_key)
            if isinstance(children, str):
                self.app.call_from_thread(
                    self.query_one("#status-bar", Static).update, f"❌ {children}"
                )
                return
            child_type_map = {
                "show": "show", "season": "season",
                "artist": "artist", "album": "album",
            }
            level = NavLevel(
                level_type=child_type_map.get(item.lib_type, "show"),
                title=item.title,
                items=children,
                lib_type=item.lib_type,
            )
            self._nav_stack.append(level)
            self._displayed = list(children)
            self.app.call_from_thread(self._update_breadcrumb)
            self.app.call_from_thread(self._repopulate_browser, level)
            self.app.call_from_thread(self._focus_browser)
        except Exception as e:
            self.app.call_from_thread(
                self.query_one("#status-bar", Static).update, f"❌ {e}"
            )
