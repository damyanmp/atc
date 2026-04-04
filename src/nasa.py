"""nasa — NASA / space mission tracking CLI.

Commands
  nasa                       Open interactive TUI dashboard
  nasa iss                   Current ISS position, altitude, speed, crew
  nasa artemis status        Live Artemis II telemetry from JPL Horizons
  nasa artemis missions      All Artemis missions (past & upcoming)
  nasa probes                Deep space probe fleet — distances & signal delays
  nasa rovers                Mars rover status (Curiosity & Perseverance)
  nasa launches [--n N]      Next N upcoming rocket launches
  nasa tui                   Alias for bare `nasa`

Data sources
  NASA/JPL Horizons API      https://ssd.jpl.nasa.gov/api/horizons.api
  NASA Open API              https://api.nasa.gov  (set NASA_API_KEY env var)
  open-notify.org            ISS crew & realtime lat/lon
  rocketlaunch.live          Upcoming launch schedule
"""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "lib"))

from lib.horizons import fetch_vectors, distance_speed

console = Console()

# ── TTL cache ──────────────────────────────────────────────────────────────────

@dataclass
class _CacheEntry:
    value: Any
    expires: float   # time.monotonic()


_CACHE: dict[str, _CacheEntry] = {}


def _cached(key: str) -> Optional[Any]:
    entry = _CACHE.get(key)
    if entry and time.monotonic() < entry.expires:
        return entry.value
    return None


def _cache_set(key: str, value: Any, ttl: float) -> None:
    _CACHE[key] = _CacheEntry(value=value, expires=time.monotonic() + ttl)


def _cache_clear() -> None:
    _CACHE.clear()

# ── Constants ──────────────────────────────────────────────────────────────────

EARTH_RADIUS_KM = 6_371.0
MOON_DIST_KM    = 384_400.0
AU_KM           = 149_597_870.7   # 1 AU in km
C_KM_S          = 299_792.458     # speed of light, km/s
ISS_NAIF_ID     = -125_544

OPEN_NOTIFY_ISS  = "http://api.open-notify.org/iss-now.json"
OPEN_NOTIFY_CREW = "http://api.open-notify.org/astros.json"
LAUNCH_API       = "https://fdo.rocketlaunch.live/json/launch/next/10"

# ── Artemis mission registry ───────────────────────────────────────────────────

@dataclass
class Mission:
    naif_id: int
    name: str
    nickname: str
    crewed: bool
    launch_utc: datetime
    splashdown_utc: Optional[datetime]
    status: str          # "upcoming" | "active" | "completed"
    crew: list[str] = field(default_factory=list)
    description: str = ""


ARTEMIS_MISSIONS: list[Mission] = [
    Mission(
        naif_id=-1023,
        name="Artemis I",
        nickname="Orion EM-1",
        crewed=False,
        launch_utc=datetime(2022, 11, 16, 6, 47, 44, tzinfo=timezone.utc),
        splashdown_utc=datetime(2022, 12, 11, 17, 40, 0, tzinfo=timezone.utc),
        status="completed",
        crew=[],
        description="First integrated test of SLS + Orion. Flew 25 days, 70,000+ km from Earth.",
    ),
    Mission(
        naif_id=-1024,
        name="Artemis II",
        nickname='"Integrity" Orion EM-2',
        crewed=True,
        launch_utc=datetime(2026, 4, 1, 22, 35, 12, tzinfo=timezone.utc),
        splashdown_utc=datetime(2026, 4, 11, 0, 17, 0, tzinfo=timezone.utc),
        status="active",
        crew=[
            "Reid Wiseman (CDR)",
            "Victor Glover (PLT)",
            "Christina Koch (MS)",
            "Jeremy Hansen (MS, CSA)",
        ],
        description="First crewed Artemis mission. 10-day free-return lunar flyby.",
    ),
]

# ── Deep space probe registry ──────────────────────────────────────────────────

@dataclass
class Probe:
    naif_id: int
    name: str
    launched: int   # year
    mission: str    # short description


# All queried Earth-centric (500@399) so distances reflect signal delay to Earth.
PROBES: list[Probe] = [
    Probe(-31,  "Voyager 1",          1977, "Interstellar — farthest human-made object"),
    Probe(-32,  "Voyager 2",          1977, "Interstellar — only probe at all 4 gas giants"),
    Probe(-98,  "New Horizons",       2006, "Kuiper Belt explorer"),
    Probe(-61,  "Juno",               2011, "Jupiter orbiter"),
    Probe(-96,  "Parker Solar Probe", 2018, "Solar corona — record close approach"),
    Probe(-159, "Europa Clipper",     2024, "En route to Jupiter / Europa"),
    Probe(-234, "Lucy",               2021, "Trojan asteroid explorer"),
    Probe(-202, "MAVEN",              2013, "Mars atmosphere orbiter"),
    Probe(-64,  "OSIRIS-APEX",        2016, "Asteroid Apophis rendezvous"),
]


@dataclass
class ProbeTelemetry:
    probe: Probe
    dist_earth_km: float
    speed_km_s: float
    error: str = ""

    @property
    def dist_au(self) -> float:
        return self.dist_earth_km / AU_KM

    @property
    def light_delay_min(self) -> float:
        return self.dist_earth_km / C_KM_S / 60 if self.dist_earth_km else 0.0


# ── Mars rover registry ────────────────────────────────────────────────────────

@dataclass
class RoverConfig:
    slug: str     # api.nasa.gov slug
    name: str
    naif_id: int  # Horizons ID for Earth-distance / light delay


@dataclass
class RoverConfig:
    slug: str
    name: str
    naif_id: int
    landing_date: str
    launch_date: str
    status: str
    mission: str


ROVERS: list[RoverConfig] = [
    RoverConfig(
        slug="curiosity", name="Curiosity", naif_id=-76,
        landing_date="2012-08-06", launch_date="2011-11-26",
        status="active",
        mission="Gale Crater — studying Mars climate & geology, habitability",
    ),
    RoverConfig(
        slug="perseverance", name="Perseverance", naif_id=-168,
        landing_date="2021-02-18", launch_date="2020-07-30",
        status="active",
        mission="Jezero Crater — astrobiology, sample caching for future return",
    ),
    RoverConfig(
        slug="ingenuity", name="Ingenuity (helo)", naif_id=-168,
        landing_date="2021-02-18", launch_date="2020-07-30",
        status="ended Jan 2024",
        mission="First powered flight on another planet — 72 flights",
    ),
]


@dataclass
class RoverStatus:
    name: str
    status: str
    mission: str
    landing_date: str
    dist_earth_km: float
    error: str = ""

    @property
    def light_delay_min(self) -> float:
        return self.dist_earth_km / C_KM_S / 60 if self.dist_earth_km else 0.0


# ── Artemis telemetry ──────────────────────────────────────────────────────────

@dataclass
class ArtemisTelemetry:
    mission: Mission
    timestamp_utc: datetime
    dist_earth_km: float
    dist_moon_km: float
    speed_km_s: float
    met_seconds: float
    error: str = ""

    @property
    def met_str(self) -> str:
        s = int(self.met_seconds)
        d, r = divmod(s, 86400)
        h, r = divmod(r, 3600)
        m, sec = divmod(r, 60)
        return f"{d}d {h:02d}h {m:02d}m {sec:02d}s"

    @property
    def mission_pct(self) -> float:
        m = self.mission
        if not m.splashdown_utc or self.met_seconds <= 0:
            return 0.0
        total = (m.splashdown_utc - m.launch_utc).total_seconds()
        return min(100.0, max(0.0, self.met_seconds / total * 100))

    @property
    def dist_earth_pct(self) -> float:
        if not self.dist_earth_km:
            return 0.0
        from_surface  = self.dist_earth_km - EARTH_RADIUS_KM
        earth_to_moon = MOON_DIST_KM - EARTH_RADIUS_KM
        return min(100.0, max(0.0, from_surface / earth_to_moon * 100))


@dataclass
class ISSTelemetry:
    timestamp_utc: datetime
    dist_earth_km: float
    altitude_km: float
    speed_km_s: float
    lat: float
    lon: float
    crew: list[str] = field(default_factory=list)
    error: str = ""


# ── Horizons helpers ───────────────────────────────────────────────────────────

async def _fetch_dist_speed(
    client: httpx.AsyncClient,
    naif_id: int,
    center: str,
    at: Optional[datetime] = None,
) -> tuple[float, float]:
    """Return (distance_km, speed_km_s) or (0, 0) on failure."""
    try:
        text = await fetch_vectors(client, naif_id, center=center, at=at)
        result = distance_speed(text, at=at)
        if result:
            return result
    except Exception:
        pass
    return 0.0, 0.0


# ── Data fetchers ──────────────────────────────────────────────────────────────

async def fetch_artemis_telemetry(mission: Mission) -> ArtemisTelemetry:
    cache_key = f"artemis_{mission.naif_id}"
    cached = _cached(cache_key)
    if cached is not None:
        # Only update MET — keep timestamp_utc as the real fetch time so display shows data age
        cached.met_seconds = max((datetime.now(tz=timezone.utc) - mission.launch_utc).total_seconds(), 0.0)
        return cached  # type: ignore[return-value]

    now = datetime.now(tz=timezone.utc)
    met = (now - mission.launch_utc).total_seconds()
    errors: list[str] = []

    async with httpx.AsyncClient() as client:
        (dist_earth, speed), (dist_moon, _) = await asyncio.gather(
            _fetch_dist_speed(client, mission.naif_id, "500@399", at=now),
            _fetch_dist_speed(client, mission.naif_id, "500@301", at=now),
        )

    if not dist_earth:
        errors.append("Earth telemetry unavailable")
    if not dist_moon:
        errors.append("Moon distance unavailable")

    result = ArtemisTelemetry(
        mission=mission,
        timestamp_utc=now,
        dist_earth_km=dist_earth,
        dist_moon_km=dist_moon,
        speed_km_s=speed,
        met_seconds=max(met, 0.0),
        error="; ".join(errors),
    )
    # Only cache successful fetches — don't cache failures so we retry next tick
    if dist_earth:
        _cache_set(cache_key, result, ttl=120)
    return result


async def fetch_iss_telemetry() -> ISSTelemetry:
    cached = _cached("iss")
    if cached is not None:
        return cached  # type: ignore[return-value]

    now = datetime.now(tz=timezone.utc)
    errors: list[str] = []
    lat = lon = 0.0
    crew: list[str] = []

    async with httpx.AsyncClient() as client:
        horizons_task = asyncio.create_task(
            _fetch_dist_speed(client, ISS_NAIF_ID, "500@399", at=now)
        )
        try:
            resp = await client.get(OPEN_NOTIFY_ISS, timeout=8)
            pos = resp.json().get("iss_position", {})
            lat = float(pos.get("latitude", 0))
            lon = float(pos.get("longitude", 0))
        except Exception as e:
            errors.append(f"lat/lon: {e}")

        try:
            resp = await client.get(OPEN_NOTIFY_CREW, timeout=8)
            crew = [
                p["name"]
                for p in resp.json().get("people", [])
                if p.get("craft") == "ISS"
            ]
        except Exception as e:
            errors.append(f"crew: {e}")

        dist_earth, speed = await horizons_task

    if not dist_earth:
        errors.append("Horizons telemetry unavailable")

    altitude = max(dist_earth - EARTH_RADIUS_KM, 0.0) if dist_earth else 0.0

    result = ISSTelemetry(
        timestamp_utc=now,
        dist_earth_km=dist_earth,
        altitude_km=altitude,
        speed_km_s=speed,
        lat=lat,
        lon=lon,
        crew=crew,
        error="; ".join(errors),
    )
    # Only cache if we got real positional data
    if dist_earth:
        _cache_set("iss", result, ttl=60)
    return result


async def fetch_all_probes() -> list[ProbeTelemetry]:
    cached = _cached("probes")
    if cached is not None:
        return cached  # type: ignore[return-value]
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[_fetch_dist_speed(client, p.naif_id, "500@399") for p in PROBES],
            return_exceptions=True,
        )
    out: list[ProbeTelemetry] = []
    for probe, res in zip(PROBES, results):
        if isinstance(res, Exception) or not isinstance(res, tuple):
            out.append(ProbeTelemetry(probe=probe, dist_earth_km=0.0, speed_km_s=0.0, error=str(res)))
        else:
            dist, speed = res
            out.append(ProbeTelemetry(probe=probe, dist_earth_km=dist, speed_km_s=speed))
    # Only cache if at least half the probes returned real data
    if sum(1 for t in out if t.dist_earth_km) >= len(out) // 2:
        _cache_set("probes", out, ttl=600)
    return out


async def _fetch_single_rover(
    client: httpx.AsyncClient,
    cfg: RoverConfig,
) -> RoverStatus:
    dist_earth, _ = await _fetch_dist_speed(client, cfg.naif_id, "500@399")
    return RoverStatus(
        name=cfg.name,
        status=cfg.status,
        mission=cfg.mission,
        landing_date=cfg.landing_date,
        dist_earth_km=dist_earth,
    )


async def fetch_all_rovers() -> list[RoverStatus]:
    cached = _cached("rovers")
    if cached is not None:
        return cached  # type: ignore[return-value]
    async with httpx.AsyncClient() as client:
        result = list(await asyncio.gather(
            *[_fetch_single_rover(client, cfg) for cfg in ROVERS]
        ))
    _cache_set("rovers", result, ttl=300)   # 5 minutes — daily updates at most
    return result


async def fetch_upcoming_launches(n: int = 5) -> list[dict]:
    cached = _cached("launches")
    if cached is not None:
        return cached  # type: ignore[return-value]
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(LAUNCH_API, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("result", [])
            launches = [r for r in results if r.get("win_open") or r.get("date_str")]
            result = launches[:n]
        except Exception as e:
            result = [{"error": str(e)}]
    # Don't cache error responses
    if result and "error" not in result[0]:
        _cache_set("launches", result, ttl=300)
    return result


# ── Formatters ─────────────────────────────────────────────────────────────────

def _km(v: float) -> str:
    return f"{v:,.0f} km"


def _dist(km: float) -> str:
    """Auto-scale: AU for ≥ 0.1 AU, km otherwise."""
    if not km:
        return "—"
    au = km / AU_KM
    return f"{au:.3f} AU" if au >= 0.1 else f"{km:,.0f} km"


def _delay(minutes: float) -> str:
    """Format one-way signal delay."""
    if not minutes:
        return "—"
    if minutes < 1:
        return f"{minutes * 60:.0f} s"
    if minutes < 60:
        return f"{minutes:.1f} min"
    h = int(minutes / 60)
    m = minutes % 60
    return f"{h}h {m:.0f}m"


def _lat_lon(lat: float, lon: float) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.2f}°{ns}  /  {abs(lon):.2f}°{ew}"


def _local_str(dt: datetime) -> str:
    """Return local-timezone representation of a UTC datetime."""
    return dt.astimezone().strftime("%H:%M %Z")


def _timestamp(dt: datetime) -> str:
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S UTC')}  ({_local_str(dt)})"


def _status_badge(status: str) -> Text:
    colors = {"active": "bold green", "completed": "dim", "upcoming": "bold cyan"}
    labels = {"active": "ACTIVE", "completed": "COMPLETED", "upcoming": "UPCOMING"}
    return Text(f" {labels.get(status, status.upper())} ", style=colors.get(status, "white"))


def _bar(pct: float, width: int = 26, color: str = "green") -> str:
    clamped = min(100.0, max(0.0, pct))
    filled  = int(width * clamped / 100)
    empty   = width - filled
    return f"[{color}]{'█' * filled}[/{color}][dim]{'░' * empty}[/dim]  {clamped:.1f}%"


def _trajectory(pct: float, width: int = 28) -> str:
    clamped = min(100.0, max(0.0, pct))
    pos     = max(0, min(width - 1, int(width * clamped / 100)))
    line    = list("─" * width)
    line[pos] = "▲"
    return f"[blue]🌎[/blue][dim]{''.join(line)}[/dim][grey66]🌑[/grey66]"


# ── Rich panel builders ────────────────────────────────────────────────────────

def _build_artemis_panel(t: ArtemisTelemetry) -> Panel:
    m     = t.mission
    badge = _status_badge(m.status)

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", min_width=22)
    grid.add_column()

    if m.crew:
        grid.add_row("Crew", "  ".join(m.crew))

    grid.add_row("Mission Elapsed Time", t.met_str if t.met_seconds > 0 else "—")
    grid.add_row("Mission Complete",     _bar(t.mission_pct, color="blue"))

    if t.dist_earth_km:
        grid.add_row("Distance from Earth", _km(t.dist_earth_km))
    if t.dist_moon_km:
        grid.add_row("Distance from Moon",  _km(t.dist_moon_km))
    if t.dist_earth_km:
        grid.add_row("Journey to Moon",     _trajectory(t.dist_earth_pct))
        grid.add_row("",                    _bar(t.dist_earth_pct, color="yellow"))
    if t.speed_km_s:
        grid.add_row("Speed",              f"{t.speed_km_s:.3f} km/s")

    grid.add_row("Data as of", _timestamp(t.timestamp_utc))

    if t.error:
        grid.add_row("[yellow]Warning[/yellow]", t.error)

    title = Text()
    title.append(m.name, style="bold white")
    title.append(f"  —  {m.nickname}  ", style="dim")
    title.append_text(badge)

    return Panel(grid, title=title, border_style="blue", padding=(1, 2))


def _build_iss_panel(t: ISSTelemetry) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", min_width=22)
    grid.add_column()

    if t.altitude_km:
        grid.add_row("Altitude",  _km(t.altitude_km))
    if t.speed_km_s:
        grid.add_row("Speed",    f"{t.speed_km_s:.3f} km/s")
    if t.lat or t.lon:
        grid.add_row("Position",  _lat_lon(t.lat, t.lon))
    if t.crew:
        grid.add_row(f"Crew ({len(t.crew)})", "  |  ".join(t.crew))

    grid.add_row("Data as of", _timestamp(t.timestamp_utc))

    if t.error:
        grid.add_row("[yellow]Warning[/yellow]", t.error)

    title = Text()
    title.append("International Space Station", style="bold white")
    title.append("  ")
    title.append_text(_status_badge("active"))

    return Panel(grid, title=title, border_style="cyan", padding=(1, 2))


def _build_probes_panel(probes: list[ProbeTelemetry]) -> Panel:
    tbl = Table(
        box=box.SIMPLE_HEAD, show_header=True, header_style="bold",
        show_edge=False, pad_edge=False,
    )
    tbl.add_column("Spacecraft",    min_width=20)
    tbl.add_column("Mission",       min_width=34)
    tbl.add_column("Launched",      min_width=8)
    tbl.add_column("Distance",      min_width=12)
    tbl.add_column("Speed (km/s)",  min_width=12)
    tbl.add_column("Signal Delay",  min_width=12)

    for t in probes:
        if t.dist_earth_km:
            dist_str  = _dist(t.dist_earth_km)
            speed_str = f"{t.speed_km_s:.2f}"
            delay_str = _delay(t.light_delay_min)
        else:
            dist_str = speed_str = delay_str = "[dim]—[/dim]"

        tbl.add_row(
            t.probe.name,
            f"[dim]{t.probe.mission}[/dim]",
            str(t.probe.launched),
            dist_str,
            speed_str,
            delay_str,
        )

    return Panel(tbl, title="[bold]Deep Space Probes[/bold]", border_style="magenta", padding=(0, 1))


def _build_rovers_panel(rovers: list[RoverStatus]) -> Panel:
    tbl = Table(
        box=box.SIMPLE_HEAD, show_header=True, header_style="bold",
        show_edge=False, pad_edge=False,
    )
    tbl.add_column("Rover",        min_width=18)
    tbl.add_column("Status",       min_width=14)
    tbl.add_column("Landed",       min_width=12)
    tbl.add_column("Signal Delay", min_width=12)
    tbl.add_column("Mission",      min_width=40, no_wrap=False)

    for r in rovers:
        status_style = "green" if r.status == "active" else "dim"
        delay = _delay(r.light_delay_min) if r.dist_earth_km else "—"
        tbl.add_row(
            r.name,
            f"[{status_style}]{r.status.upper()}[/{status_style}]",
            r.landing_date,
            delay,
            f"[dim]{r.mission}[/dim]",
        )

    return Panel(tbl, title="[bold]Mars Surface Missions[/bold]", border_style="red", padding=(0, 1))


def _build_launches_panel(launches: list[dict]) -> Panel:
    if not launches:
        inner = "[dim]No upcoming launches found.[/dim]"
    elif "error" in launches[0]:
        inner = f"[red]Error:[/red] {launches[0]['error']}"
    else:
        tbl = Table(
            box=box.SIMPLE_HEAD, show_header=True, header_style="bold",
            show_edge=False, pad_edge=False,
        )
        tbl.add_column("Launch",       min_width=32)
        tbl.add_column("Provider",     min_width=18)
        tbl.add_column("Vehicle",      min_width=14)
        tbl.add_column("Pad",          min_width=18)
        tbl.add_column("Window Open",  min_width=26)

        for r in launches:
            name     = r.get("name", "?")
            provider = r.get("provider", {}).get("name", "?")
            vehicle  = r.get("vehicle",  {}).get("name", "?")
            pad_loc  = r.get("pad", {}).get("location", {})
            pad      = f"{r.get('pad', {}).get('name', '?')}, {pad_loc.get('name', '?')}"
            win      = r.get("win_open") or r.get("date_str") or "TBD"
            if "T" in str(win):
                try:
                    dt  = datetime.fromisoformat(win.replace("Z", "+00:00"))
                    win = f"{dt.strftime('%b %d  %H:%M')} UTC  ({_local_str(dt)})"
                except ValueError:
                    pass
            tbl.add_row(name, provider, vehicle, pad, win)
        inner = tbl  # type: ignore[assignment]

    return Panel(inner, title="[bold]Upcoming Launches[/bold]", border_style="dim", padding=(0, 1))


# ── Textual TUI ────────────────────────────────────────────────────────────────

from textual.app import App, ComposeResult
from textual.containers import Horizontal, ScrollableContainer
from textual.widgets import Footer, Header, Static
from textual import work as textual_work


class _ArtemisWidget(Static):
    def on_mount(self) -> None:
        self.update("[dim]⏳ Fetching Artemis telemetry…[/dim]")

    def set_telemetry(self, t: ArtemisTelemetry) -> None:
        self.update(_build_artemis_panel(t))


class _ISSWidget(Static):
    def on_mount(self) -> None:
        self.update("[dim]⏳ Fetching ISS telemetry…[/dim]")

    def set_telemetry(self, t: ISSTelemetry) -> None:
        self.update(_build_iss_panel(t))


class _ProbesWidget(Static):
    def on_mount(self) -> None:
        self.update("[dim]⏳ Fetching probe fleet…[/dim]")

    def set_probes(self, probes: list[ProbeTelemetry]) -> None:
        self.update(_build_probes_panel(probes))


class _RoversWidget(Static):
    def on_mount(self) -> None:
        self.update("[dim]⏳ Fetching rover status…[/dim]")

    def set_rovers(self, rovers: list[RoverStatus]) -> None:
        self.update(_build_rovers_panel(rovers))


class _LaunchesWidget(Static):
    def on_mount(self) -> None:
        self.update("[dim]⏳ Fetching launch schedule…[/dim]")

    def set_launches(self, launches: list[dict]) -> None:
        self.update(_build_launches_panel(launches))


class NasaApp(App):
    """nasa tui — live NASA mission dashboard."""

    TITLE     = "NASA Mission Tracker"
    SUB_TITLE = "JPL Horizons · NASA API · open-notify · rocketlaunch.live"

    CSS = """
    Screen {
        background: #070714;
    }
    Header {
        background: #001a3d;
        color: #7ec8e3;
    }
    Footer {
        background: #001a3d;
    }
    #row1, #row2 {
        height: auto;
        width: 100%;
    }
    #row2 {
        margin-top: 1;
    }
    _ArtemisWidget {
        width: 1fr;
        height: auto;
    }
    _ISSWidget {
        width: 1fr;
        height: auto;
        margin-left: 1;
    }
    _RoversWidget {
        width: 1fr;
        height: auto;
    }
    _LaunchesWidget {
        width: 1fr;
        height: auto;
        margin-left: 1;
    }
    _ProbesWidget {
        width: 100%;
        height: auto;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("q",         "quit",           "Quit"),
        ("r",         "refresh",        "Refresh"),
        ("shift+r",   "force_refresh",  "Force refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with ScrollableContainer():
            with Horizontal(id="row1"):
                yield _ArtemisWidget(id="artemis")
                yield _ISSWidget(id="iss")
            with Horizontal(id="row2"):
                yield _RoversWidget(id="rovers")
                yield _LaunchesWidget(id="launches")
            yield _ProbesWidget(id="probes")
        yield Footer()

    def on_mount(self) -> None:
        self._active_mission = next(
            (m for m in ARTEMIS_MISSIONS if m.status == "active"),
            ARTEMIS_MISSIONS[-1],
        )
        self.refresh_data()
        self.set_interval(30, self.refresh_data)

    def action_refresh(self) -> None:
        self.refresh_data()

    def action_force_refresh(self) -> None:
        _cache_clear()
        self.refresh_data()

    @textual_work(exclusive=True)
    async def refresh_data(self) -> None:
        artemis_t, iss_t, probes, rovers, launches = await asyncio.gather(
            fetch_artemis_telemetry(self._active_mission),
            fetch_iss_telemetry(),
            fetch_all_probes(),
            fetch_all_rovers(),
            fetch_upcoming_launches(5),
        )
        self.query_one("#artemis",  _ArtemisWidget).set_telemetry(artemis_t)
        self.query_one("#iss",      _ISSWidget).set_telemetry(iss_t)
        self.query_one("#rovers",   _RoversWidget).set_rovers(rovers)
        self.query_one("#launches", _LaunchesWidget).set_launches(launches)
        self.query_one("#probes",   _ProbesWidget).set_probes(probes)


# ── CLI ────────────────────────────────────────────────────────────────────────

app = typer.Typer(
    name="nasa",
    help="[bold]nasa[/bold] — NASA / space mission tracking",
    no_args_is_help=False,
    invoke_without_command=True,
    rich_markup_mode="rich",
    add_completion=False,
)

artemis_app = typer.Typer(
    help="Artemis lunar programme — live telemetry and mission list",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
app.add_typer(artemis_app, name="artemis")


@app.callback()
def _default(ctx: typer.Context) -> None:
    """Launch the TUI when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        NasaApp().run()


@artemis_app.command("status")
def cmd_artemis_status(
    watch: bool  = typer.Option(False, "--watch", "-w", help="Refresh every 30 s"),
    force: bool  = typer.Option(False, "--force", "-f", help="Bypass cache and fetch fresh data"),
) -> None:
    """Live telemetry for the current Artemis mission (JPL Horizons)."""
    mission = next((m for m in ARTEMIS_MISSIONS if m.status == "active"), ARTEMIS_MISSIONS[-1])
    while True:
        if force:
            _cache_clear()
        t = asyncio.run(fetch_artemis_telemetry(mission))
        if watch:
            console.clear()
        console.print(_build_artemis_panel(t))
        if not watch:
            break
        try:
            time.sleep(30)
        except KeyboardInterrupt:
            break


@artemis_app.command("missions")
def cmd_artemis_missions() -> None:
    """List all Artemis missions."""
    tbl = Table(
        box=box.SIMPLE_HEAD, show_header=True, header_style="bold",
        show_edge=False, pad_edge=False,
    )
    tbl.add_column("Mission",      min_width=14)
    tbl.add_column("Nickname",     min_width=22)
    tbl.add_column("Crewed")
    tbl.add_column("Launch",       min_width=26)
    tbl.add_column("Splashdown",   min_width=26)
    tbl.add_column("Status",       min_width=10)
    tbl.add_column("Description",  max_width=50, no_wrap=False)

    for m in ARTEMIS_MISSIONS:
        badge  = _status_badge(m.status)
        crewed = "[green]Yes[/]" if m.crewed else "[dim]No[/]"
        launch = f"{m.launch_utc.strftime('%Y-%m-%d %H:%M')} UTC  ({_local_str(m.launch_utc)})"
        splash = (
            f"{m.splashdown_utc.strftime('%Y-%m-%d %H:%M')} UTC  ({_local_str(m.splashdown_utc)})"
            if m.splashdown_utc else "—"
        )
        tbl.add_row(m.name, m.nickname, crewed, launch, splash, badge, m.description)

    console.print(tbl)


@app.command("iss")
def cmd_iss(
    watch: bool = typer.Option(False, "--watch", "-w", help="Refresh every 30 s"),
    force: bool = typer.Option(False, "--force", "-f", help="Bypass cache and fetch fresh data"),
) -> None:
    """Current ISS position, altitude, speed, and crew."""
    while True:
        if force:
            _cache_clear()
        t = asyncio.run(fetch_iss_telemetry())
        if watch:
            console.clear()
        console.print(_build_iss_panel(t))
        if not watch:
            break
        try:
            time.sleep(30)
        except KeyboardInterrupt:
            break


@app.command("probes")
def cmd_probes(
    force: bool = typer.Option(False, "--force", "-f", help="Bypass cache and fetch fresh data"),
) -> None:
    """Deep space probe fleet — distances and signal delays (JPL Horizons)."""
    if force:
        _cache_clear()
    probes = asyncio.run(fetch_all_probes())
    console.print(_build_probes_panel(probes))


@app.command("rovers")
def cmd_rovers(
    force: bool = typer.Option(False, "--force", "-f", help="Bypass cache and fetch fresh data"),
) -> None:
    """Mars rover status — Curiosity & Perseverance (NASA Open API + JPL Horizons)."""
    if force:
        _cache_clear()
    rovers = asyncio.run(fetch_all_rovers())
    console.print(_build_rovers_panel(rovers))


@app.command("launches")
def cmd_launches(
    n:     int  = typer.Option(5,     "--n",     "-n", help="Number of upcoming launches to show"),
    force: bool = typer.Option(False, "--force", "-f", help="Bypass cache and fetch fresh data"),
) -> None:
    """Upcoming rocket launches (rocketlaunch.live)."""
    if force:
        _cache_clear()
    launches = asyncio.run(fetch_upcoming_launches(n))
    console.print(_build_launches_panel(launches))


@app.command("tui")
def cmd_tui() -> None:
    """Interactive live dashboard — all missions, ISS, rovers, probes, and launches."""
    NasaApp().run()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
