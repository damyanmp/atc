# CLAUDE.md — dev guide for this repo

`atc` is a homelab CLI (Proxmox/Docker/media/HA/etc). This file is
for anyone (human or agent) checking the repo out to make code changes —
not end-user docs (see README.md for that).

## Project layout

```
atc                     Bash entry point — resolves config, execs `uv run src/main.py`
src/main.py             Entry point — routes to CLI (typer) or TUI (Textual)
src/cli.py              All typer CLI commands (~8k lines, one big file by design)
src/config.py           config.yaml loader → typed dataclasses, keychain secret resolution
src/auth.py             Keyring/secrets backend plumbing
src/lib/                Per-service clients (proxmox, pihole, unifi, plex, radarr, …)
src/lib/config_sections.py   Declarative registry of config.yaml sections, used by `atc init`
src/screens/            Textual TUI screens (`atc radar`)
tests/                  pytest, mirrors src/lib/*.py mostly 1:1
.github/workflows/ci.yml   Push/PR CI: ruff (advisory), mypy (advisory), pytest (gating)
```

Within `src/cli.py`, commands are grouped by comment-delimited sections
(`# ── pihole ───` etc.) — grep for the section header, not line numbers,
they drift.

## Setup

```bash
uv sync --all-groups      # installs runtime + dev deps (pytest, ruff, mypy, textual-dev)
```

No config.yaml is needed to run tests or lint — only to actually invoke `atc`
against real infrastructure. If you need a working config to manually test
against, copy `config.yaml.example` and fill in whatever section you're
touching; see "Where config.yaml actually lives" below.

## Running checks (same commands CI runs)

```bash
uv run pytest                              # gating — must pass
uv run pytest --cov=src --cov-report=term-missing   # with coverage, like CI
uv run ruff check src tests                # advisory only — pre-existing findings untriaged (see pyproject.toml comment)
uv run mypy src                            # advisory only — codebase isn't fully typed yet
```

Only `pytest` blocks CI; ruff/mypy are `continue-on-error` until those passes
are done deliberately. Don't treat a ruff/mypy failure as a merge blocker,
but don't add *new* ruff findings either — check the diff is clean on rules
in `pyproject.toml`'s `[tool.ruff.lint] select`.

One known pre-existing test failure, unrelated to config-path work:
`tests/test_config.py::TestLoadConfig::test_scripts_dir_default` expects the
old `"../../"` default from before the 2026-09-08 repo split; not yet fixed.

`pyproject.toml` sets `pythonpath = ["src", "src/lib"]` for pytest, so tests
import modules the same bare way runtime code does (`from config import …`,
`from lib.pihole import …`) — don't add `sys.path` hacks in test files.

## Where config.yaml actually lives (don't hardcode repo-root paths)

`config.yaml` resolves via `config.default_config_path()`:
`$ATC_CONFIG` → `~/.config/atc/config.yaml` → legacy repo-root `config.yaml`
(back-compat only). **Never add new code that hardcodes
`Path(__file__).parent.parent / "config.yaml"`** — always go through
`default_config_path()` / `load_config()`. The repo-root fallback exists so
old installs don't break, not as a place to put real data — `config.yaml`
is gitignored but that's not a trust boundary; the recommended location is
outside the git working tree entirely. Same rule for the `guest_users.yaml`
sidecar (resolved as `config_path.parent / "guest_users.yaml"` — it follows
`config.yaml`'s location automatically, don't special-case it).

## Hard rule: no real infrastructure data in code or commits

This bit the repo once already (see CHANGELOG/git history) — a prior
feature (`tls_deploy.py`, since removed) hardcoded real LAN IPs, SSH
usernames, and filesystem paths directly as Python constants instead of
reading them from `config.yaml`. When adding a feature that needs
host/user/path/credential info:

- It belongs in `config.yaml` (add a dataclass in `config.py` + a loader
  block + wire it into `Config`), **not** as a module-level constant.
- Secrets go through the `keychain:<key-name>` sentinel +
  `_resolve_secret()`, never as literal strings anywhere, including tests
  (use obviously-fake values like `"k"` or `"plaintoken"` — see
  `tests/test_config.py` for the existing pattern).
- If a feature only makes sense against one specific infra setup and isn't
  generically configurable, it probably doesn't belong in this repo at all
  — it belongs in his private `pentagon-management` repo as a standalone
  script, reachable generically via `atc run` (`scripts_dir` is user-set,
  see `atc run set-dir` / `atc run show-dir`).
- Before committing, grep your diff for `192.168.`, `/home/`, and any
  hardcoded username — these are the three things that have leaked before.

## Commit identity

Global git config on this machine uses `git@localhost` as the commit
email (not his real address) — if you're setting up a fresh clone/checkout,
match that rather than letting git pick up a different default identity.

## Testing changes that touch `atc` itself end-to-end

`uv run` can hang in some sandboxed/headless environments when `keyring`
tries to initialize a secret-service backend — if a CLI invocation during
manual testing hangs with no output, that's the likely cause, not your
change. Prefer running the project's own venv python directly
(`.venv/bin/python -m pytest` / `.venv/bin/python src/main.py <args>`) with
`PYTHONPATH` unset in the environment, which avoids both that hang and a
separate issue where a leaked Hermes-session `PYTHONPATH` makes `atc` pick
the wrong keyring backend silently.
