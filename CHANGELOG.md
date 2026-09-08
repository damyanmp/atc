# Changelog

All notable changes to `atc` are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning is not strict
semver (this is a personal tool, not a published library) but bumps roughly
track breaking config/CLI changes.

## [Unreleased]

### Added
- `atc init` — interactive setup wizard. Lists all 20 supported config
  sections with a checkmark for what's already configured; pick sections by
  number, `all`, or quit. Secrets are stored in the OS keychain automatically
  (never written to config.yaml in plaintext). Safe to re-run — untouched
  sections are preserved, only selected sections are replaced. Also supports
  `atc init <section>` for a non-interactive single-section add/update.
  Backed by a declarative registry (`lib/config_sections.py`) with 23 unit
  tests, and end-to-end verified against real scratch config files covering
  scalar sections, secret fields, list sections (multi-instance Pi-hole),
  the interactive menu, and merge-preserves-existing-sections behavior.
- `atc upgrade` — self-update command. Repo-agnostic (reads the checkout's
  own git remote, so forks self-update from themselves automatically),
  refuses to run on a dirty working tree, fast-forward-only pulls, and
  re-syncs dependencies via `uv sync` afterward. `--check`/`--yes`/
  `--remote-url` flags. Verified end-to-end against real scratch git repos
  (check/pull/dirty-refusal/fork-remote scenarios), not just unit-mocked.
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
