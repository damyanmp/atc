"""Shared Servarr API client for Radarr and Sonarr.

Both services expose identical API shapes under /api/v3/.
Authentication uses X-Api-Key header.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from config import ArrConfig


@dataclass
class ArrStatus:
    online: bool
    version: str = ""
    app_name: str = ""   # "Radarr" or "Sonarr"
    error: str = ""


@dataclass
class ArrCommand:
    command_id: int
    name: str
    status: str   # "queued", "started", "completed", "failed"
    message: str = ""


def _headers(cfg: ArrConfig) -> dict:
    return {
        "X-Api-Key": cfg.api_key,
        "Content-Type": "application/json",
    }


def _client(cfg: ArrConfig) -> httpx.Client:
    return httpx.Client(verify=cfg.verify_ssl, timeout=15)


def get_status(cfg: ArrConfig) -> ArrStatus:
    try:
        with _client(cfg) as client:
            r = client.get(f"{cfg.base_url}/api/v3/system/status", headers=_headers(cfg))
            if not r.is_success:
                return ArrStatus(online=False, error=f"HTTP {r.status_code}: {r.text}")
            d = r.json()
            return ArrStatus(
                online=True,
                version=d.get("version", ""),
                app_name=d.get("appName", cfg.name.capitalize()),
            )
    except Exception as e:
        return ArrStatus(online=False, error=str(e))


def _run_command(cfg: ArrConfig, command: str, **extra) -> ArrCommand:
    payload = {"name": command, **extra}
    with _client(cfg) as client:
        r = client.post(f"{cfg.base_url}/api/v3/command", headers=_headers(cfg), json=payload)
        if not r.is_success:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
        d = r.json()
    return ArrCommand(
        command_id=d["id"],
        name=d.get("name", command),
        status=d.get("status", "queued"),
        message=d.get("message", ""),
    )


# ── Radarr commands ───────────────────────────────────────────────────────────

def radarr_scan(cfg: ArrConfig) -> ArrCommand:
    """Trigger a full library rescan (RefreshMovie — all movies)."""
    return _run_command(cfg, "RefreshMovie")


# ── Sonarr commands ───────────────────────────────────────────────────────────

def sonarr_scan(cfg: ArrConfig) -> ArrCommand:
    """Trigger a full library rescan (RefreshSeries — all series)."""
    return _run_command(cfg, "RefreshSeries")


# Broad MissingMoviesSearch / MissingEpisodeSearch are deliberately NOT exposed:
# one call fans out to every missing item x every indexer and was the source of
# indexer throttling. Use get_missing() + the per-ID searches below in capped
# batches instead.

def sonarr_search_season(cfg: ArrConfig, series_id: int, season: int) -> ArrCommand:
    """Trigger a search for one season of a series."""
    return _run_command(cfg, "SeasonSearch", seriesId=series_id, seasonNumber=season)


def sonarr_search_series(cfg: ArrConfig, series_id: int) -> ArrCommand:
    """Trigger a search for all missing episodes in a specific series."""
    return _run_command(cfg, "SeriesSearch", seriesIds=[series_id])


def radarr_search_movies(cfg: ArrConfig, movie_ids: list[int]) -> ArrCommand:
    """Trigger a search for specific Radarr movie IDs."""
    return _run_command(cfg, "MoviesSearch", movieIds=movie_ids)


def sonarr_search_episodes(cfg: ArrConfig, episode_ids: list[int]) -> ArrCommand:
    """Trigger a search for specific Sonarr episode IDs."""
    return _run_command(cfg, "EpisodeSearch", episodeIds=episode_ids)


def get_episode_id(cfg: ArrConfig, series_id: int, season: int, episode: int) -> int | None:
    """Look up a Sonarr episode's internal ID by series/season/episode number."""
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/v3/episode",
            headers=_headers(cfg),
            params={"seriesId": series_id},
        )
        if not r.is_success:
            return None
        episodes = r.json()
    for ep in episodes:
        if ep.get("seasonNumber") == season and ep.get("episodeNumber") == episode:
            return ep.get("id")
    return None


# ── Pipeline lookups ──────────────────────────────────────────────────────────

@dataclass
class ArrMovieRecord:
    arr_id: int
    title: str
    year: int
    tmdb_id: int
    monitored: bool
    has_file: bool
    quality: str = ""
    size_bytes: int = 0
    date_added: str = ""   # date file was imported


@dataclass
class ArrSeriesRecord:
    arr_id: int
    title: str
    year: int
    monitored: bool
    episode_count: int = 0
    episode_file_count: int = 0
    percent: float = 0.0


@dataclass
class ArrQueueItem:
    queue_id: int
    title: str
    movie_id: int = 0
    series_id: int = 0
    status: str = ""          # "queued", "downloading", "paused", "completed", "warning", "error"
    tracked_state: str = ""   # "downloading", "importPending", "importing", "imported", "failed"
    size_bytes: int = 0
    size_left: int = 0
    time_left: str = ""
    error_message: str = ""
    download_id: str = ""     # download-client hash — one torrent can be N queue rows (one per episode)
    episode_id: int = 0
    tracked_status: str = ""  # "ok", "warning", "error"
    indexer: str = ""
    added: str = ""           # ISO datetime the grab was added
    series_title: str = ""    # Sonarr only (includeSeries)
    season: int = 0
    episode: int = 0
    status_messages: list[str] = field(default_factory=list)   # every title + message, flattened

    @property
    def progress_pct(self) -> float:
        if self.size_bytes == 0:
            return 0.0
        return (self.size_bytes - self.size_left) / self.size_bytes * 100

    @property
    def stuck(self) -> bool:
        """Needs a human: failed/blocked import, client error, or stalled with an error."""
        state = (self.tracked_state or "").lower()
        if state in ("importblocked", "failedpending", "failed"):
            return True
        if self.status in ("failed", "warning") or self.tracked_status in ("warning", "error"):
            return True
        return state == "importpending" and bool(self.error_message)

    @property
    def hint(self) -> str:
        """Suggested fix for a stuck item, from the *arr's own status text."""
        text = " ".join([self.error_message, *self.status_messages]).lower()
        if "already imported" in text:
            return "files already imported — safe to remove from queue"
        if "not a quality revision upgrade" in text or "not an upgrade" in text:
            return "existing file is same/better — remove from queue (or manual-import to replace)"
        if "stalled" in text:
            return "stalled — remove + blocklist so the *arr grabs another release"
        if "no files found" in text or "sample" in text:
            return "nothing importable in the download — remove + blocklist"
        return ""


@dataclass
class QueueGroup:
    """Queue rows collapsed per download (a season pack is one torrent, N rows)."""
    download_id: str
    title: str
    items: list[ArrQueueItem]

    @property
    def first(self) -> ArrQueueItem:
        return self.items[0]

    @property
    def stuck(self) -> bool:
        return any(i.stuck for i in self.items)

    @property
    def hint(self) -> str:
        return next((i.hint for i in self.items if i.hint), "")


def group_queue(items: list[ArrQueueItem]) -> list[QueueGroup]:
    """Collapse queue rows sharing a download_id, preserving first-seen order."""
    groups: dict[str, QueueGroup] = {}
    for it in items:
        key = it.download_id or f"q{it.queue_id}"
        if key not in groups:
            groups[key] = QueueGroup(download_id=it.download_id, title=it.title, items=[])
        groups[key].items.append(it)
    return list(groups.values())


def _queue_error(rec: dict) -> str:
    """Best human-readable reason from a queue record.

    errorMessage is often empty; statusMessages is a list of {title, messages}
    where the useful text can be in either field (e.g. importBlocked season
    packs put the reason in title and per-file detail in messages).
    """
    if rec.get("errorMessage"):
        return rec["errorMessage"]
    for sm in rec.get("statusMessages") or []:
        msgs = sm.get("messages") or []
        if msgs:
            return msgs[0]
        if sm.get("title"):
            return sm["title"]
    return ""


def get_movie_by_id(cfg: ArrConfig, arr_id: int) -> ArrMovieRecord | None:
    """Look up a Radarr movie record by Radarr's internal movie ID."""
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/v3/movie/{arr_id}", headers=_headers(cfg))
        if not r.is_success:
            return None
        m = r.json()
    quality_str = size_bytes = 0
    date_added = quality_str = ""
    if m.get("movieFile"):
        mf = m["movieFile"]
        quality_str = ((mf.get("quality") or {}).get("quality") or {}).get("name", "")
        size_bytes = mf.get("size", 0)
        date_added = (mf.get("dateAdded") or "")[:10]
    return ArrMovieRecord(
        arr_id=m["id"],
        title=m.get("title", ""),
        year=m.get("year", 0),
        tmdb_id=m.get("tmdbId", 0),
        monitored=m.get("monitored", False),
        has_file=m.get("hasFile", False),
        quality=quality_str,
        size_bytes=size_bytes,
        date_added=date_added,
    )


def get_movie_by_tmdb(cfg: ArrConfig, tmdb_id: int) -> ArrMovieRecord | None:
    """Look up a Radarr movie record by TMDB ID."""
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/v3/movie",
            headers=_headers(cfg),
            params={"tmdbId": tmdb_id},
        )
        if not r.is_success:
            return None
        movies = r.json()
    if not movies:
        return None
    m = movies[0]
    size_bytes = 0
    date_added = quality_str = ""
    if m.get("movieFile"):
        mf = m["movieFile"]
        quality_str = ((mf.get("quality") or {}).get("quality") or {}).get("name", "")
        size_bytes = mf.get("size", 0)
        date_added = (mf.get("dateAdded") or "")[:10]
    return ArrMovieRecord(
        arr_id=m["id"],
        title=m.get("title", ""),
        year=m.get("year", 0),
        tmdb_id=m.get("tmdbId", tmdb_id),
        monitored=m.get("monitored", False),
        has_file=m.get("hasFile", False),
        quality=quality_str,
        size_bytes=size_bytes,
        date_added=date_added,
    )


def get_series_by_id(cfg: ArrConfig, series_id: int) -> ArrSeriesRecord | None:
    """Look up a Sonarr series record by Sonarr's internal series ID."""
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/v3/series/{series_id}", headers=_headers(cfg))
        if not r.is_success:
            return None
        s = r.json()
    stats = s.get("statistics") or {}
    return ArrSeriesRecord(
        arr_id=s["id"],
        title=s.get("title", ""),
        year=s.get("year", 0),
        monitored=s.get("monitored", False),
        episode_count=stats.get("episodeCount", 0),
        episode_file_count=stats.get("episodeFileCount", 0),
        percent=stats.get("percentOfEpisodes", 0.0),
    )


def get_series_by_title(cfg: ArrConfig, title: str) -> ArrSeriesRecord | None:
    """Find a Sonarr series by title (case-insensitive substring match)."""
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/v3/series", headers=_headers(cfg))
        if not r.is_success:
            return None
        series_list = r.json()
    title_lower = title.lower()
    for s in series_list:
        if s.get("title", "").lower() == title_lower:
            stats = s.get("statistics") or {}
            return ArrSeriesRecord(
                arr_id=s["id"],
                title=s.get("title", ""),
                year=s.get("year", 0),
                monitored=s.get("monitored", False),
                episode_count=stats.get("episodeCount", 0),
                episode_file_count=stats.get("episodeFileCount", 0),
                percent=stats.get("percentOfEpisodes", 0.0),
            )
    return None


def get_queue(cfg: ArrConfig) -> list[ArrQueueItem]:
    """Fetch the full download queue (all pages)."""
    records: list[dict] = []
    page = 1
    with _client(cfg) as client:
        while True:
            r = client.get(
                f"{cfg.base_url}/api/v3/queue",
                headers=_headers(cfg),
                params={"page": page, "pageSize": 250, "includeUnknownMovieItems": False,
                        "includeSeries": True, "includeEpisode": True},
            )
            if not r.is_success:
                break
            data = r.json()
            if not isinstance(data, dict):     # very old API returned a bare list
                records.extend(data)
                break
            batch = data.get("records", [])
            records.extend(batch)
            if not batch or len(records) >= data.get("totalRecords", 0):
                break
            page += 1
    items = []
    for rec in records:
        err = _queue_error(rec)
        ep = rec.get("episode") or {}
        items.append(ArrQueueItem(
            queue_id=rec.get("id", 0),
            title=rec.get("title", ""),
            movie_id=rec.get("movieId", 0),
            series_id=rec.get("seriesId", 0),
            status=rec.get("status", ""),
            tracked_state=rec.get("trackedDownloadState", ""),
            size_bytes=int(rec.get("size", 0) or 0),
            size_left=int(rec.get("sizeleft", 0) or 0),
            time_left=rec.get("timeleft", "") or "",
            error_message=err or "",
            download_id=rec.get("downloadId", "") or "",
            episode_id=rec.get("episodeId", 0) or 0,
            tracked_status=rec.get("trackedDownloadStatus", "") or "",
            indexer=rec.get("indexer", "") or "",
            added=rec.get("added", "") or "",
            series_title=(rec.get("series") or {}).get("title", ""),
            season=rec.get("seasonNumber", ep.get("seasonNumber", 0)) or 0,
            episode=ep.get("episodeNumber", 0) or 0,
            status_messages=[
                t for sm in (rec.get("statusMessages") or [])
                for t in [sm.get("title", ""), *(sm.get("messages") or [])] if t
            ],
        ))
    return items


# ── Health / indexers ─────────────────────────────────────────────────────────

@dataclass
class HealthItem:
    type: str        # "ok", "notice", "warning", "error"
    source: str      # e.g. "IndexerStatusCheck"
    message: str


def get_health(cfg: ArrConfig) -> list[HealthItem]:
    """The *arr's own health checks (System → Status → Health in the UI)."""
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/v3/health", headers=_headers(cfg))
        r.raise_for_status()
        return [HealthItem(h.get("type", ""), h.get("source", ""), h.get("message", "")) for h in r.json()]


def parse_failing_indexers(health: list[HealthItem]) -> set[str]:
    """Indexer names the *arr reports as unavailable/backed-off.

    The per-indexer status endpoint isn't available on every version (404 on
    current Radarr/Sonarr), but the health check message lists them:
    "Indexers unavailable due to failures: a, b, c".
    """
    names: set[str] = set()
    for h in health:
        if h.source not in ("IndexerStatusCheck", "IndexerLongTermStatusCheck"):
            continue
        _, _, tail = h.message.partition(":")
        names.update(n.strip() for n in tail.split(",") if n.strip())
    return names


@dataclass
class IndexerHealth:
    auto_search: list[str]      # indexers with automatic search enabled
    failing: set[str]           # subset currently failing / backed off

    @property
    def healthy(self) -> int:
        return len([n for n in self.auto_search if n not in self.failing])

    @property
    def total(self) -> int:
        return len(self.auto_search)

    @property
    def ratio(self) -> float:
        return self.healthy / self.total if self.total else 0.0


def get_indexer_health(cfg: ArrConfig, health: list[HealthItem] | None = None) -> IndexerHealth:
    """Auto-search indexers and which of them are failing. Pass `health` to reuse a fetch."""
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/v3/indexer", headers=_headers(cfg))
        r.raise_for_status()
        auto = [i.get("name", "") for i in r.json() if i.get("enableAutomaticSearch")]
    if health is None:
        health = get_health(cfg)
    return IndexerHealth(auto_search=auto, failing=parse_failing_indexers(health))


# ── Wanted / missing ──────────────────────────────────────────────────────────

@dataclass
class MissingItem:
    arr_id: int          # Radarr movie id, or Sonarr EPISODE id
    title: str           # movie title, or series title
    year: int = 0
    series_id: int = 0   # Sonarr only
    season: int = 0
    episode: int = 0
    episode_title: str = ""
    date: str = ""       # release/air date YYYY-MM-DD (sort key)


def get_missing(cfg: ArrConfig, page_size: int = 100) -> list[MissingItem]:
    """Monitored items with no file, newest release/air date first.

    Newest-first because recent releases are the ones indexers actually have;
    a years-old backlog item that never resolved is unlikely to start now.
    Only fetches one page — callers search in capped batches anyway.
    """
    is_sonarr = cfg.name == "sonarr"
    params = {"page": 1, "pageSize": page_size, "monitored": "true",
              "sortDirection": "descending"}
    if is_sonarr:
        params.update(sortKey="episodes.airDateUtc", includeSeries="true")
    else:
        params.update(sortKey="movies.digitalRelease")
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/v3/wanted/missing", headers=_headers(cfg), params=params)
        r.raise_for_status()
        records = r.json().get("records", [])
    out: list[MissingItem] = []
    for rec in records:
        if is_sonarr:
            out.append(MissingItem(
                arr_id=rec.get("id", 0),
                title=(rec.get("series") or {}).get("title", ""),
                series_id=rec.get("seriesId", 0),
                season=rec.get("seasonNumber", 0),
                episode=rec.get("episodeNumber", 0),
                episode_title=rec.get("title", ""),
                date=(rec.get("airDateUtc") or rec.get("airDate") or "")[:10],
            ))
        else:
            # Radarr's missing list includes announced/in-cinemas titles no
            # indexer can have yet — searching those is pure indexer load.
            if rec.get("isAvailable") is False:
                continue
            date_ = rec.get("digitalRelease") or rec.get("physicalRelease") or rec.get("inCinemas") or ""
            out.append(MissingItem(
                arr_id=rec.get("id", 0),
                title=rec.get("title", ""),
                year=rec.get("year", 0),
                date=date_[:10],
            ))
    out.sort(key=lambda m: m.date or "0000", reverse=True)
    return out


def get_movie_import_date(cfg: ArrConfig, movie_id: int) -> str:
    """Return the date of the most recent successful import for a movie (YYYY-MM-DD), or ''."""
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/v3/history/movie",
            headers=_headers(cfg),
            params={"movieId": movie_id},
        )
        if not r.is_success:
            return ""
        events = r.json()
    for ev in events:
        if ev.get("eventType") == "downloadFolderImported":
            return (ev.get("date") or "")[:10]
    return ""


# ── History / imports ─────────────────────────────────────────────────────────

@dataclass
class ArrImportRecord:
    title: str
    year: int = 0
    media_type: str = ""      # "movie" or "episode"
    season: int = 0
    episode: int = 0
    episode_title: str = ""
    imported_at: str = ""     # YYYY-MM-DD
    quality: str = ""         # e.g. "Bluray-2160p", "WEB-1080p"
    size_bytes: int = 0


_IMPORT_EVENT_TYPES = {"downloadFolderImported", "episodeFileImported", "seriesFolderImported"}


def get_today_imports(cfg: ArrConfig, target_date: date) -> list[ArrImportRecord]:
    """Return all import history events for target_date.

    Sonarr can emit downloadFolderImported, episodeFileImported, or
    seriesFolderImported depending on the download client and import mode.
    All three indicate a file landed on disk today.
    """
    date_prefix = target_date.isoformat()
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/v3/history",
            headers=_headers(cfg),
            params={
                "page": 1, "pageSize": 250, "sortKey": "date", "sortDir": "desc",
                "includeMovie": "true",    # Radarr: expand movie object
                "includeSeries": "true",   # Sonarr: expand series object
                "includeEpisode": "true",  # Sonarr: expand episode object
            },
        )
        if not r.is_success:
            return []
        data = r.json()
    records = data.get("records", []) if isinstance(data, dict) else []
    results = []
    for rec in records:
        if rec.get("eventType") not in _IMPORT_EVENT_TYPES:
            continue
        raw_date = rec.get("date") or ""
        try:
            rec_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00")) \
                .astimezone(tz=None).date().isoformat()
        except (ValueError, AttributeError):
            rec_date = raw_date[:10]
        if rec_date != date_prefix:
            continue
        movie = rec.get("movie") or {}
        series = rec.get("series") or {}
        episode = rec.get("episode") or {}
        if movie:
            mf = movie.get("movieFile") or {}
            mf_quality = ((mf.get("quality") or {}).get("quality") or {}).get("name", "")
            mf_size = int(mf.get("size", 0) or 0)
            results.append(ArrImportRecord(
                title=movie.get("title", rec.get("sourceTitle", "")),
                year=movie.get("year", 0),
                media_type="movie",
                imported_at=rec_date,
                quality=mf_quality,
                size_bytes=mf_size,
            ))
        elif series:
            # Sonarr: try episodeFile at record level (present for episodeFileImported events)
            ep_file = rec.get("episodeFile") or {}
            if not ep_file:
                # Fallback: some Sonarr versions embed file info in episode object
                ep_file = episode.get("episodeFile") or {}
            ep_quality = ((ep_file.get("quality") or {}).get("quality") or {}).get("name", "")
            ep_size = int(ep_file.get("size", 0) or 0)
            results.append(ArrImportRecord(
                title=series.get("title", ""),
                year=series.get("year", 0),
                media_type="episode",
                season=episode.get("seasonNumber", 0),
                episode=episode.get("episodeNumber", 0),
                episode_title=episode.get("title", ""),
                imported_at=rec_date,
                quality=ep_quality,
                size_bytes=ep_size,
            ))
    return results


# ── Calendar ──────────────────────────────────────────────────────────────────

@dataclass
class CalendarMovie:
    title: str
    year: int
    release_date: str     # YYYY-MM-DD (earliest of digital/physical/cinema)
    release_type: str     # "digital", "physical", or "cinema"
    has_file: bool
    monitored: bool
    arr_id: int = 0       # Radarr internal movie ID
    tmdb_id: int = 0


@dataclass
class CalendarEpisode:
    series_title: str
    season: int
    episode: int
    title: str
    air_date: str         # YYYY-MM-DD
    has_file: bool
    monitored: bool
    series_id: int = 0    # Sonarr internal series ID
    episode_id: int = 0   # Sonarr internal episode ID (from calendar response)


def get_radarr_calendar(cfg: ArrConfig, start: date, end: date) -> list[CalendarMovie]:
    """Return movies with a release date in [start, end]."""
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/v3/calendar",
            headers=_headers(cfg),
            params={"start": start.isoformat(), "end": end.isoformat()},
        )
        if not r.is_success:
            return []
        movies = r.json()

    start_str = start.isoformat()
    end_str   = end.isoformat()
    results = []
    for m in movies:
        labelled = [
            ("digital",  (m.get("digitalRelease") or "")[:10]),
            ("physical", (m.get("physicalRelease") or "")[:10]),
            ("cinema",   (m.get("inCinemas") or "")[:10]),
        ]
        valid = [(label, d) for label, d in labelled if d]
        if not valid:
            continue
        # Prefer the earliest date that falls within the query window;
        # fall back to earliest overall (keeps the movie visible if no date is in-range)
        in_range = [(label, d) for label, d in valid if start_str <= d <= end_str]
        label, release_date = min(in_range or valid, key=lambda x: x[1])
        results.append(CalendarMovie(
            title=m.get("title", ""),
            year=m.get("year", 0),
            release_date=release_date,
            release_type=label,
            has_file=m.get("hasFile", False),
            monitored=m.get("monitored", False),
            arr_id=m.get("id", 0),
            tmdb_id=m.get("tmdbId", 0),
        ))
    results.sort(key=lambda m: m.release_date)
    return results


def get_sonarr_calendar(cfg: ArrConfig, start: date, end: date) -> list[CalendarEpisode]:
    """Return episodes airing in [start, end]."""
    with _client(cfg) as client:
        r = client.get(
            f"{cfg.base_url}/api/v3/calendar",
            headers=_headers(cfg),
            params={"start": start.isoformat(), "end": end.isoformat()},
        )
        if not r.is_success:
            return []
        episodes = r.json()

    # Build a series-id → title map for episodes that lack an embedded series object
    series_ids_needed = {
        ep["seriesId"] for ep in episodes
        if ep.get("seriesId") and not (ep.get("series") or {}).get("title")
    }
    series_map: dict[int, str] = {}
    if series_ids_needed:
        with _client(cfg) as client:
            rs = client.get(f"{cfg.base_url}/api/v3/series", headers=_headers(cfg))
            if rs.is_success:
                series_map = {s["id"]: s.get("title", "") for s in rs.json()}

    results = []
    for ep in episodes:
        air_date = (ep.get("airDate") or "")[:10]
        if not air_date:
            continue
        embedded = (ep.get("series") or {}).get("title", "")
        series_title = (
            embedded
            or ep.get("seriesTitle", "")
            or series_map.get(ep.get("seriesId", 0), "")
        )
        results.append(CalendarEpisode(
            series_title=series_title,
            season=ep.get("seasonNumber", 0),
            episode=ep.get("episodeNumber", 0),
            title=ep.get("title", ""),
            air_date=air_date,
            has_file=ep.get("hasFile", False),
            monitored=ep.get("monitored", False),
            series_id=ep.get("seriesId", 0),
            episode_id=ep.get("id", 0),
        ))
    results.sort(key=lambda e: (e.air_date, e.series_title, e.season, e.episode))
    return results
