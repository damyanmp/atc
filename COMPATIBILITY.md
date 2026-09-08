# Integration compatibility

`atc` talks to each service's REST/API surface directly (no SDKs). API shapes
change between major versions of these tools, so `atc` targets specific
generations. Running against an older instance than listed will generally
fail outright (wrong endpoint/auth shape) rather than degrade gracefully —
this CLI does not attempt to support both old and new APIs of the same
service at once.

| Service | Required version | Why | Notes |
|---|---|---|---|
| **Pi-hole** | **v6.0+** | Uses the v6 REST API (`POST /api/auth` → session ID → `X-FTL-SID` header; `/api/dns/blocking`, `/api/stats/summary`). Pi-hole v5's API (`?auth=<token>` query param, different endpoint paths) is a completely different shape and is **not supported**. | `src/lib/pihole.py`. Multi-instance sync (primary/replica) assumes all instances are on the same major version. |
| **UniFi Network** | **8.x+ recommended** (API key auth); **any version** via username/password fallback | API-key auth (`Settings → Control Plane → Integrations → Create API Key`) was introduced in Network Application 8.x. A separate "Integration API" (zone-based firewall, site UUIDs) is also 8.x+-only and used for firewall/zone queries. | `src/lib/unifi.py`. Set `udm: true` for UDM/UDR/Cloud Key (adds `/proxy/network/` prefix) vs `udm: false` for a standalone controller. Legacy username/password auth works on essentially any version but exposes fewer endpoints. |
| **Home Assistant** | **2023.6+** | Long-lived access token auth (`/api/states`, `/api/services/<domain>/<service>`) is stable across many versions, but log retrieval explicitly requires 2023.6+ — the old `/api/error_log` endpoint was removed then and `atc` doesn't fall back to it. | `src/lib/ha.py`. |
| **Proxmox VE** | **7.0+** (8.x tested) | Uses `proxmoxer` against the cluster resources API. Scheduled-backup trigger (`atc backup snapshot`) requires PVE 7+'s job-trigger endpoint. | `src/lib/proxmox.py`. API token auth (`user@realm!tokenid=secret`), not password. |
| **Portainer** | **CE or EE, 2.x** (Bearer-token API) | Uses Access Tokens (`Account → Access tokens`), not the older JWT-login flow. Stack/env/container endpoints assumed stable across recent 2.x releases; not tested against 1.x. | `src/lib/portainer.py`. |
| **Radarr / Sonarr** | **v3 API** (`/api/v3/...`) | Both are "Servarr" apps sharing an identical API shape at v3. v2 API (older Radarr/Sonarr releases) is a different path and **not supported**. | `src/lib/arr.py`. Auth via `X-Api-Key` header. |
| **Overseerr / Jellyseerr** | Any version exposing **API v1** (`/api/v1/...`) | Both Overseerr and its Jellyseerr fork implement the same v1 API. | `src/lib/seerr.py`. Auth via `X-Api-Key` header. |
| **Plex Media Server** | Any recent version (uses stable `X-Plex-Token` auth) | No hard version floor identified — the endpoints used (`/status/sessions`, `/library/...`) have been stable for years. | `src/lib/plex.py`. |
| **Tautulli** | Any version with the v2 JSON API (`/api/v2?apikey=...&cmd=...`) — effectively all current releases | Session termination prefers `session_id` over `session_key` (Tautulli's `session_key` lookup depends on its internal activity cache, which isn't always populated). | `src/lib/tautulli.py`. |
| **Nginx Proxy Manager** | Any current release (uses `POST /api/tokens` email+password auth, undocumented-but-stable REST API) | No official API docs exist; behavior confirmed against a live instance. Tokens expire in 24h with no refresh — `atc` re-authenticates per invocation, so this doesn't matter operationally. | `src/lib/nginx_proxy_manager.py`. |
| **Uptime Kuma** | Any version exposing `/metrics` (Prometheus endpoint, needs an API key) **or** a public status page | Falls back to the public status-page heartbeat API if no `api_key` is configured — works on essentially any Uptime Kuma release, but with less detail. | `src/lib/uptimekuma.py`. |
| **Beszel** | Any version (built on PocketBase, Bearer-token auth) | Reads system metrics via `/api/collections/systems/records`. No known version floor. | `src/lib/beszel.py`. |
| **Prometheus** | Any version with the standard HTTP query API (`/api/v1/query`, `/api/v1/query_range`) | | `src/lib/prometheus.py`. |
| **Loki** | Any version with the standard query API | | `src/lib/loki.py`. |
| **InfluxDB** | **2.x** (Flux query language) | InfluxDB 1.x uses InfluxQL, a different query language entirely — **not supported**. | `src/lib/influxdb.py`. |
| **Docker registries** (Docker Hub, ghcr.io, self-hosted) | Docker Registry **v2** HTTP API (or Docker Hub's own `hub.docker.com` API) | Used for `atc versions` (n-2 image tag tracking). | `src/lib/registry.py`. |
| **OpenSpeedTest** | Any version (`openspeedtest/latest` image) exposing the standard ping/download/upload endpoints | | `src/lib/speedtest.py`. |

## General notes

- All HTTP clients use `httpx`; TLS verification is configurable per-service
  via each block's `verify_ssl: false` (default assumes self-signed certs on
  a local homelab).
- SSH-based operations (`src/lib/ssh.py`, node/guest updates) use `asyncssh`
  and assume a standard OpenSSH server — no version dependency there.
- This matrix reflects what the current code path talks to, not what's been
  exhaustively tested against every point release. If a service update
  breaks a previously-working `atc` command, check that service's changelog
  for API/auth changes first — see the individual `src/lib/*.py` module
  docstrings for the exact endpoints and auth flow each integration uses.
