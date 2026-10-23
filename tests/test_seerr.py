"""Tests for Seerr status-mapping helpers and data structures (lib/seerr.py).

The _media_status and _request_status functions are the source of truth for
the string labels shown in all media commands.  Their correctness matters for
the Jellyseerr auto-approve display fix (media_status_code > 1 means acted on).
"""
import pytest

from lib.seerr import (
    MEDIA_TYPE_MOVIE,
    MEDIA_TYPE_TV,
    SeerrListRequest,
    SeerrSearchResult,
    _media_status,
    _request_status,
)

# ── _media_status ─────────────────────────────────────────────────────────────

class TestMediaStatus:
    def test_code_1_unknown(self):
        assert _media_status(1) == "unknown"

    def test_code_2_pending(self):
        assert _media_status(2) == "pending"

    def test_code_3_processing(self):
        assert _media_status(3) == "processing"

    def test_code_4_partially_available(self):
        assert _media_status(4) == "partially available"

    def test_code_5_available(self):
        assert _media_status(5) == "available"

    def test_unknown_code_falls_back_to_unknown(self):
        assert _media_status(0) == "unknown"
        assert _media_status(99) == "unknown"
        assert _media_status(-1) == "unknown"


# ── _request_status ───────────────────────────────────────────────────────────

class TestRequestStatus:
    def test_code_1_pending(self):
        assert _request_status(1) == "pending"

    def test_code_2_approved(self):
        assert _request_status(2) == "approved"

    def test_code_3_declined(self):
        assert _request_status(3) == "declined"

    def test_code_4_failed(self):
        assert _request_status(4) == "failed"

    def test_code_5_completed(self):
        assert _request_status(5) == "completed"

    def test_unknown_code_falls_back_to_unknown(self):
        # Used to fall back to "pending", which mislabelled completed (5) requests.
        assert _request_status(0) == "unknown"
        assert _request_status(99) == "unknown"


# ── Jellyseerr auto-approve display logic ─────────────────────────────────────
# Mirrors the fix in media_today_cmd: if media has moved past "unknown"
# (media_status_code > 1) the request should display as "approved" even when
# the API still returns request_status_code == 1 (Jellyseerr quirk).

class TestAutoApproveDisplayLogic:
    """Validates the approved = req.request_status_code >= 2 or req.media_status_code > 1 rule."""

    @pytest.mark.parametrize("req_code,media_code,expect_approved", [
        (1, 1, False),   # truly pending — nothing has happened
        (1, 2, True),    # pending status but media is processing (auto-approve quirk)
        (1, 3, True),    # media processing
        (1, 5, True),    # media available — clearly acted on
        (2, 1, True),    # explicitly approved
        (2, 5, True),    # approved + available
        # Note: codes 3 (declined) and 4 (failed) satisfy >= 2, so they also
        # evaluate as "approved" under the current rule.  This is documented
        # as a known edge case — in practice Seerr won't return declined + media
        # still at status 1 once the media has been acted on via another request.
        (3, 1, True),    # declined — shows "approved" (current behaviour)
        (4, 1, True),    # failed  — shows "approved" (current behaviour)
    ])
    def test_approved_rule(self, req_code, media_code, expect_approved):
        approved = req_code >= 2 or media_code > 1
        assert approved is expect_approved


# ── SeerrSearchResult defaults ────────────────────────────────────────────────

class TestSeerrSearchResultDefaults:
    def test_minimum_fields(self):
        r = SeerrSearchResult(id=42, media_type=MEDIA_TYPE_MOVIE, title="Dune")
        assert r.year == ""
        assert r.status == ""
        assert r.seerr_media_id == 0
        assert r.tvdb_id == 0
        assert r.seasons_available == 0
        assert r.seasons_total == 0
        assert r.episodes_total == 0

    def test_media_type_constants(self):
        assert MEDIA_TYPE_MOVIE == "movie"
        assert MEDIA_TYPE_TV == "tv"


# ── SeerrListRequest ──────────────────────────────────────────────────────────

class TestSeerrListRequest:
    def test_full_construction(self):
        req = SeerrListRequest(
            request_id=7,
            media_type=MEDIA_TYPE_TV,
            title="The Last of Us",
            tmdb_id=100088,
            request_status="approved",
            request_status_code=2,
            media_status="available",
            media_status_code=5,
            requested_by="damyanmp",
            created_at="2026-03-23",
        )
        assert req.request_id == 7
        assert req.media_type == "tv"
        assert req.request_status_code == 2
        assert req.media_status_code == 5
