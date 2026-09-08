"""Nginx Proxy Manager (NPM) REST API client.

NPM has no long-lived API-key concept: the only way to get a token is
POST /api/tokens with an admin email+password, and the resulting JWT
expires in 24h with no option to extend it (there's no valid 'expiry'
field on that endpoint despite some third-party docs suggesting otherwise
— confirmed by testing against this instance). So this module always logs
in with email+password (keychain-backed, see config.yaml
`nginx_proxy_manager:` block), caches the derived token in-process only for
the life of one CLI invocation, and transparently re-logs-in on any 401.

Two config shapes are technically supported (see NginxProxyManagerConfig):
  - `email` + `password`  — the only practical path (see above).
  - `token`                — supported in code for a pre-issued JWT, but
    not recommended: it expires in 24h with no refresh path, so anything
    relying on it silently breaks once a day. Left in for completeness /
    manual short-lived testing, not for config.yaml's default setup.

API reference (unofficial but stable): POST /api/tokens for auth,
GET/POST/PUT/DELETE /api/nginx/proxy-hosts for CRUD. Community Postman docs:
https://documenter.getpostman.com/view/4475423/2s93mBvyLZ
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from config import NginxProxyManagerConfig

# ── Auth ──────────────────────────────────────────────────────────────────────

_token_cache: dict[str, str] = {}  # keyed by base_url, process-lifetime only


def _client(cfg: NginxProxyManagerConfig, timeout: float = 15) -> httpx.Client:
    return httpx.Client(verify=cfg.verify_ssl, timeout=timeout)


def _get_token(cfg: NginxProxyManagerConfig) -> str:
    """Return a usable JWT.

    If cfg.token is set (a pre-issued long-lived token), use it directly —
    no login call, no password needed at all. Otherwise fall back to
    email+password login, caching the derived token in-process only.
    """
    if cfg.token:
        return cfg.token

    cached = _token_cache.get(cfg.base_url)
    if cached:
        return cached
    with _client(cfg) as client:
        r = client.post(
            f"{cfg.base_url}/api/tokens",
            json={"identity": cfg.email, "secret": cfg.password},
        )
        if not r.is_success:
            raise RuntimeError(f"NPM login HTTP {r.status_code}: {r.text[:200]}")
        token = r.json().get("token", "")
        if not token:
            raise RuntimeError("NPM login succeeded but no token in response")
        _token_cache[cfg.base_url] = token
        return token


def _headers(cfg: NginxProxyManagerConfig) -> dict:
    return {"Authorization": f"Bearer {_get_token(cfg)}"}


def _request(cfg: NginxProxyManagerConfig, method: str, path: str, **kwargs):
    """Issue one API call, retrying once on 401 with a fresh token.

    If cfg.token is set (pre-issued long-lived token), a 401 means that
    token has actually expired or been revoked — there's nothing to refresh
    to, so this raises immediately with a clear message instead of retrying.
    """
    with _client(cfg, timeout=kwargs.pop("timeout", 15)) as client:
        r = client.request(method, f"{cfg.base_url}{path}", headers=_headers(cfg), **kwargs)
        if r.status_code == 401:
            if cfg.token:
                raise RuntimeError(
                    "NPM token rejected (401) — it has likely expired or been revoked. "
                    "Mint a new one (see module docstring) and update the "
                    "'npm-api-token' keychain entry."
                )
            _token_cache.pop(cfg.base_url, None)
            r = client.request(method, f"{cfg.base_url}{path}", headers=_headers(cfg), **kwargs)
        if not r.is_success:
            raise RuntimeError(f"NPM {method} {path} HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else None



# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ProxyHost:
    id: int
    domain_names: list[str]
    forward_host: str
    forward_port: int
    forward_scheme: str      # "http" or "https"
    enabled: bool
    ssl_forced: bool
    certificate_id: int      # 0 = no cert attached
    block_exploits: bool
    caching_enabled: bool
    allow_websocket_upgrade: bool
    advanced_config: str
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def primary_domain(self) -> str:
        return self.domain_names[0] if self.domain_names else ""


def _build_proxy_host(h: dict) -> ProxyHost:
    return ProxyHost(
        id=h.get("id", 0),
        domain_names=h.get("domain_names", []),
        forward_host=h.get("forward_host", ""),
        forward_port=h.get("forward_port", 0),
        forward_scheme=h.get("forward_scheme", "http"),
        enabled=bool(h.get("enabled", 1)),
        ssl_forced=bool(h.get("ssl_forced", 0)),
        certificate_id=h.get("certificate_id", 0) or 0,
        block_exploits=bool(h.get("block_exploits", 0)),
        caching_enabled=bool(h.get("caching_enabled", 0)),
        allow_websocket_upgrade=bool(h.get("allow_websocket_upgrade", 0)),
        advanced_config=h.get("advanced_config", ""),
        raw=h,
    )


# ── Proxy hosts ───────────────────────────────────────────────────────────────

def list_proxy_hosts(cfg: NginxProxyManagerConfig) -> list[ProxyHost]:
    """Return all configured proxy hosts."""
    data = _request(cfg, "GET", "/api/nginx/proxy-hosts")
    return [_build_proxy_host(h) for h in (data or [])]


def find_proxy_host(cfg: NginxProxyManagerConfig, domain: str) -> ProxyHost | None:
    """Find an existing proxy host by exact domain match, or None."""
    for h in list_proxy_hosts(cfg):
        if domain in h.domain_names:
            return h
    return None


def create_proxy_host(
    cfg: NginxProxyManagerConfig,
    domain: str,
    forward_host: str,
    forward_port: int,
    forward_scheme: str = "http",
    block_exploits: bool = True,
    allow_websocket_upgrade: bool = True,
    caching_enabled: bool = False,
    certificate_id: int = 0,
    ssl_forced: bool = False,
    advanced_config: str = "",
) -> ProxyHost:
    """Create a new proxy host. Returns the created ProxyHost.

    certificate_id=0 means no SSL cert attached (plain HTTP). Pass an
    existing certificate_id (see list_certificates()) or 'new' is NOT
    supported here — cert issuance is a separate, slower call
    (request_certificate()) that should be done explicitly, not implicitly
    bundled into every host creation.
    """
    body = {
        "domain_names": [domain],
        "forward_host": forward_host,
        "forward_port": forward_port,
        "forward_scheme": forward_scheme,
        "block_exploits": block_exploits,
        "allow_websocket_upgrade": allow_websocket_upgrade,
        "caching_enabled": caching_enabled,
        "certificate_id": certificate_id,
        "ssl_forced": ssl_forced,
        "hsts_enabled": False,
        "http2_support": bool(certificate_id),
        "advanced_config": advanced_config,
        "meta": {},
        "locations": [],
    }
    data = _request(cfg, "POST", "/api/nginx/proxy-hosts", json=body)
    return _build_proxy_host(data or {})


def update_proxy_host(cfg: NginxProxyManagerConfig, host_id: int, updated_fields: dict) -> ProxyHost:
    """Fetch the current host, merge updated_fields, PUT the full object back.

    NPM's API expects the full proxy-host object on PUT, not a partial patch.
    """
    current: dict = _request(cfg, "GET", f"/api/nginx/proxy-hosts/{host_id}") or {}
    current.pop("id", None)
    current.pop("created_on", None)
    current.pop("modified_on", None)
    current.pop("owner_user_id", None)
    current.pop("meta", None)
    current.update(updated_fields)
    data = _request(cfg, "PUT", f"/api/nginx/proxy-hosts/{host_id}", json=current)
    return _build_proxy_host(data or {})


def delete_proxy_host(cfg: NginxProxyManagerConfig, host_id: int) -> None:
    """Delete a proxy host by id."""
    _request(cfg, "DELETE", f"/api/nginx/proxy-hosts/{host_id}")


def enable_proxy_host(cfg: NginxProxyManagerConfig, host_id: int) -> None:
    _request(cfg, "POST", f"/api/nginx/proxy-hosts/{host_id}/enable")


def disable_proxy_host(cfg: NginxProxyManagerConfig, host_id: int) -> None:
    _request(cfg, "POST", f"/api/nginx/proxy-hosts/{host_id}/disable")


# ── Certificates ──────────────────────────────────────────────────────────────

@dataclass
class Certificate:
    id: int
    nice_name: str
    domain_names: list[str]
    provider: str          # "letsencrypt" or "other"
    expires_on: str        # ISO 8601, empty for "other" provider certs
    raw: dict = field(default_factory=dict, repr=False)


def list_certificates(cfg: NginxProxyManagerConfig) -> list[Certificate]:
    """Return all SSL certificates known to NPM."""
    data = _request(cfg, "GET", "/api/nginx/certificates")
    return [
        Certificate(
            id=c.get("id", 0),
            nice_name=c.get("nice_name", ""),
            domain_names=c.get("domain_names", []),
            provider=c.get("provider", ""),
            expires_on=c.get("expires_on", ""),
            raw=c,
        )
        for c in (data or [])
    ]
