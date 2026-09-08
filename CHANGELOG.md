# Changelog

All notable changes to `atc` are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning is not strict
semver (this is a personal tool, not a published library) but bumps roughly
track breaking config/CLI changes.

## [Unreleased]

### Added
- TUI screens for Docker/Portainer, UniFi, Prometheus metrics, Proxmox
  backups, and Quick sites — closes most of the CLI/TUI parity gap
  (`versions`, and the admin-only `unifi`/`npm`/`ha`/`certs`/`acars`/
  `secrets`/`auth` commands, remain CLI-only by design).

## [0.1.0] - 2026-09-08

Initial standalone release — extracted from `pentagon-management`
(`infrastructure/cli/`) into its own repo with full git history preserved
via `git-filter-repo`.

### Added
- `COMPATIBILITY.md` — required API/version matrix per integration (e.g.
  Pi-hole v6+ only, InfluxDB 2.x only, Radarr/Sonarr v3 API only).
- Full install instructions in `README.md` (uv, PATH, keyring backend
  bootstrap, optional PIN auth).
- `keyrings.alt` as a declared dependency — fixes a previously-silent gap
  where headless/no-secret-service Linux boxes couldn't use `atc secrets`
  at all (`ModuleNotFoundError: No module named 'keyrings'`).
- Test coverage for `lib/registry.py` (n-2 version-pin logic) and
  `lib/portainer.py` (container state/health classification) — previously
  untested at 0%.
- CI (GitHub Actions): ruff lint, mypy (advisory), pytest with coverage on
  every push/PR.
- `ruff` linting config (minimal ruleset: `E`, `F`, `UP`, `B` — not the full
  opinionated default, to avoid drowning real signal in a codebase that was
  never linted before) and `mypy` config (advisory only, not yet gating).

### Fixed
- `tests/test_config.py` — stale import of `DSMConfig`, which was replaced
  by `BeszelConfig` in an earlier (pre-extraction) commit; test suite
  couldn't even be collected until this was fixed.
- `tests/test_tautulli.py` — `TautulliStream` fixture helpers were missing
  the (now-required) `session_id` field, causing 7 test failures.
- Two genuinely undefined names in `cli.py` (`NodeStatus`, `PlexMatch` used
  in forward-ref type annotations but never imported) — harmless at runtime
  (quoted annotations aren't evaluated) but real gaps caught by `ruff`/type
  checking.
- Untracked a large (~700KB) diagnostic dump file (`blackbox-*.txt`) that
  had been committed to the old repo; added to `.gitignore`.

### Changed
- Repo structure flattened: `infrastructure/cli/*` is now repo root.
