# Changelog

All notable changes to `atc` are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning is not strict
semver (this is a personal tool, not a published library) but bumps roughly
track breaking config/CLI changes.

## [Unreleased]

### Security
- Env files (`.env`, `.env.*`, `*.env`) are gitignored, and a tracked `.githooks/pre-commit` runs gitleaks on staged changes and blocks commits that contain secrets. Enable once per clone with `git config core.hooksPath .githooks` (needs `gitleaks` on PATH).

## [0.3.0] - 2026-10-24

### Breaking
- **The mobile QR wire format changed.** `mobile_qr` payloads are now
  passphrase-encrypted (`ATCQR1:` + base64; scrypt-derived AES-256-GCM, with
  a fresh salt and nonce on every run). A scanner that reads the old plaintext
  YAML QR has to be updated; the ATCQR1 format is documented in the repo.
- **SSH host-key verification is now on by default.** `verify_host_keys`
  defaults to `true`. A config that never set it used to connect with
  `StrictHostKeyChecking=no`. Set it to `false` explicitly only when you
  mean it.

### Security
- The mobile QR config no longer embeds plaintext Proxmox/Plex/Tautulli/*arr/
  Seerr/Portainer/Beszel credentials, either in the terminal QR or in a
  `--save` PNG. You are prompted for the passphrase (min 8 chars, typed twice);
  it is never written to disk or passed as an argument.
- The UniFi write helpers (`delete_port_forward`, `update_port_forward`,
  `update_firewall_zone_v1`, `update_firewall_policy_v1`,
  `delete_firewall_policy_v1`, `create_firewall_policy_v1`) are now
  **dry-run by default**. They return a `UniFiWritePlan` (method, URL, body)
  and send nothing unless called with `dry_run=False` or via
  `execute_write_plan()`. A new `cli._unifi_apply()` guard is in place for
  future write commands: it prints the plan, honours `--dry-run`, and sends
  only with `-y/--yes` or an interactive confirm.

### Added
- `atc acars` shows Pi-hole FTL's own diagnostic messages
  (`/api/info/messages`: rate limits, certificate mismatch, gravity/list
  failures, disk, NTP and more) for every configured Pi-hole instance.

### Fixed
- `atc upgrade` now recovers when the remote history was rewritten (a
  force-push after a PII scrub). It used to fail on `--ff-only` every time.
- `atc quick list` and the TUI now parse quick-site's new card-based directory
  page, keeping the old format as a fallback. They used to report
  "nothing published".


### Media overhaul

**Fixed**
- Seerr request status `5` (completed) was unmapped and showed as "pending";
  media statuses `6`/`7` (blocklisted/deleted) showed as "unknown".
- `media seerr list` only read the newest 100 requests before filtering —
  it now pages through all of them, and looks up titles only for rows shown.
  The default view hides available, completed and declined requests.
- `media status` reported Seerr as "online" with no version (reads `/api/v1/status` now).
- `seerr request` refuses duplicates (already available / processing / in an
  active request; TV checks per season) unless `--force`.
- Queue reasons were often blank (`statusMessages[0].messages` empty) — now
  falls back to the status title; queue fetch pages past 100 records.
- Pipeline "Download" stage said "Imported" for movies whose file was since
  deleted; stuck queue items now show as stuck with a suggested fix.

**Added**
- `atc media queue [--stuck] [-o json]` — Radarr + Sonarr queue, one row per
  download (season packs collapsed), stuck detection + fix hints.
- `atc media search <title> | --tmdb ID --type T [--season N]` — force one
  indexer search when Seerr didn't trigger one.
- `media_search:` config section (`enabled`, `max_items`, `min_healthy_pct`)
  replacing the hardcoded `ENABLE_ARR_SEARCH` constant. Every search path
  (search, search-missing, search-today, pipeline menu) checks it, plus an
  indexer-health gate (from the *arr's own health checks), per-item cooldowns,
  and skip-if-queued. `--force` overrides once. Exit 3 = refused by a guard rail.
- `acars` "Media" source: stuck queue items, *arr health errors, indexers
  mostly failing, failed Seerr requests (all WARN).
- `-o json` on `pipeline`, `calendar`, `status`, `today`, `queue`, `search`,
  `seerr request`; `poster_url` in `seerr search -o json`.
- `pipeline`/`calendar`/`search` never prompt without a TTY: ambiguous titles
  print candidates and exit 2 (`--pick N` or `--tmdb ID --type T`).
- `seerr request --seasons 1,3` / `2-4`.

**Changed / removed**
- `radarr|sonarr search-missing` no longer fire the broad
  MissingMoviesSearch/MissingEpisodeSearch (whole backlog × every indexer).
  They search the newest `--limit N` items (default `media_search.max_items`),
  skipping queued/cooled-down ones, so repeated runs roll through the backlog.
  Radarr skips titles that aren't released yet.
- Removed `media radarr status` / `media sonarr status` (use `media status --radarr|--sonarr`).
- `media status` exits 1 when a configured service is down.
- `search-today` matches the queue per episode, not per series.
- Merged the two near-identical pipeline implementations (search vs calendar).

### Added
- `atc acars -o json` — machine-readable alert digest for scripts and
  monitoring wrappers: `{summary: {total, crit, warn, sources_checked},
  alerts: [{severity, source, message}], source_errors: [...]}`. No spinner
  or table in JSON mode; exit code is still 1 when any CRIT is present.

## [0.2.0] - 2026-10-15

### Added
- `atc init` now opens with a p10k-`configure`-style keyring-hardening step:
  if the plaintext (base64) keyring fallback is active and has real secrets
  in it, it explains the risk and the upgrade path in plain language, asks
  one yes/no question, and runs `secrets migrate-encryption` on confirm.
  Already encrypted or nothing to migrate? A single confirmation line, no
  interruption. `config.keyring_backend_status()` is the reusable detection
  helper behind it (used standalone too, e.g. future `atc secrets status`).
- `atc secrets migrate-encryption` — upgrades the headless-Linux plaintext
  (base64, not encrypted) keyring fallback to a real AES-256-GCM encrypted
  backend (`keyrings.cryptfile`), protected by a freshly generated
  0600-permissioned master password file. Read-only against the old
  plaintext backend (never modified/deleted), verifies every migrated value
  byte-for-byte, safe to re-run. `atc` auto-detects and switches to the
  encrypted backend on every subsequent run once it exists, with no config
  changes needed and no impact on macOS/GNOME-Keyring/KWallet setups where
  a real OS keychain is already in use. Uses Argon2 KDF params tuned for a
  machine-generated random master password rather than the library's
  human-password-oriented defaults (time_cost=2, 19MB memory vs the
  library's time_cost=15, 64MB) — the stock defaults re-derive the key on
  every individual secret lookup and made multi-secret commands (`atc
  pihole status`, `atc acars`) take 8-10s; tuned params keep them at ~1-2s.
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
