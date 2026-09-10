# atc — Air Traffic Control

Homelab CLI for Proxmox, VMs, LXCs, Home Assistant, and the surrounding
service stack (Portainer, Pi-hole, UniFi, Plex, Seerr, Grafana/Prometheus,
Radarr/Sonarr, etc).

Extracted from `pentagon-management` (2026-09-08) into its own repo since it
had become the load-bearing tool for day-to-day homelab operations.

## Install

1. **Clone the repo** somewhere stable (moving it later means re-linking the
   symlink in step 3):
   ```bash
   git clone git@github.com:damyanmp/atc.git ~/repos/atc
   ```

2. **Install [uv](https://astral.sh/uv/)** if you don't have it — the `atc`
   wrapper script requires it and bootstraps the venv on first run:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

3. **Symlink `atc` onto your PATH**:
   ```bash
   mkdir -p ~/bin
   ln -s ~/repos/atc/atc ~/bin/atc
   # ~/bin must be on PATH — most distros' default .bashrc/.profile add it
   # automatically if the directory exists; otherwise add it yourself:
   #   export PATH="$HOME/bin:$PATH"
   ```

4. **Keyring backend** — secrets (step 5, and `atc secrets set <key>` later)
   are stored in the OS keychain via the `keyring` package. On a desktop with
   GNOME Keyring/KWallet/macOS Keychain this works out of the box. On a
   headless Linux box (no secret service running), install the
   plaintext-file fallback backend and point `keyring` at it:
   ```bash
   uv run --project ~/repos/atc python -c "import keyrings.alt"  # already a dependency
   mkdir -p ~/.config/python_keyring
   cat > ~/.config/python_keyring/keyringrc.cfg <<'EOF'
   [backend]
   default-keyring=keyrings.alt.file.PlaintextKeyring
   EOF
   ```
   Without this, the very first `atc` invocation on a fresh headless machine
   fails with `ModuleNotFoundError: No module named 'keyrings'` or a keyring
   backend error — this step (plus the `keyrings.alt` dependency already
   declared in `pyproject.toml`) is what fixes it.

   **This plaintext fallback is base64-encoded, not encrypted** — anyone who
   can read the file (or a stolen backup/disk copy of it) can decode every
   secret trivially. If that headless box has no OS Secret Service daemon
   available (the common case), upgrade to a real AES-256-GCM encrypted
   backend instead:
   ```bash
   atc secrets migrate-encryption
   ```
   This reads your existing plaintext-keyring secrets (read-only, never
   modifies that file), writes them into a new `keyrings.cryptfile` backend
   protected by a freshly generated master password (`~/.config/atc/keyring.key`,
   `0600`), and verifies every value byte-for-byte before reporting success.
   `atc` auto-detects and switches to the encrypted backend on every run once
   it exists — no further config needed. Safe to re-run. The old plaintext
   file is left in place untouched as a fallback (delete it manually once
   you've confirmed the new backend works, if you want it gone).

   Note: this master-password-in-a-file approach protects against **cold /
   offline theft** of the secrets file alone (stolen backup, copied disk).
   It does **not** add protection against a live process running as your own
   user on that box — that process can read the same password file you can.
   For that threat model you need to restrict who/what can get shell access
   to the box in the first place, not swap the keyring backend.

   Performance note: `keyrings.cryptfile`'s stock Argon2 KDF params
   (time_cost=15, 64MB memory) are tuned for hardening a human-memorable
   password against brute force, and re-derive the encryption key on
   *every individual secret lookup* — a command resolving several config
   secrets could take 8-10s. Since our master password is a 384-bit
   machine-generated random token (not human-chosen), that hardening buys
   nothing, so `atc` uses much cheaper KDF params (~40ms/lookup) instead.
   Commands touching multiple secrets run in ~1-2s, not ~9s.

5. **Create your config**:
   ```bash
   cd ~/repos/atc
   atc init
   ```
   This launches an interactive wizard listing every supported integration —
   pick which ones to configure (comma-separated numbers, `all`, or `q` to
   skip). Secrets you enter are stored in the OS keychain automatically,
   never written to `config.yaml` in plaintext. Safe to re-run any time to
   add or update a section — existing sections are left untouched unless you
   select them again.

   Prefer to hand-edit instead? `cp config.yaml.example config.yaml` and fill
   in values manually — `atc init` and the example file produce identical
   config shapes.

6. **First run** — `atc` builds its `uv` venv automatically on first
   invocation (a few seconds), then runs:
   ```bash
   atc get hosts
   ```
   Config values can reference secrets with the `keychain:<key-name>`
   sentinel instead of plaintext — see `config.yaml.example`.

7. **Optional: PIN-lock the CLI** (prompts once per terminal session):
   ```bash
   atc auth set-pin
   ```
   Skip this if you don't want a PIN gate; `atc` runs unauthenticated by
   default when no PIN is configured.

## Usage

```
atc tui               # interactive TUI
atc get hosts          # node/VM/LXC status
atc acars               # alert digest (Prometheus/HA/etc)
atc secrets             # manage keychain secrets
atc run [script]        # browse and run repo scripts
```

## Updating

```bash
atc upgrade           # check for updates and pull if behind (prompts to confirm)
atc upgrade --check   # only check, don't pull
atc upgrade --yes     # pull without confirmation prompt
```

`atc upgrade` is repo-agnostic: it reads the local checkout's own configured
git remote, so if you've forked this repo it self-updates from *your* fork
automatically — no hardcoded URL, no config needed. Pass `--remote-url` to
check against a different remote than the one currently configured (e.g.
syncing a fork against upstream without changing `origin`).

It refuses to run if the working tree has uncommitted local changes (won't
silently discard edits), and only fast-forwards — if history has diverged
(e.g. you've made local commits), it stops and asks you to resolve that
manually rather than merging or rebasing on your behalf. Dependencies are
re-synced via `uv sync` automatically after a successful pull.

Equivalent to running `git pull && uv sync` by hand in the repo directory —
`atc upgrade` just wraps that with a status check and safety guards.

## Supported integration versions

Each integration targets a specific API generation of that service — see
[`COMPATIBILITY.md`](COMPATIBILITY.md) for the full matrix (e.g. Pi-hole
**v6+ only**, InfluxDB **2.x only**, Radarr/Sonarr **v3 API only**). Running
against an older instance than listed will generally fail outright rather
than degrade gracefully.

## Testing

```bash
uv run pytest                                    # run the test suite
uv run pytest --cov=src --cov-report=term-missing  # with coverage
```

Coverage is currently concentrated in `src/lib/*.py` data models and pure
logic (config parsing, formatters, search-throttle state); the Typer command
tree (`src/cli.py`) and TUI screens (`src/screens/*.py`) are mostly
integration-tested manually against the live homelab rather than unit
tested.

## Other scripts in this repo

A few standalone utilities live at the repo root, separate from the main
`atc` CLI:

- **`mobile_qr.py`** — generates a QR code for importing `atc`'s config into
  the companion Android app. Resolves secrets from the keychain
  automatically. Run with `uv run python mobile_qr.py`.
- **`guest_update_cmds.yaml`** — per-service override commands for
  `atc update`, keyed by guest hostname (e.g. `pihole: pihole updatePihole
  --unattended`). Edit directly; not templated from `config.yaml.example`.

Obsidian vault sync (`sync_vault.py`, `obsidian_decrypt.py`) and the
Mac Keychain→host secret sync helper (`atc-sync-secrets.sh`) live in
`pentagon-management/infrastructure/cli/` — they're homelab-specific
automation, not part of the `atc` CLI itself.
