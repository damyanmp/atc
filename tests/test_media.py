"""Tests for the media overhaul: Seerr status/dup-check/pagination, *arr queue
triage + indexer health + missing list, media_search config, and the CLI
guard rails. HTTP is faked at the client level — no network."""
import textwrap

import pytest

import lib.arr as arr
import lib.seerr as seerr
from config import ArrConfig, MediaSearchConfig, SeerrConfig, load_config
from lib.arr import (
    ArrQueueItem,
    HealthItem,
    IndexerHealth,
    group_queue,
    parse_failing_indexers,
)


# ── fake httpx client ─────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status
        self.text = str(data)

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.is_success:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """Routes GETs by URL substring → response (or callable(params) → response)."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params))
        for frag, resp in self.routes.items():
            if frag in url:
                return resp(params) if callable(resp) else resp
        return _Resp({}, 404)


SEERR = SeerrConfig(host="seerr.example.test", api_key="k")
SONARR = ArrConfig(name="sonarr", host="arr.example.test", api_key="k")
RADARR = ArrConfig(name="radarr", host="arr.example.test", api_key="k")


# ── Seerr status maps ─────────────────────────────────────────────────────────

class TestSeerrStatusMaps:
    def test_request_completed_is_not_pending(self):
        # Regression: status 5 used to fall back to "pending".
        assert seerr._request_status(5) == "completed"

    def test_media_blocklisted_and_deleted(self):
        assert seerr._media_status(6) == "blocklisted"
        assert seerr._media_status(7) == "deleted"

    def test_unknown_codes(self):
        assert seerr._request_status(99) == "unknown"
        assert seerr._media_status(99) == "unknown"


class TestParseResult:
    def test_poster_url_and_tvdb(self):
        r = seerr._parse_result({
            "id": 1, "name": "Show", "firstAirDate": "2020-01-01", "posterPath": "/p.jpg",
            "externalIds": {"tvdbId": 42}, "mediaInfo": {"id": 9, "status": 4},
        }, "tv")
        assert r.poster_url == "https://image.tmdb.org/t/p/w500/p.jpg"
        assert (r.year, r.tvdb_id, r.status, r.seerr_media_id) == ("2020", 42, "partially available", 9)

    def test_no_poster(self):
        assert seerr._parse_result({"id": 1, "title": "M"}, "movie").poster_url == ""


# ── Seerr duplicate check ─────────────────────────────────────────────────────

def _seerr_with(monkeypatch, routes):
    c = _Client(routes)
    monkeypatch.setattr(seerr, "_client", lambda cfg: c)
    return c


class TestCheckRequestable:
    def test_available_movie_refused(self, monkeypatch):
        _seerr_with(monkeypatch, {"/movie/1": _Resp({"title": "M", "mediaInfo": {"status": 5}})})
        chk = seerr.check_requestable(SEERR, 1, "movie")
        assert not chk.ok and "already available" in chk.reason and chk.title == "M"

    def test_active_request_refused(self, monkeypatch):
        _seerr_with(monkeypatch, {"/movie/1": _Resp({"title": "M", "mediaInfo": {
            "status": 1, "requests": [{"id": 7, "status": 2}]}})})
        chk = seerr.check_requestable(SEERR, 1, "movie")
        assert not chk.ok and "#7" in chk.reason

    def test_deleted_movie_can_be_rerequested(self, monkeypatch):
        _seerr_with(monkeypatch, {"/movie/1": _Resp({"title": "M", "mediaInfo": {"status": 7}})})
        assert seerr.check_requestable(SEERR, 1, "movie").ok

    def test_new_movie_ok(self, monkeypatch):
        _seerr_with(monkeypatch, {"/movie/1": _Resp({"title": "M"})})
        assert seerr.check_requestable(SEERR, 1, "movie").ok

    def test_tv_seasons_drop_covered(self, monkeypatch):
        _seerr_with(monkeypatch, {"/tv/2": _Resp({"name": "S", "mediaInfo": {
            "status": 4,
            "seasons": [{"seasonNumber": 1, "status": 5}, {"seasonNumber": 2, "status": 1},
                        {"seasonNumber": 3, "status": 7}],
            "requests": [{"id": 3, "status": 1, "seasons": [{"seasonNumber": 4}]}],
        }})})
        chk = seerr.check_requestable(SEERR, 2, "tv", [1, 2, 3, 4])
        assert chk.ok and chk.seasons == [2, 3]
        assert "1, 4" in chk.reason

    def test_tv_all_seasons_covered_refused(self, monkeypatch):
        _seerr_with(monkeypatch, {"/tv/2": _Resp({"name": "S", "mediaInfo": {
            "seasons": [{"seasonNumber": 1, "status": 5}]}})})
        chk = seerr.check_requestable(SEERR, 2, "tv", [1])
        assert not chk.ok and chk.seasons == []

    def test_unknown_tmdb(self, monkeypatch):
        _seerr_with(monkeypatch, {})
        assert not seerr.check_requestable(SEERR, 404, "movie").ok


# ── Seerr request list pagination ─────────────────────────────────────────────

def _req(i, status=2, media_status=3):
    return {"id": i, "status": status, "type": "movie",
            "media": {"tmdbId": i, "status": media_status, "mediaType": "movie"}}


class TestListRequests:
    def _pages(self, total, monkeypatch):
        def page(params):
            s, t = params["skip"], params["take"]
            return _Resp({"pageInfo": {"results": total},
                          "results": [_req(i) for i in range(s, min(s + t, total))]})
        return _seerr_with(monkeypatch, {"/api/v1/request": page})

    def test_single_page_by_default(self, monkeypatch):
        self._pages(250, monkeypatch)
        assert len(seerr.list_requests(SEERR, take=100, enrich=False)) == 100

    def test_fetch_all_pages(self, monkeypatch):
        c = self._pages(250, monkeypatch)
        out = seerr.list_requests(SEERR, take=100, fetch_all=True, enrich=False)
        assert len(out) == 250 and len(c.calls) == 3

    def test_completed_status_label(self, monkeypatch):
        _seerr_with(monkeypatch, {"/api/v1/request": _Resp({
            "pageInfo": {"results": 1}, "results": [_req(1, status=5, media_status=7)]})})
        r = seerr.list_requests(SEERR, enrich=False)[0]
        assert (r.request_status, r.media_status) == ("completed", "deleted")

    def test_bad_filter_rejected(self):
        with pytest.raises(ValueError):
            seerr.list_requests(SEERR, filter="nope")


# ── *arr queue triage ─────────────────────────────────────────────────────────

class TestQueueStuck:
    def test_import_blocked_is_stuck(self):
        assert ArrQueueItem(1, "x", tracked_state="importBlocked").stuck

    def test_plain_download_not_stuck(self):
        assert not ArrQueueItem(1, "x", status="downloading", tracked_state="downloading",
                                tracked_status="ok").stuck

    def test_import_pending_only_stuck_with_error(self):
        assert not ArrQueueItem(1, "x", tracked_state="importPending").stuck
        assert ArrQueueItem(1, "x", tracked_state="importPending", error_message="nope").stuck

    def test_warning_status_is_stuck(self):
        assert ArrQueueItem(1, "x", status="warning").stuck

    def test_hints(self):
        assert "safe to remove" in ArrQueueItem(
            1, "x", status_messages=["Episode file already imported at 09/25"]).hint
        assert "blocklist" in ArrQueueItem(1, "x", error_message="The download is stalled").hint
        assert ArrQueueItem(1, "x").hint == ""


class TestGroupQueue:
    def test_groups_by_download_id_in_order(self):
        items = [ArrQueueItem(1, "pack", download_id="A"), ArrQueueItem(2, "solo", download_id="B"),
                 ArrQueueItem(3, "pack", download_id="A")]
        groups = group_queue(items)
        assert [g.title for g in groups] == ["pack", "solo"]
        assert len(groups[0].items) == 2

    def test_missing_download_id_not_merged(self):
        assert len(group_queue([ArrQueueItem(1, "a"), ArrQueueItem(2, "b")])) == 2


class TestQueueError:
    def test_prefers_error_message(self):
        assert arr._queue_error({"errorMessage": "boom", "statusMessages": [{"title": "t"}]}) == "boom"

    def test_falls_back_to_title_when_no_messages(self):
        # Regression: importBlocked packs carry the reason in title, messages=[]
        rec = {"statusMessages": [{"title": "One or more episodes expected", "messages": []}]}
        assert arr._queue_error(rec) == "One or more episodes expected"


# ── indexer health ────────────────────────────────────────────────────────────

class TestIndexerHealth:
    def test_parse_failing(self):
        h = [HealthItem("warning", "IndexerStatusCheck", "Indexers unavailable due to failures: a, b"),
             HealthItem("warning", "IndexerLongTermStatusCheck",
                        "Indexers unavailable due to failures for more than 6 hours: c"),
             HealthItem("warning", "AllowedHostsCheck", "Allowed Hosts: x, y")]
        assert parse_failing_indexers(h) == {"a", "b", "c"}

    def test_ratio_only_counts_auto_search(self):
        ih = IndexerHealth(auto_search=["a", "b", "c", "d"], failing={"a", "zzz"})
        assert (ih.healthy, ih.total, ih.ratio) == (3, 4, 0.75)

    def test_empty(self):
        assert IndexerHealth([], set()).ratio == 0.0


# ── wanted/missing ────────────────────────────────────────────────────────────

class TestGetMissing:
    def test_radarr_skips_unavailable_and_sorts_newest(self, monkeypatch):
        c = _Client({"/wanted/missing": _Resp({"records": [
            {"id": 1, "title": "Old", "year": 2001, "digitalRelease": "2001-01-01", "isAvailable": True},
            {"id": 2, "title": "Future", "year": 2030, "digitalRelease": "2030-01-01", "isAvailable": False},
            {"id": 3, "title": "New", "year": 2024, "inCinemas": "2024-05-01", "isAvailable": True},
        ]})})
        monkeypatch.setattr(arr, "_client", lambda cfg: c)
        assert [m.title for m in arr.get_missing(RADARR)] == ["New", "Old"]

    def test_sonarr_uses_episode_ids(self, monkeypatch):
        c = _Client({"/wanted/missing": _Resp({"records": [
            {"id": 55, "seriesId": 9, "seasonNumber": 2, "episodeNumber": 3, "title": "Ep",
             "airDateUtc": "2026-01-02T00:00:00Z", "series": {"title": "Show"}},
        ]})})
        monkeypatch.setattr(arr, "_client", lambda cfg: c)
        m = arr.get_missing(SONARR)[0]
        assert (m.arr_id, m.series_id, m.title, m.date) == (55, 9, "Show", "2026-01-02")
        assert c.calls[0][1]["sortKey"] == "episodes.airDateUtc"


# ── media_search config ───────────────────────────────────────────────────────

def _yaml(tmp_path, extra=""):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""\
        proxmox:
          host: pve.local
          user: root@pam
          token_name: atc
          token_value: plaintoken
        """) + extra)
    return p


class TestMediaSearchConfig:
    def test_defaults_when_absent(self, tmp_path):
        ms = load_config(_yaml(tmp_path)).media_search
        assert (ms.enabled, ms.max_items, ms.min_healthy_pct) == (False, 10, 50)

    def test_parsed_and_clamped(self, tmp_path):
        ms = load_config(_yaml(tmp_path, "media_search:\n  enabled: true\n  max_items: 0\n"
                                         "  min_healthy_pct: 150\n")).media_search
        assert (ms.enabled, ms.max_items, ms.min_healthy_pct) == (True, 1, 100)


# ── CLI helpers + guard rails ────────────────────────────────────────────────

import cli  # noqa: E402  (after lib imports so sys.path is set up the same way)


class _Cfg:
    def __init__(self, **ms):
        self.media_search = MediaSearchConfig(**ms)


class TestSearchGate:
    def _ih(self, monkeypatch, healthy, total):
        names = [f"i{n}" for n in range(total)]
        ih = IndexerHealth(auto_search=names, failing=set(names[healthy:]))
        monkeypatch.setattr(cli, "arr_get_indexer_health", lambda cfg: ih)

    def test_disabled_refuses_without_force(self, monkeypatch):
        self._ih(monkeypatch, 4, 4)
        ok, msg = cli._search_gate(_Cfg(enabled=False), RADARR, "Radarr", force=False)
        assert not ok and "--force" in msg

    def test_force_overrides_disabled(self, monkeypatch):
        self._ih(monkeypatch, 4, 4)
        assert cli._search_gate(_Cfg(enabled=False), RADARR, "Radarr", force=True)[0]

    def test_unhealthy_indexers_refuse(self, monkeypatch):
        self._ih(monkeypatch, 1, 4)
        ok, msg = cli._search_gate(_Cfg(enabled=True, min_healthy_pct=50), RADARR, "Radarr", force=False)
        assert not ok and "1/4" in msg

    def test_healthy_enough_allowed(self, monkeypatch):
        self._ih(monkeypatch, 2, 4)
        assert cli._search_gate(_Cfg(enabled=True, min_healthy_pct=50), RADARR, "Radarr", force=False)[0]

    def test_no_auto_search_indexers_refused_even_with_force(self, monkeypatch):
        self._ih(monkeypatch, 0, 0)
        assert not cli._search_gate(_Cfg(enabled=True), RADARR, "Radarr", force=True)[0]


class TestCliHelpers:
    def test_strip_year(self):
        assert cli._strip_year("Dark Matter (2024)") == ("Dark Matter", 2024)
        assert cli._strip_year("Formula 1") == ("Formula 1", 0)

    def test_parse_seasons(self):
        assert cli._parse_seasons("1,3") == [1, 3]
        assert cli._parse_seasons("2-4,1") == [1, 2, 3, 4]
        assert cli._parse_seasons("") is None

    def test_parse_seasons_rejects_garbage(self):
        import typer
        with pytest.raises(typer.Exit):
            cli._parse_seasons("one")

    def test_stage_download_flags_removed_file(self):
        from lib.arr import ArrMovieRecord
        rec = ArrMovieRecord(arr_id=1, title="M", year=2020, tmdb_id=1, monitored=False, has_file=False)
        icon, _, summary, _ = cli._stage_download([], rec, "2026-05-14")
        assert icon == "✗" and "removed" in summary

    def test_stage_download_stuck_queue(self):
        q = ArrQueueItem(1, "x", tracked_state="importBlocked",
                         status_messages=["Episode file already imported at 09/25"])
        icon, _, summary, detail = cli._stage_download([q], None, "")
        assert icon == "✗" and "Stuck" in summary and "safe to remove" in detail
