# Spike: can atc run on Dockhand instead of Portainer?

Date: 2026-11-21 · Dockhand v1.0.58 · Hawser latest (0.2.53) · Docker 29.10 / API 1.57

## Answer

Yes for everything atc does today. `src/lib/dockhand.py` now covers every Portainer
call atc makes (`lib/portainer.py` as used by `cli.py` and `blackbox.py`). It was run
against a live Dockhand on two environments: the local socket and a Hawser
standard-mode agent.

- Raw API contract probe (`contract_probe.py`): **43/43** checks passed.
- atc-shaped library probe (`lib_probe.py`): **31/31** passed. See `RESULTS-lib-probe.md`.
- Unit tests (`tests/test_dockhand_api.py`, using fixtures from the live responses):
  12 new, full suite 445 passed.

The "no API docs" risk in the ADR was wrong. Dockhand serves a full **OpenAPI 3.0.3
spec, 289 paths**, at `/api/docs`, viewable at `/api/docs/ui`. It's off by default;
set `FEAT_API_DOCS=true`. The spec is generated from the source at build time, so it
matches the running version.

## atc feature → Dockhand call

| atc feature | Portainer today | Dockhand | Result |
|---|---|---|---|
| list environments | `GET /api/endpoints` | `GET /api/environments` | PASS |
| `docker ls` | `/endpoints/{id}/docker/containers/json` | `GET /api/containers?env=&all=true` | PASS (same `status`/`state`/`labels`) |
| `docker logs -n --since` | Docker logs, framed stream | `GET /api/containers/{id}/logs?tail=&since=` → `{"logs": text}` | PASS (no demux needed) |
| start/stop/restart container | `.../containers/{id}/{action}` | `POST /api/containers/{id}/{action}?env=` | PASS |
| `docker stacks` | `GET /api/stacks` | `GET /api/stacks?env=` per env | PASS (also lists adopted + Git stacks) |
| stack start/stop | `POST /api/stacks/{id}/{action}` | `POST /api/stacks/{name}/{action}?env=` | PASS |
| `compose up` (new) | `POST /api/stacks/create/standalone/string` | `POST /api/stacks` `{name, compose, envVars, start}` | PASS (2 networks) |
| `compose up` (existing) | `PUT /api/stacks/{id}` | `PUT /env/raw` then `PUT /compose {restart:true}` | PASS (container saw new env) |
| `compose down` | `DELETE /api/stacks/{id}` | `DELETE /api/stacks/{name}?force&files` | PASS |
| `docker images` / `volumes` | Docker proxy | `GET /api/images`, `GET /api/volumes` | PASS |
| `docker prune` images | Docker proxy | `POST /api/prune/images?dangling=` | PASS |
| `docker prune` volumes | Docker proxy | `POST /api/prune/volumes` | endpoint present, **not fired** (see below) |
| `quick publish` | `PUT .../archive` (tar) | `/files/create` + multipart `/files/upload` | PASS (nested dirs) |
| `blackbox` outages | Docker `/events?since=` | `GET /api/activity?actions=&fromDate=` | PASS (fed into the unchanged `outages_from_events`) |
| read-only exec | Docker exec create + start | `POST /api/containers/{id}/exec/run` → `{stdout, stderr, exitCode}` | PASS (simpler) |

## Behaviour differences callers must handle

1. **Failures come back as HTTP 200.** A sync deploy that fails (bad YAML, pull error)
   returns `200 {"success": false, "error": ...}`. Portainer used 4xx/5xx.
   `_check()` now raises on `success: false`. The probe confirms a broken compose
   raises.
2. **Stacks are keyed by name + env**, not a numeric id. `cli.py` call sites that pass
   `stack.id` need to pass `env_id, name`.
3. **Stack env lives in two places.** The stack's `.env` file holds non-secret vars and
   the DB holds secrets. `PUT /api/stacks/{name}/env` (JSON) only writes the DB, so a
   running internal stack never sees the change. The first probe run caught this.
   `update_stack` writes `/env/raw` instead. With `env_vars=None` the env is left alone,
   so the Portainer env-wipe incident (2026-09-02) can't happen. The `env_vars=[]`
   guard is kept.
4. **Long operations need `Accept: application/json`**, or they stream SSE or return a
   job id for the UI. `_headers(sync=True)` handles this.
5. **Events come from Dockhand's own activity log**, not the daemon's in-memory buffer.
   - Better: the history survives daemon restarts.
   - Worse: it only covers time since Dockhand started recording (`collectActivity`).
6. **On Hawser environments, stack files live on the agent host** (`/data/stacks/<name>`
   inside the agent), not on the Dockhand server. Stack files stay on the host that
   runs them, unlike Portainer, which keeps them centrally.

## Not verified

- **Prune volumes:** the response shape is assumed to mirror prune images. It wasn't
  fired, because it deletes every unused volume on the daemon, and this sandbox shares
  a daemon with real (orphaned, unknown) volumes on the host.
- **Old Synology engines:** nothing was run against an old Synology engine
  (Docker 20.10 / API 1.41, or 24.0 / API 1.43). That needs Hawser on the NAS. It's the next spike
  step, read-only first.
- **Edge-mode Hawser:** only standard mode was exercised.
- **cli.py is not switched over.** This spike proves the library layer. The backend
  switch (`DockerBackend` interface, `docker.backend: portainer|dockhand` in config) is
  separate work.

## Gotchas found while running it

- Fresh Dockhand has **auth off**. The sandbox binds to `127.0.0.1` and `bootstrap.py`
  turns auth on before anything else. After that, unauthenticated calls return 401.
- Creating an API token needs a **session plus the user's password**
  (`POST /api/auth/tokens {name, password}`). A bearer token can't mint tokens.
- `env: [A=${A}]` (flow style with `${}`) is invalid YAML for go-yaml. Use block-style
  lists.

## `atc init dockhand` (setup wizard)

- Writes the `dockhand:` section. The token goes to the keychain as
  `dockhand-api-token`, the same key as `config.yaml.example`.
- Checks the token with an authenticated call. `/api/health` alone would pass with a
  bad token.
- Offers to register Docker hosts in Dockhand: Hawser standard, Hawser edge, the local
  socket, or direct TCP.
  - Standard mode: generates the agent token and prints the `docker run` line.
  - Edge mode: mints the token in Dockhand and waits for the agent to dial in.
  - Every config is tested before it's saved (`POST /api/environments/test`). On a
    failure you can retry, save anyway, or skip. Duplicate names are refused.
- Re-running `atc init dockhand` once it's configured offers "add hosts" without
  re-entering the token.
- Agent tokens live in Dockhand only; atc never stores them.
- End-to-end run (all 4 host types, a failing host, a re-run, duplicate-name refusal):
  `RESULTS-init-wizard.md`.

## Files

- `sandbox.sh up|down`: Dockhand + Hawser on loopback (`HW_TOKEN_FILE` = 0600 env file
  with `TOKEN=`).
- `bootstrap.py`: admin user, auth on, API token, two environments. Secrets go to
  0600 files in `$SPIKE_DIR` and are never printed.
- `contract_probe.py`: raw API probe; JSON results on stdout.
- `lib_probe.py`: drives `lib/dockhand.py` the way `atc docker …` drives Portainer;
  markdown table on stdout.
- `shapes.py`: dumps the raw response shapes used for the fixtures.
