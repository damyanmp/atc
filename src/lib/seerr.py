"""Seerr (Overseerr/Jellyseerr) API client.

Supports searching for movies/TV shows and submitting requests.
Authentication uses an API key via the X-Api-Key header.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from config import SeerrConfig

MEDIA_TYPE_MOVIE = "movie"
MEDIA_TYPE_TV = "tv"


@dataclass
class SeerrSearchResult:
    id: int                  # TMDB ID
    media_type: str          # "movie" or "tv"
    title: str
    year: str = ""
    overview: str = ""
    status: str = ""         # label from _media_status()
    poster_url: str = ""     # full TMDB poster URL (w500), "" when TMDB has none
    # Populated from mediaInfo when Seerr has a record for this title
    seerr_media_id: int = 0          # Seerr internal media record ID
    external_service_id: int = 0     # Radarr/Sonarr internal ID (0 = not linked)
    tvdb_id: int = 0                 # TVDB ID for TV shows (used by Sonarr)
    media_status_code: int = 1       # 1=unknown 2=pending 3=processing 4=partial 5=available 6=blocklisted 7=deleted
    request_status_code: int = 0     # 0=none 1=pending 2=approved 3=declined 4=failed 5=completed
    request_by: str = ""
    request_date: str = ""
    seasons_available: int = 0       # TV only: seasons with status==5
    seasons_total: int = 0           # TV only: total seasons tracked by Seerr
    seasons_requested: int = 0       # TV only: seasons in the latest request object
    episodes_total: int = 0          # TV only: total episode count across all tracked seasons
    episodes_requested: int = 0      # TV only: episode count across requested seasons only
    episodes_available: int = 0      # TV only: episode count across seasons with status==5


@dataclass
class SeerrRequest:
    request_id: int
    media_type: str
    title: str
    status: str              # see _request_status()
    requested_by: str = ""
    created_at: str = ""


@dataclass
class SeerrListRequest:
    request_id: int
    media_type: str          # "movie" or "tv"
    title: str
    tmdb_id: int
    request_status: str      # see _request_status()
    request_status_code: int
    media_status: str        # see _media_status()
    media_status_code: int
    requested_by: str
    created_at: str          # YYYY-MM-DD


def _headers(cfg: SeerrConfig) -> dict:
    return {
        "X-Api-Key": cfg.api_key,
        "Content-Type": "application/json",
    }


def _client(cfg: SeerrConfig) -> httpx.Client:
    return httpx.Client(verify=cfg.verify_ssl, timeout=15)


def search(cfg: SeerrConfig, query: str, language: str = "en") -> list[SeerrSearchResult]:
    """Search for movies and TV shows. Returns combined results."""
    with _client(cfg) as client:
        # Seerr rejects '+' (from urlencode) — must use %20 for spaces
        encoded_query = quote(query, safe="")
        r = client.get(
            f"{cfg.base_url}/api/v1/search?query={encoded_query}&page=1&language={language}",
            headers=_headers(cfg),
        )
        if not r.is_success:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
        data = r.json()

    results = []
    for item in data.get("results", []):
        media_type = item.get("mediaType", "")
        if media_type not in (MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV):
            continue
        results.append(_parse_result(item, media_type))
    return results


TMDB_POSTER_BASE = "https://image.tmdb.org/t/p/w500"


def _parse_result(item: dict, media_type: str) -> SeerrSearchResult:
    """Build a SeerrSearchResult from a /search hit or a /movie|/tv detail body."""
    title = item.get("title") or item.get("name") or ""
    release = item.get("releaseDate") or item.get("firstAirDate") or ""
    year = release[:4] if release else ""

    media_info = item.get("mediaInfo") or {}
    status_code = media_info.get("status", 1)
    status = _media_status(status_code)

    # Extract the latest request info
    requests = media_info.get("requests") or []
    req_status_code = 0
    req_by = ""
    req_date = ""
    seasons_requested = 0
    if requests:
        latest = requests[0]
        req_status_code = latest.get("status", 0)
        req_by = (latest.get("requestedBy") or {}).get("displayName", "")
        req_date = (latest.get("createdAt") or "")[:10]
        seasons_requested = len(latest.get("seasons") or [])

    # TVDB ID for TV shows (Sonarr uses TVDB)
    tvdb_id = (
        (item.get("externalIds") or {}).get("tvdbId")
        or media_info.get("tvdbId")
        or 0
    )

    # TV season availability counts
    seerr_seasons = media_info.get("seasons") or []
    seasons_available = sum(1 for s in seerr_seasons if s.get("status") == 5)
    seasons_total = len(seerr_seasons)

    poster = item.get("posterPath") or ""
    return SeerrSearchResult(
        id=item["id"],
        media_type=media_type,
        title=title,
        year=year,
        overview=(item.get("overview") or "")[:120],
        status=status,
        poster_url=f"{TMDB_POSTER_BASE}{poster}" if poster else "",
        seerr_media_id=media_info.get("id", 0),
        external_service_id=media_info.get("externalServiceId") or 0,
        tvdb_id=tvdb_id or 0,
        media_status_code=status_code,
        request_status_code=req_status_code,
        request_by=req_by,
        request_date=req_date,
        seasons_available=seasons_available,
        seasons_total=seasons_total,
        seasons_requested=seasons_requested,
    )


def get_details(cfg: SeerrConfig, tmdb_id: int, media_type: str) -> SeerrSearchResult:
    """Look up one title by TMDB ID (no search disambiguation needed).

    TMDB IDs are NOT unique across movie/tv, so the type is required.
    """
    if media_type not in (MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV):
        raise ValueError(f"media_type must be 'movie' or 'tv', got {media_type!r}")
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/v1/{media_type}/{tmdb_id}", headers=_headers(cfg))
        if r.status_code == 404:
            raise LookupError(f"No {media_type} with TMDB ID {tmdb_id}")
        if not r.is_success:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
    return _parse_result(data, media_type)


def get_version(cfg: SeerrConfig) -> str:
    """Seerr server version from /api/v1/status ("" if not reported)."""
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/v1/status", headers=_headers(cfg))
        r.raise_for_status()
        return r.json().get("version", "") or ""


# Media/season statuses that mean "already covered — don't request again".
_COVERED_MEDIA_CODES = {2, 3, 5}          # pending, processing, available
_ACTIVE_REQUEST_CODES = {1, 2}            # pending, approved (not yet completed)


@dataclass
class RequestCheck:
    ok: bool                       # False = would duplicate an existing/available item
    reason: str                    # why refused, or a note (e.g. dropped seasons); "" if nothing to say
    title: str = ""
    seasons: list[int] | None = None   # TV: seasons still worth requesting (None = all)


def check_requestable(
    cfg: SeerrConfig, tmdb_id: int, media_type: str, seasons: list[int] | None = None,
) -> RequestCheck:
    """Decide whether a request would be a duplicate.

    For TV with explicit seasons, the returned seasons have already-covered
    ones (available/processing/pending, or in an active request) removed.
    """
    with _client(cfg) as client:
        r = client.get(f"{cfg.base_url}/api/v1/{media_type}/{tmdb_id}", headers=_headers(cfg))
        if r.status_code == 404:
            return RequestCheck(False, f"No {media_type} with TMDB ID {tmdb_id}")
        r.raise_for_status()
        data = r.json()
    title = data.get("title") or data.get("name") or f"TMDB #{tmdb_id}"
    mi = data.get("mediaInfo") or {}
    code = mi.get("status", 1) or 1
    active = [q for q in (mi.get("requests") or []) if q.get("status") in _ACTIVE_REQUEST_CODES]

    if media_type == MEDIA_TYPE_MOVIE:
        if code == 5:
            return RequestCheck(False, f"{title} is already available", title)
        if active:
            q = active[0]
            return RequestCheck(False, f"{title} already requested (#{q['id']}, {_request_status(q['status'])})", title)
        if code in (2, 3):
            return RequestCheck(False, f"{title} is already {_media_status(code)}", title)
        return RequestCheck(True, "", title)

    # TV
    season_codes = {s.get("seasonNumber"): (s.get("status") or 1) for s in (mi.get("seasons") or [])}
    active_seasons = {s.get("seasonNumber") for q in active for s in (q.get("seasons") or [])}
    covered = {n for n, c in season_codes.items() if c in _COVERED_MEDIA_CODES} | active_seasons
    if seasons:
        wanted = [n for n in seasons if n not in covered]
        dropped = [n for n in seasons if n in covered]
        if not wanted:
            listed = ", ".join(map(str, seasons))
            return RequestCheck(False, f"{title}: season(s) {listed} already available/requested", title, [])
        note = f"skipping already covered season(s) {', '.join(map(str, dropped))}" if dropped else ""
        return RequestCheck(True, note, title, wanted)
    if code == 5:
        return RequestCheck(False, f"{title} is already fully available", title)
    return RequestCheck(True, "", title)


def request_media(
    cfg: SeerrConfig, tmdb_id: int, media_type: str, seasons: list[int] | None = None,
) -> SeerrRequest:
    """Submit a request for a movie or TV show by TMDB ID.

    TV: `seasons` limits the request to those season numbers; None = all.
    """
    if media_type not in (MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV):
        raise ValueError(f"media_type must be 'movie' or 'tv', got {media_type!r}")

    payload: dict = {"mediaType": media_type, "mediaId": tmdb_id}
    if media_type == MEDIA_TYPE_TV:
        payload["seasons"] = sorted(set(seasons)) if seasons else "all"

    with _client(cfg) as client:
        r = client.post(
            f"{cfg.base_url}/api/v1/request",
            headers=_headers(cfg),
            json=payload,
        )
        if not r.is_success:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
        data = r.json()

    media = data.get("media") or {}
    title = media.get("title") or media.get("name") or str(tmdb_id)
    requested_by = (data.get("requestedBy") or {}).get("displayName", "")

    return SeerrRequest(
        request_id=data["id"],
        media_type=media_type,
        title=title,
        status=_request_status(data.get("status", 1)),
        requested_by=requested_by,
        created_at=data.get("createdAt", ""),
    )


def _fetch_title(cfg: SeerrConfig, media_type: str, tmdb_id: int) -> str:
    """Fetch the title for a single media item from its detail endpoint."""
    endpoint = "movie" if media_type == MEDIA_TYPE_MOVIE else "tv"
    try:
        with _client(cfg) as client:
            r = client.get(
                f"{cfg.base_url}/api/v1/{endpoint}/{tmdb_id}",
                headers=_headers(cfg),
            )
            if not r.is_success:
                return f"TMDB #{tmdb_id}"
            data = r.json()
            return data.get("title") or data.get("name") or f"TMDB #{tmdb_id}"
    except Exception:
        return f"TMDB #{tmdb_id}"


# Server-side filters Seerr's /request endpoint accepts.
REQUEST_FILTERS = ("all", "approved", "available", "pending", "processing",
                   "unavailable", "failed", "deleted", "completed")


def list_requests(
    cfg: SeerrConfig,
    *,
    take: int = 100,
    skip: int = 0,
    filter: str = "all",
    fetch_all: bool = False,
    enrich: bool = True,
) -> list[SeerrListRequest]:
    """Fetch media requests from Seerr, newest first.

    fetch_all=True pages through every request (take = page size) instead of
    returning just one page — use it whenever the caller filters client-side,
    otherwise "unfulfilled" silently means "unfulfilled among the newest N".
    enrich=False skips the per-row title lookups (titles become "TMDB #id");
    call enrich_titles() on the subset you actually display.
    """
    if filter not in REQUEST_FILTERS:
        raise ValueError(f"filter must be one of {REQUEST_FILTERS}, got {filter!r}")
    raw: list[dict] = []
    with _client(cfg) as client:
        while True:
            r = client.get(
                f"{cfg.base_url}/api/v1/request",
                params={"take": take, "skip": skip, "sort": "added", "filter": filter},
                headers=_headers(cfg),
            )
            if not r.is_success:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
            data = r.json()
            page = data.get("results", [])
            raw.extend(page)
            total = (data.get("pageInfo") or {}).get("results", 0)
            skip += len(page)
            if not fetch_all or not page or skip >= total:
                break

    results = [_parse_request(item) for item in raw]
    if enrich:
        enrich_titles(cfg, results)
    return results


def _parse_request(item: dict) -> SeerrListRequest:
    media = item.get("media") or {}
    media_type = item.get("type") or media.get("mediaType", "")
    tmdb_id = media.get("tmdbId", 0)
    req_status_code = item.get("status") or 1
    media_status_code = media.get("status") or 1
    # Jellyseerr auto-approve leaves request at status=1 (pending) even though the
    # media is already being processed. If the media has moved past "unknown", the
    # request was clearly acted upon — treat it as approved.
    if req_status_code == 1 and media_status_code > 1:
        req_status_code = 2
    requested_by = (item.get("requestedBy") or {}).get("displayName", "")
    created_at = (item.get("createdAt") or "")[:10]

    return SeerrListRequest(
        request_id=item["id"],
        media_type=media_type,
        title=f"TMDB #{tmdb_id}",  # filled by enrich_titles()
        tmdb_id=tmdb_id,
        request_status=_request_status(req_status_code),
        request_status_code=req_status_code,
        media_status=_media_status(media_status_code),
        media_status_code=media_status_code,
        requested_by=requested_by,
        created_at=created_at,
    )


def enrich_titles(cfg: SeerrConfig, results: list[SeerrListRequest]) -> None:
    """Fill in titles in place — the /request endpoint doesn't return them.

    One detail call per unique (type, tmdb_id), run concurrently.
    """
    keys = {(r.media_type, r.tmdb_id) for r in results if r.tmdb_id}
    if not keys:
        return
    titles: dict[tuple[str, int], str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_title, cfg, mt, tid): (mt, tid) for mt, tid in keys}
        for future in as_completed(futures):
            titles[futures[future]] = future.result()
    for r in results:
        if (r.media_type, r.tmdb_id) in titles:
            r.title = titles[(r.media_type, r.tmdb_id)]


def enrich_tv_seasons(cfg: SeerrConfig, result: SeerrSearchResult) -> None:
    """Fetch full season/episode data from /api/v1/tv/{tmdbId} and update result in-place.

    Fills in seasons_total, seasons_available, seasons_requested, episodes_total,
    episodes_requested, and episodes_available so the pipeline can show
    "N/M seasons · A/B eps" (requested/available vs total).
    """
    if result.media_type != MEDIA_TYPE_TV:
        return
    try:
        with _client(cfg) as client:
            r = client.get(f"{cfg.base_url}/api/v1/tv/{result.id}", headers=_headers(cfg))
            if not r.is_success:
                return
            data = r.json()

        media_info = data.get("mediaInfo") or {}

        # Seerr season records — have per-season status but no episode counts
        seerr_seasons = [s for s in (media_info.get("seasons") or []) if s.get("seasonNumber", 0) > 0]

        # TMDB season records — have episode counts but no Seerr status
        tmdb_seasons = [s for s in (data.get("seasons") or []) if s.get("seasonNumber", 0) > 0]
        ep_count: dict[int, int] = {s["seasonNumber"]: s.get("episodeCount", 0) for s in tmdb_seasons}

        if seerr_seasons:
            result.seasons_total = len(seerr_seasons)
            result.seasons_available = sum(1 for s in seerr_seasons if s.get("status") == 5)
        elif tmdb_seasons:
            result.seasons_total = len(tmdb_seasons)

        result.episodes_total = sum(ep_count.values())
        result.episodes_available = sum(
            ep_count.get(s["seasonNumber"], 0)
            for s in seerr_seasons
            if s.get("status") == 5
        )

        # Requested seasons: prefer explicit request object; fall back to seasons
        # that Seerr is actively tracking (status > 1 = anything beyond "unknown").
        # This handles the case where a request was already fulfilled and no longer
        # appears in the requests list.
        requests = media_info.get("requests") or []
        if requests:
            req_season_nums = {s["seasonNumber"] for s in (requests[0].get("seasons") or [])}
            result.seasons_requested = len(req_season_nums)
            result.episodes_requested = sum(ep_count.get(n, 0) for n in req_season_nums)
        elif seerr_seasons:
            active_nums = {s["seasonNumber"] for s in seerr_seasons if s.get("status", 1) > 1}
            result.seasons_requested = len(active_nums)
            result.episodes_requested = sum(ep_count.get(n, 0) for n in active_nums)
    except Exception:
        pass


# Seerr MediaStatus enum (server/constants/media.ts).
_MEDIA_STATUS = {
    1: "unknown",
    2: "pending",
    3: "processing",
    4: "partially available",
    5: "available",
    6: "blocklisted",
    7: "deleted",        # was available, file since removed from the media server
}

# Seerr MediaRequestStatus enum. 5 (completed) was previously unmapped and
# fell back to "pending" — that's why fulfilled requests showed as pending.
_REQUEST_STATUS = {
    1: "pending",
    2: "approved",
    3: "declined",
    4: "failed",
    5: "completed",
}


def _media_status(code: int) -> str:
    return _MEDIA_STATUS.get(code, "unknown")


def _request_status(code: int) -> str:
    return _REQUEST_STATUS.get(code, "unknown")
