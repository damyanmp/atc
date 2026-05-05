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


@dataclass
class PlexItem:
    rating_key: str
    title: str
    year: int = 0
    lib_type: str = ""       # "movie", "show", "season", "episode", "artist", "album", "track"
    library: str = ""
    duration_ms: int = 0
    rating: float = 0.0
    audience_rating: float = 0.0
    added_at: str = ""
    summary: str = ""
    genres: str = ""         # comma-separated
    directors: str = ""      # comma-separated
    studio: str = ""
    # TV / episode extras
    season: int = 0          # season number (parentIndex)
    episode: int = 0         # episode number (index)
    parent_title: str = ""   # show title for episodes, album for tracks
    child_count: int = 0     # seasons for shows, albums for artists
    leaf_count: int = 0      # total episodes for shows/seasons, tracks for albums


def _headers(cfg: PlexConfig) -> dict:
    return {
        "X-Plex-Token": cfg.token,
        "Accept": "application/json",
    }


def _client(cfg: PlexConfig) -> httpx.Client:
    return httpx.Client(verify=cfg.verify_ssl, timeout=10)


_YEAR_SUFFIX = re.compile(r"\s*\(\d{4}\)$")


def find_media(
    cfg: PlexConfig,
    title: str,
    media_type: str,
    year: int = 0,
    tvdb_id: int = 0,
    tmdb_id: int = 0,
) -> PlexMatch:
    """Search Plex for a movie or TV show.

    media_type: "movie" or "tv"
    tvdb_id / tmdb_id: when provided, matched against the Guid array Plex attaches
        to each item (e.g. "tvdb://12345", "tmdb://67890"). ID match takes priority
        over title matching and is immune to regional title differences.
    year: used only for title-based fallback disambiguation.

    Plex sometimes stores titles with a year suffix (e.g. "Lost in Space (2018)").
    The title match strips any trailing "(YYYY)" before comparing.
    """
    plex_hub_type = "movie" if media_type == "movie" else "show"
    tvdb_prefix = f"tvdb://{tvdb_id}" if tvdb_id else ""
    tmdb_prefix = f"tmdb://{tmdb_id}" if tmdb_id else ""

    def _build_match(item) -> PlexMatch:
        added_at = ""
        ts = item.get("addedAt")
        if ts:
            try:
                added_at = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
            except Exception:
                pass
        return PlexMatch(
            found=True,
            title=item.get("title", ""),
            year=item.get("year", 0),
            library=item.get("librarySectionTitle", ""),
            added_at=added_at,
            episode_count=item.get("leafCount", 0),
            season_count=item.get("childCount", 0),
        )

    try:
        with _client(cfg) as client:
            # Pass 1: GUID lookup via library sections (reliable, immune to title differences).
            # /library/sections/{key}/all?guid=tvdb://12345&type=2 returns items matching that
            # external ID. type: 1=movie, 2=show.
            if tvdb_prefix or tmdb_prefix:
                guid_query = tvdb_prefix or tmdb_prefix
                lib_type_num = 1 if media_type == "movie" else 2
                sections_r = client.get(
                    f"{cfg.base_url}/library/sections",
                    headers=_headers(cfg),
                )
                if sections_r.is_success:
                    sections = (sections_r.json().get("MediaContainer") or {}).get("Directory") or []
                    for section in sections:
                        if section.get("type") != plex_hub_type:
                            continue
                        key = section.get("key")
                        gr = client.get(
                            f"{cfg.base_url}/library/sections/{key}/all",
                            headers=_headers(cfg),
                            params={"guid": guid_query, "type": lib_type_num},
                        )
                        if not gr.is_success:
                            continue
                        items = (gr.json().get("MediaContainer") or {}).get("Metadata") or []
                        if items:
                            item = items[0]
                            # librarySectionTitle not present in this response — use section title
                            item.setdefault("librarySectionTitle", section.get("title", ""))
                            return _build_match(item)

            # Pass 2: title search via /hubs/search (fallback when no IDs, or not found by GUID)
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
                normalised = _YEAR_SUFFIX.sub("", item_title).strip().lower()
                title_matches = normalised == title_lower or item_title.lower() == title_lower
                year_matches = (not year) or (item_year == year)
                if title_matches and year_matches:
                    return _build_match(item)

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

def _section_count(client: httpx.Client, cfg: PlexConfig, key: int) -> int:
    """Fetch the real item count for a library section via a zero-size container request."""
    try:
        r = client.get(
            f"{cfg.base_url}/library/sections/{key}/all",
            headers=_headers(cfg),
            params={"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0},
        )
        if r.is_success:
            mc = r.json().get("MediaContainer") or {}
            return int(mc.get("totalSize", mc.get("size", 0)))
    except Exception:
        pass
    return 0


def list_libraries(cfg: PlexConfig) -> list[PlexLibrary] | str:
    """Return all library sections with accurate item counts. Returns error string on failure."""
    try:
        with _client(cfg) as client:
            r = client.get(f"{cfg.base_url}/library/sections", headers=_headers(cfg))
            if not r.is_success:
                return f"HTTP {r.status_code}: {r.text[:200]}"
            dirs = (r.json().get("MediaContainer") or {}).get("Directory") or []
            libs = []
            for d in dirs:
                key = int(d.get("key", 0))
                # Plex sometimes returns count in the sections listing; fall back to a
                # lightweight size-0 request to get totalSize from the actual section.
                count = int(d.get("count", 0) or d.get("size", 0))
                if count == 0 and key:
                    count = _section_count(client, cfg, key)
                libs.append(PlexLibrary(
                    key=key,
                    title=d.get("title", ""),
                    lib_type=d.get("type", ""),
                    count=count,
                    agent=d.get("agent", ""),
                ))
            return libs
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


def _build_item(item: dict, library_title: str = "") -> PlexItem:
    added = ""
    ts = item.get("addedAt")
    if ts:
        try:
            added = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
        except Exception:
            pass
    genres = ", ".join(g.get("tag", "") for g in (item.get("Genre") or []))
    directors = ", ".join(d.get("tag", "") for d in (item.get("Director") or []))
    return PlexItem(
        rating_key=str(item.get("ratingKey", "")),
        title=item.get("title", ""),
        year=item.get("year", 0),
        lib_type=item.get("type", ""),
        library=item.get("librarySectionTitle", library_title),
        duration_ms=item.get("duration", 0),
        rating=float(item.get("rating", 0) or 0),
        audience_rating=float(item.get("audienceRating", 0) or 0),
        added_at=added,
        summary=item.get("summary", ""),
        genres=genres,
        directors=directors,
        studio=item.get("studio", ""),
        season=item.get("parentIndex", 0),
        episode=item.get("index", 0),
        parent_title=item.get("grandparentTitle", "") or item.get("parentTitle", ""),
        child_count=int(item.get("childCount", 0)),
        leaf_count=int(item.get("leafCount", 0)),
    )


def search_plex(cfg: PlexConfig, query: str, limit: int = 30) -> list[PlexItem] | str:
    """Full-text search across all libraries via /hubs/search."""
    try:
        with _client(cfg) as client:
            r = client.get(
                f"{cfg.base_url}/hubs/search",
                headers=_headers(cfg),
                params={"query": query, "limit": limit},
            )
            if not r.is_success:
                return f"HTTP {r.status_code}: {r.text[:200]}"
            hubs = (r.json().get("MediaContainer") or {}).get("Hub") or []
            results: list[PlexItem] = []
            for hub in hubs:
                for item in hub.get("Metadata") or []:
                    results.append(_build_item(item))
            return results
    except Exception as e:
        return str(e)


def get_children(cfg: PlexConfig, rating_key: str) -> list[PlexItem] | str:
    """Get direct children of a show (seasons) or season (episodes) or artist (albums)."""
    try:
        with _client(cfg) as client:
            r = client.get(
                f"{cfg.base_url}/library/metadata/{rating_key}/children",
                headers=_headers(cfg),
            )
            if not r.is_success:
                return f"HTTP {r.status_code}: {r.text[:200]}"
            mc = r.json().get("MediaContainer") or {}
            items = mc.get("Metadata") or []
            return [_build_item(item) for item in items]
    except Exception as e:
        return str(e)


def browse_library(cfg: PlexConfig, section_key: int, limit: int = 300) -> list[PlexItem] | str:
    """List all items in a library section, sorted by title."""
    try:
        with _client(cfg) as client:
            r = client.get(
                f"{cfg.base_url}/library/sections/{section_key}/all",
                headers=_headers(cfg),
                params={
                    "X-Plex-Container-Start": 0,
                    "X-Plex-Container-Size": limit,
                    "sort": "titleSort",
                },
            )
            if not r.is_success:
                return f"HTTP {r.status_code}: {r.text[:200]}"
            mc = r.json().get("MediaContainer") or {}
            lib_title = mc.get("librarySectionTitle", "") or mc.get("title2", "")
            items = mc.get("Metadata") or []
            return [_build_item(item, lib_title) for item in items]
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
