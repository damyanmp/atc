"""Docker registry client — fetch available image tags for n-2 version tracking.

Supports Docker Hub (hub.docker.com) and any Docker Registry v2 compatible
endpoint (e.g. docker.n8n.io, ghcr.io, local registries).

Docker Hub rate limits unauthenticated tag-list API calls aggressively.
Pass a DockerHubCreds with a Personal Access Token to avoid 429 errors when
checking many images at once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import NamedTuple

import httpx


class DockerHubCreds(NamedTuple):
    username: str
    token: str   # Docker Hub Personal Access Token


# ── Image reference parsing ───────────────────────────────────────────────────

@dataclass
class ImageRef:
    registry: str    # "" = Docker Hub; otherwise hostname[:port]
    namespace: str   # "n8nio", "library" (Docker Hub official images)
    image: str       # "n8n", "couchdb"
    tag: str         # "latest", "1.2.3", "3"

    @property
    def full_name(self) -> str:
        prefix = f"{self.registry}/" if self.registry else ""
        return f"{prefix}{self.namespace}/{self.image}"

    @property
    def hub_repo(self) -> str:
        return f"{self.namespace}/{self.image}"


def parse_image_ref(image: str) -> ImageRef:
    """Parse a Docker image reference into components.

    Examples:
        "n8nio/n8n"                    → registry="", ns="n8nio", img="n8n", tag="latest"
        "couchdb:3"                    → registry="", ns="library", img="couchdb", tag="3"
        "docker.n8n.io/n8nio/n8n"      → registry="docker.n8n.io", ns="n8nio", img="n8n"
        "nickfedor/watchtower:latest"  → registry="", ns="nickfedor", img="watchtower"
    """
    tag = "latest"
    # Extract tag from the last path segment
    last_sep = image.rfind("/")
    last_part = image[last_sep + 1:]
    if ":" in last_part:
        image, tag = image.rsplit(":", 1)

    parts = image.split("/")

    # First segment is a registry if it contains a dot, colon, or is "localhost"
    if len(parts) >= 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry = parts[0]
        rest_parts = parts[1:]
    else:
        registry = ""
        rest_parts = parts

    if len(rest_parts) == 1:
        # Official Docker Hub image (e.g. "couchdb", "nginx")
        namespace = "library"
        img = rest_parts[0]
    else:
        namespace = rest_parts[0]
        img = "/".join(rest_parts[1:])

    return ImageRef(registry=registry, namespace=namespace, image=img, tag=tag)


# ── Semver helpers ────────────────────────────────────────────────────────────

def _parse_semver(tag: str) -> tuple[int, int, int] | None:
    """Parse 'vX.Y.Z', 'X.Y.Z', 'vX.Y', or 'X.Y' → (X, Y, Z). Returns None otherwise.

    Two-part versions (e.g. postgres '16.9', nginx '1.28') are treated as X.Y.0
    so they participate in n-2 grouping even without an explicit patch digit.

    Major values ≥ 10000 are rejected — these are YYYYMMDD-style nightly build
    tags (e.g. jellyfin '20210301.10-unstable') that would otherwise sort above
    all stable releases and corrupt the version ladder.
    """
    m = re.match(r'^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[.\-].*)?$', tag)
    if not m:
        return None
    major = int(m.group(1))
    if major >= 10000:
        return None
    return (major, int(m.group(2)), int(m.group(3) or 0))


_ARCH_RE = re.compile(r'-(arm64|arm|amd64|386|i386|s390x|ppc64le|mips)\b', re.IGNORECASE)
_PLATFORM_RE = re.compile(r'-(windows|ltsc|nanoserver|windowsltsc)', re.IGNORECASE)
_VARIANT_RE = re.compile(
    r'-(enterprise|development|develop|dev|ubuntu|alpine|bookworm|bullseye|buster|trixie|'
    r'focal|jammy|noble|bionic|centos|rhel|ubi)',
    re.IGNORECASE,
)


def _tag_quality(tag: str) -> int:
    """Score a tag's suitability as a pin recommendation. Lower is better.

    0 = plain X.Y.Z or vX.Y.Z — ideal
    1 = OS/edition variant suffix (e.g. -alpine, -enterprise)
    2 = architecture-specific (e.g. -arm64, -amd64)
    3 = Windows/platform-specific (e.g. -windowsltsc2025-amd64)
    """
    if re.match(r'^v?\d+\.\d+(\.\d+)?$', tag):
        return 0
    if _PLATFORM_RE.search(tag):
        return 3
    if _ARCH_RE.search(tag):
        return 2
    if _VARIANT_RE.search(tag):
        return 1
    return 1


# ── Registry API clients ──────────────────────────────────────────────────────

def _dockerhub_jwt(creds: DockerHubCreds, client: httpx.Client) -> str:
    """Exchange a Docker Hub PAT for a short-lived JWT used by the Hub API."""
    r = client.post(
        "https://hub.docker.com/v2/users/login",
        json={"username": creds.username, "password": creds.token},
        headers={"Content-Type": "application/json"},
    )
    if not r.is_success:
        raise RuntimeError(
            f"Docker Hub login failed (HTTP {r.status_code}) — check docker_hub credentials in config.yaml"
        )
    return r.json()["token"]


def prefetch_dockerhub_jwt(creds: DockerHubCreds, timeout: int = 15) -> str:
    """Exchange credentials for a Docker Hub JWT once — reuse across many calls."""
    with httpx.Client(timeout=timeout) as client:
        return _dockerhub_jwt(creds, client)


def fetch_tags_dockerhub(
    namespace: str,
    image: str,
    creds: DockerHubCreds | None = None,
    jwt: str | None = None,
    timeout: int = 15,
) -> list[str]:
    """Return all tag names from Docker Hub for namespace/image.

    Paginates automatically. For official images namespace should be "library".
    Pass jwt (pre-fetched via prefetch_dockerhub_jwt) or creds to authenticate.
    Without auth, Docker Hub rate-limits anonymous callers to ~10 requests before
    returning HTTP 429.
    """
    tags: list[str] = []
    url: str | None = (
        f"https://hub.docker.com/v2/repositories/{namespace}/{image}/tags"
    )
    params: dict = {"page_size": 100, "ordering": "last_updated"}
    headers: dict = {}

    with httpx.Client(timeout=timeout) as client:
        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"
        elif creds:
            headers["Authorization"] = f"Bearer {_dockerhub_jwt(creds, client)}"

        while url:
            r = client.get(url, params=params, headers=headers)
            if r.status_code == 429:
                raise RuntimeError(
                    f"Docker Hub rate limit hit (HTTP 429) for {namespace}/{image}. "
                    f"Add docker_hub credentials to config.yaml to authenticate."
                )
            if not r.is_success:
                raise RuntimeError(
                    f"Docker Hub /tags HTTP {r.status_code} for {namespace}/{image}: {r.text[:200]}"
                )
            data = r.json()
            tags.extend(t["name"] for t in data.get("results", []))
            url = data.get("next")
            params = {}  # next URL already carries pagination params
    return tags


def fetch_tags_registry_v2(registry: str, image: str, timeout: int = 15) -> list[str]:
    """Return all tag names from a Docker Registry v2 endpoint.

    image should be the full path after the registry host, e.g. "n8nio/n8n".
    SSL verification is skipped for internal registries (self-signed certs common).
    """
    url = f"https://{registry}/v2/{image}/tags/list"
    with httpx.Client(timeout=timeout, verify=False) as client:
        r = client.get(url)
        if r.status_code == 401:
            # Try with www-authenticate challenge (anonymous token flow)
            auth_header = r.headers.get("www-authenticate", "")
            token = _fetch_registry_token(auth_header, client)
            if token:
                r = client.get(url, headers={"Authorization": f"Bearer {token}"})
        if not r.is_success:
            raise RuntimeError(
                f"Registry {registry}/v2/{image}/tags/list HTTP {r.status_code}: {r.text[:120]}"
            )
    return r.json().get("tags") or []


def _fetch_registry_token(
    www_authenticate: str,
    client: httpx.Client,
    creds: DockerHubCreds | None = None,
) -> str | None:
    """Attempt token fetch from a Bearer challenge header.

    Pass creds to use basic auth (PAT) for higher rate limits on Docker Hub.
    """
    m = re.search(r'Bearer realm="([^"]+)"(?:,service="([^"]+)")?(?:,scope="([^"]+)")?', www_authenticate)
    if not m:
        return None
    realm, service, scope = m.group(1), m.group(2) or "", m.group(3) or ""
    params: dict = {}
    if service:
        params["service"] = service
    if scope:
        params["scope"] = scope
    try:
        kwargs: dict = {"params": params}
        if creds:
            kwargs["auth"] = (creds.username, creds.token)
        r = client.get(realm, **kwargs)
        return r.json().get("token") or r.json().get("access_token")
    except Exception:
        return None


def fetch_tags_registry_dockerhub(
    namespace: str,
    image: str,
    creds: DockerHubCreds | None = None,
    timeout: int = 15,
) -> list[str]:
    """Fetch tags from Docker Hub via Docker Registry v2 (registry-1.docker.io).

    Uses the same protocol as docker pull — more reliable than the Hub REST API
    because rate limits are higher and auth uses per-image scoped tokens.
    Pass creds (PAT) to authenticate and get higher rate limits.
    """
    repo = f"{namespace}/{image}"
    base = "https://registry-1.docker.io"
    path = f"/v2/{repo}/tags/list"
    auth_headers: dict = {}

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(f"{base}{path}", params={"n": 100})
        if r.status_code == 401:
            token = _fetch_registry_token(
                r.headers.get("www-authenticate", ""), client, creds=creds
            )
            if not token:
                raise RuntimeError(f"Docker Hub auth failed for {repo}")
            auth_headers = {"Authorization": f"Bearer {token}"}
            r = client.get(f"{base}{path}", params={"n": 100}, headers=auth_headers)

        all_tags: list[str] = []
        for _ in range(20):  # cap at 20 pages (2000 tags)
            if not r.is_success:
                raise RuntimeError(
                    f"registry-1.docker.io HTTP {r.status_code} for {repo}: {r.text[:200]}"
                )
            all_tags.extend(r.json().get("tags") or [])
            next_m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("link", ""))
            if not next_m:
                break
            r = client.get(f"{base}{next_m.group(1)}", headers=auth_headers)

    return all_tags


# ── Version analysis ──────────────────────────────────────────────────────────

@dataclass
class VersionInfo:
    ref: ImageRef
    # Sorted newest-first: list of ((major, minor, patch), raw_tag)
    semver_tags: list[tuple[tuple[int, int, int], str]] = field(default_factory=list)
    fetch_error: str = ""

    @property
    def is_floating(self) -> bool:
        """True when the pinned tag is not a specific semver (e.g. 'latest', ':3')."""
        return _parse_semver(self.ref.tag) is None

    @property
    def latest_tag(self) -> str | None:
        return self.semver_tags[0][1] if self.semver_tags else None

    @property
    def _two_part_image(self) -> bool:
        """True when the image uses X.Y (2-part) rather than X.Y.Z versioning.

        Detected by checking whether the majority of parsed semver tags have
        patch=0 (the value assigned to 2-part tags like postgres '16.9').
        When true, n_minus groups by major alone so postgres 18/17/16 are
        treated as three distinct major release lines rather than many minor ones.
        """
        if not self.semver_tags:
            return False
        zero_patch = sum(1 for (_, _, p), _ in self.semver_tags if p == 0)
        return zero_patch / len(self.semver_tags) > 0.7

    def n_minus(self, n: int) -> str | None:
        """Return the best-quality tag for the Nth-previous unique release line.

        n=1 → one line behind latest (n-1)
        n=2 → two lines behind latest (n-2, the recommended safe pin)

        For 3-part images (e.g. n8n 1.2.3): groups by (major, minor) — all 1.2.x
        patches are one line.
        For 2-part images (e.g. postgres 16.9): groups by major alone — all 16.x
        patches are one line, so n-2 from 18 correctly yields 16.
        Within each group, picks the highest patch with the best tag quality
        (plain X.Y.Z preferred over arch/OS/variant-suffixed tags).
        """
        two_part = self._two_part_image

        seen: list[tuple] = []
        best_ver: dict[tuple, tuple[int, int, int]] = {}
        best_tag: dict[tuple, str] = {}

        for (maj, min_, patch), tag in self.semver_tags:
            key: tuple = (maj,) if two_part else (maj, min_)
            ver = (maj, min_, patch)
            if key not in best_ver:
                best_ver[key] = ver
                best_tag[key] = tag
                seen.append(key)
            else:
                prev_ver = best_ver[key]
                if ver > prev_ver or (ver == prev_ver and _tag_quality(tag) < _tag_quality(best_tag[key])):
                    best_ver[key] = ver
                    best_tag[key] = tag

        if len(seen) <= n:
            return None
        return best_tag[seen[n]]

    @property
    def recommended(self) -> str | None:
        return self.n_minus(2)

    def current_position(self) -> str:
        """Describe where the pinned tag sits in the version ladder."""
        if self.is_floating:
            return "floating"
        pinned = _parse_semver(self.ref.tag)
        if not pinned or not self.semver_tags:
            return "unknown"
        two_part = self._two_part_image
        seen_minors: list[tuple] = []
        for (maj, min_, _), _ in self.semver_tags:
            k: tuple = (maj,) if two_part else (maj, min_)
            if k not in seen_minors:
                seen_minors.append(k)
        key: tuple = (pinned[0],) if two_part else (pinned[0], pinned[1])
        if key not in seen_minors:
            return "unlisted"
        idx = seen_minors.index(key)
        if idx == 0:
            return "latest"
        return f"n-{idx}"


# ── Public entry point ────────────────────────────────────────────────────────

def check_image(
    image_ref_str: str,
    docker_hub_creds: DockerHubCreds | None = None,
    docker_hub_jwt: str | None = None,
) -> VersionInfo:
    """Fetch version info for an image reference string.

    Automatically routes to Docker Hub or registry v2 based on the image ref.
    Pass docker_hub_jwt (pre-fetched via prefetch_dockerhub_jwt) for parallel
    calls — avoids one login per image. Falls back to docker_hub_creds if no jwt.
    Returns a VersionInfo with fetch_error set on failure (never raises).
    """
    ref = parse_image_ref(image_ref_str)
    try:
        if ref.registry:
            raw_tags = fetch_tags_registry_v2(ref.registry, f"{ref.namespace}/{ref.image}")
        else:
            raw_tags = fetch_tags_registry_dockerhub(
                ref.namespace, ref.image, creds=docker_hub_creds
            )
    except Exception as exc:
        return VersionInfo(ref=ref, fetch_error=str(exc))

    parsed = []
    for t in raw_tags:
        v = _parse_semver(t)
        if v is not None:
            parsed.append((v, t))
    parsed.sort(reverse=True)

    return VersionInfo(ref=ref, semver_tags=parsed)
