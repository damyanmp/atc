"""Tests for lib/registry.py — image ref parsing, semver, and n-2 version logic."""
from __future__ import annotations

from lib.registry import (
    ImageRef,
    VersionInfo,
    _parse_semver,
    _tag_quality,
    parse_image_ref,
)


# ── parse_image_ref ─────────────────────────────────────────────────────────

class TestParseImageRef:
    def test_dockerhub_namespaced(self):
        ref = parse_image_ref("n8nio/n8n")
        assert ref == ImageRef(registry="", namespace="n8nio", image="n8n", tag="latest")

    def test_dockerhub_official_image(self):
        ref = parse_image_ref("couchdb:3")
        assert ref.registry == ""
        assert ref.namespace == "library"
        assert ref.image == "couchdb"
        assert ref.tag == "3"

    def test_custom_registry(self):
        ref = parse_image_ref("docker.n8n.io/n8nio/n8n")
        assert ref.registry == "docker.n8n.io"
        assert ref.namespace == "n8nio"
        assert ref.image == "n8n"
        assert ref.tag == "latest"

    def test_ghcr_with_tag(self):
        ref = parse_image_ref("ghcr.io/damyanmp/peppermint-backend:v1.16.1")
        assert ref.registry == "ghcr.io"
        assert ref.namespace == "damyanmp"
        assert ref.image == "peppermint-backend"
        assert ref.tag == "v1.16.1"

    def test_localhost_registry(self):
        ref = parse_image_ref("localhost:5000/myimage:latest")
        assert ref.registry == "localhost:5000"
        assert ref.namespace == "library"
        assert ref.image == "myimage"
        assert ref.tag == "latest"

    def test_no_tag_defaults_latest(self):
        ref = parse_image_ref("nickfedor/watchtower")
        assert ref.tag == "latest"

    def test_full_name_property(self):
        ref = ImageRef(registry="ghcr.io", namespace="damyanmp", image="peppermint", tag="v1")
        assert ref.full_name == "ghcr.io/damyanmp/peppermint"

    def test_full_name_no_registry(self):
        ref = ImageRef(registry="", namespace="library", image="postgres", tag="16")
        assert ref.full_name == "library/postgres"

    def test_hub_repo_property(self):
        ref = ImageRef(registry="", namespace="n8nio", image="n8n", tag="latest")
        assert ref.hub_repo == "n8nio/n8n"


# ── _parse_semver ────────────────────────────────────────────────────────────

class TestParseSemver:
    def test_full_semver_with_v_prefix(self):
        assert _parse_semver("v1.16.1") == (1, 16, 1)

    def test_full_semver_no_prefix(self):
        assert _parse_semver("1.16.1") == (1, 16, 1)

    def test_two_part_version(self):
        assert _parse_semver("16.9") == (16, 9, 0)

    def test_two_part_with_v_prefix(self):
        assert _parse_semver("v1.28") == (1, 28, 0)

    def test_variant_suffix_still_parses(self):
        assert _parse_semver("1.28.0-alpine") == (1, 28, 0)

    def test_non_numeric_tag_returns_none(self):
        assert _parse_semver("latest") is None

    def test_nightly_yyyymmdd_rejected(self):
        assert _parse_semver("20210301.10-unstable") is None

    def test_single_number_returns_none(self):
        assert _parse_semver("3") is None


# ── _tag_quality ─────────────────────────────────────────────────────────────

class TestTagQuality:
    def test_plain_semver_is_best(self):
        assert _tag_quality("1.28.0") == 0
        assert _tag_quality("v1.28.0") == 0
        assert _tag_quality("1.28") == 0

    def test_variant_suffix_scores_1(self):
        assert _tag_quality("1.28.0-alpine") == 1
        assert _tag_quality("1.28.0-bookworm") == 1

    def test_arch_suffix_scores_2(self):
        assert _tag_quality("1.28.0-arm64") == 2
        assert _tag_quality("1.28.0-amd64") == 2

    def test_platform_suffix_scores_3(self):
        assert _tag_quality("1.28.0-windowsltsc2025-amd64") == 3


# ── VersionInfo.n_minus / recommended / current_position ────────────────────

def _vi(ref_tag: str, tags: list[str]) -> VersionInfo:
    ref = ImageRef(registry="", namespace="library", image="test", tag=ref_tag)
    parsed = sorted(
        ((v, t) for t in tags if (v := _parse_semver(t)) is not None),
        reverse=True,
    )
    return VersionInfo(ref=ref, semver_tags=parsed)


class TestVersionInfoThreePartImage:
    """3-part versioned image (e.g. n8n): groups by (major, minor)."""

    TAGS = ["1.4.0", "1.4.1", "1.3.0", "1.3.2", "1.2.0", "1.1.0"]

    def test_latest_tag(self):
        vi = _vi("1.4.1", self.TAGS)
        assert vi.latest_tag == "1.4.1"

    def test_recommended_is_n_minus_2(self):
        vi = _vi("1.4.1", self.TAGS)
        # groups (by major.minor): 1.4 (n=0), 1.3 (n=1), 1.2 (n=2)
        assert vi.recommended == "1.2.0"

    def test_current_position_latest(self):
        vi = _vi("1.4.1", self.TAGS)
        assert vi.current_position() == "latest"

    def test_current_position_n_minus_2(self):
        vi = _vi("1.2.0", self.TAGS)
        assert vi.current_position() == "n-2"

    def test_current_position_unlisted(self):
        vi = _vi("2.0.0", self.TAGS)
        assert vi.current_position() == "unlisted"

    def test_is_floating_for_latest_tag(self):
        vi = _vi("latest", self.TAGS)
        assert vi.is_floating

    def test_is_pinned_not_floating(self):
        vi = _vi("1.4.1", self.TAGS)
        assert not vi.is_floating


class TestVersionInfoTwoPartImage:
    """2-part versioned image (e.g. postgres): groups by major only."""

    TAGS = ["18.0", "17.5", "17.4", "16.9", "16.8", "15.3"]

    def test_two_part_detection(self):
        vi = _vi("18.0", self.TAGS)
        assert vi._two_part_image

    def test_recommended_groups_by_major(self):
        vi = _vi("18.0", self.TAGS)
        # n-2 from 18 → 16 (18=n0, 17=n1, 16=n2), best patch within 16.x = 16.9
        assert vi.recommended == "16.9"

    def test_current_position_for_16(self):
        vi = _vi("16.9", self.TAGS)
        assert vi.current_position() == "n-2"


class TestVersionInfoQualityPreference:
    def test_prefers_plain_tag_over_variant_at_same_version(self):
        ref = ImageRef(registry="", namespace="library", image="test", tag="1.0.0")
        tags = ["1.2.0-alpine", "1.2.0", "1.2.0-arm64"]
        parsed = sorted(
            ((v, t) for t in tags if (v := _parse_semver(t)) is not None),
            reverse=True,
        )
        vi = VersionInfo(ref=ref, semver_tags=parsed)
        # all three parse to the same (1,2,0) — n_minus(0) should prefer the plain tag
        assert vi.n_minus(0) == "1.2.0"


class TestVersionInfoEdgeCases:
    def test_no_tags_recommended_is_none(self):
        vi = VersionInfo(ref=ImageRef("", "library", "x", "latest"))
        assert vi.recommended is None

    def test_fewer_lines_than_n_returns_none(self):
        vi = _vi("1.0.0", ["1.0.0"])
        assert vi.n_minus(2) is None

    def test_fetch_error_preserved(self):
        vi = VersionInfo(ref=ImageRef("", "library", "x", "latest"), fetch_error="HTTP 429")
        assert vi.fetch_error == "HTTP 429"
        assert vi.recommended is None
