"""Plex Media Server API client — media availability check and library management."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from config import PlexConfig


@dataclass
class PlexMatch:
    found: bool
    title: str = ""
    year: int = 0
    library: str = ""
    added_at: str = ""   # YYYY-MM-DD
    episode_count: int = 0   # TV only: total episodes in Plex (leafCount)
    season_count: int = 0    # TV only: number of seasons (childCount)
    error: str = ""


@dataclass
class PlexLibrary:
    key: int          # section ID used in API calls
    title: str
    lib_type: str     # "movie", "show", "music", etc.
    count: int = 0    # number of items in the library
    agent: str = ""


@dataclass
class PlexServerInfo:
    version: str = ""
    machine_id: str = ""
    friendly_name: str = ""
    error: str = ""


def _headers(cfg: PlexConfig) -> dict:
    return {
        "X-Plex-Token": cfg.token,
        "Accept": "application/json",
    }


def _client(cfg: PlexConfig) -> httpx.Client:
    return httpx.Client(verify=cfg.verify_ssl, timeout=10)


_YEAR_SUFFIX = re.compile(r"\s*\(\d{4}\)$")


def find_media(cfg: PlexConfig, title: str, media_type: str, year: int = 0) -> PlexMatch:
    """Search Plex for a movie or TV show by title.

    media_type: "movie" or "tv"
    year: optional — used to disambiguate when multiple titles match (e.g. remakes).
    Uses the /hubs/search endpoint (searches all libraries).

    Plex sometimes stores titles with a year suffix (e.g. "Lost in Space (2018)").
    The match strips any trailing "(YYYY)" from the Plex title before comparing so
    that "Lost in Space (2018)" matches a search for "Lost in Space".
    """
    plex_hub_type = "movie" if media_type == "movie" else "show"
    try:
        with _client(cfg) as client:
            r = client.get(
                f"{cfg.base_url}/hubs/search",
                headers=_headers(cfg),
                params={"query": title, "limit": 20},
            )
            if not r.is_success:
                return PlexMatch(found=False, error=f"HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()

        hubs = (data.get("MediaContainer") or {}).get("Hub") or []
        title_lower = title.lower()

        for hub in hubs:
            if hub.get("type") != plex_hub_type:
                continue
            for item in hub.get("Metadata") or []:
                item_title = item.get("title", "")
                item_year = item.get("year", 0)
                # Normalise: strip trailing "(YYYY)" Plex sometimes appends
                normalised = _YEAR_SUFFIX.sub("", item_title).strip().lower()
                title_matches = normalised == title_lower or item_title.lower() == title_lower
                year_matches = (not year) or (item_year == year)
                if title_matches and year_matches:
                    added_at = ""
                    ts = item.get("addedAt")
                    if ts:
                        try:
                            added_at = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                        except Exception:
                            pass
                    return PlexMatch(
                        found=True,
                        title=item_title,
                        year=item.get("year", 0),
                        library=item.get("librarySectionTitle", ""),
                        added_at=added_at,
                        episode_count=item.get("leafCount", 0),
                        season_count=item.get("childCount", 0),
                    )

        return PlexMatch(found=False)
    except Exception as e:
        return PlexMatch(found=False, error=str(e))


# ── Server info ───────────────────────────────────────────────────────────────

def get_server_info(cfg: PlexConfig) -> PlexServerInfo:
    """Fetch Plex server identity and version."""
    try:
        with _client(cfg) as client:
            r = client.get(f"{cfg.base_url}/identity", headers=_headers(cfg))
            if not r.is_success:
                return PlexServerInfo(error=f"HTTP {r.status_code}: {r.text[:200]}")
            mc = (r.json().get("MediaContainer") or {})
            return PlexServerInfo(
                version=mc.get("version", ""),
                machine_id=mc.get("machineIdentifier", ""),
                friendly_name=mc.get("friendlyName", ""),
            )
    except Exception as e:
        return PlexServerInfo(error=str(e))


# ── Libraries ─────────────────────────────────────────────────────────────────

def list_libraries(cfg: PlexConfig) -> list[PlexLibrary] | str:
    """Return all library sections. Returns error string on failure."""
    try:
        with _client(cfg) as client:
            r = client.get(f"{cfg.base_url}/library/sections", headers=_headers(cfg))
            if not r.is_success:
                return f"HTTP {r.status_code}: {r.text[:200]}"
            dirs = (r.json().get("MediaContainer") or {}).get("Directory") or []
            return [
                PlexLibrary(
                    key=int(d.get("key", 0)),
                    title=d.get("title", ""),
                    lib_type=d.get("type", ""),
                    count=int(d.get("count", 0)),
                    agent=d.get("agent", ""),
                )
                for d in dirs
            ]
    except Exception as e:
        return str(e)


def _section_key(cfg: PlexConfig, name_or_id: str) -> int | str:
    """Resolve a library name or numeric ID to a section key.

    Returns the int key on success, or an error string.
    """
    if name_or_id.isdigit():
        return int(name_or_id)
    libs = list_libraries(cfg)
    if isinstance(libs, str):
        return libs
    for lib in libs:
        if lib.title.lower() == name_or_id.lower():
            return lib.key
    titles = ", ".join(f'"{l.title}"' for l in libs)
    return f"Library {name_or_id!r} not found. Available: {titles}"


# ── Library actions ───────────────────────────────────────────────────────────

def scan_library(cfg: PlexConfig, section_id: int) -> str | None:
    """Trigger a filesystem scan for a library section.

    Returns None on success, error string on failure.
    Plex uses GET /library/sections/{id}/refresh to queue a scan.
    """
    try:
        with _client(cfg) as client:
            r = client.get(
                f"{cfg.base_url}/library/sections/{section_id}/refresh",
                headers=_headers(cfg),
            )
            if not r.is_success:
                return f"HTTP {r.status_code}: {r.text[:200]}"
            return None
    except Exception as e:
        return str(e)


def analyze_library(cfg: PlexConfig, section_id: int) -> str | None:
    """Trigger media analysis (thumbnail generation, chapter images, etc.).

    Returns None on success, error string on failure.
    """
    try:
        with _client(cfg) as client:
            r = client.put(
                f"{cfg.base_url}/library/sections/{section_id}/analyze",
                headers=_headers(cfg),
            )
            if not r.is_success:
                return f"HTTP {r.status_code}: {r.text[:200]}"
            return None
    except Exception as e:
        return str(e)


def empty_trash(cfg: PlexConfig, section_id: int) -> str | None:
    """Empty the trash for a library section.

    Returns None on success, error string on failure.
    """
    try:
        with _client(cfg) as client:
            r = client.put(
                f"{cfg.base_url}/library/sections/{section_id}/emptyTrash",
                headers=_headers(cfg),
            )
            if not r.is_success:
                return f"HTTP {r.status_code}: {r.text[:200]}"
            return None
    except Exception as e:
        return str(e)


def clean_bundles(cfg: PlexConfig) -> str | None:
    """Clean up orphaned metadata bundles (server-wide).

    Returns None on success, error string on failure.
    """
    try:
        with _client(cfg) as client:
            r = client.put(
                f"{cfg.base_url}/library/clean/bundles",
                headers=_headers(cfg),
            )
            if not r.is_success:
                return f"HTTP {r.status_code}: {r.text[:200]}"
            return None
    except Exception as e:
        return str(e)
