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

4. **Create your config**:
   ```bash
   cd ~/repos/atc
   cp config.yaml.example config.yaml
   $EDITOR config.yaml   # fill in hosts/users for the services you run
   ```

5. **Keyring backend** — `atc secrets set <key>` stores secrets in the OS
   keychain via the `keyring` package. On a desktop with GNOME
   Keyring/KWallet/macOS Keychain this works out of the box. On a headless
   Linux box (no secret service running), install the plaintext-file fallback
   backend and point `keyring` at it:
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

See `config.yaml.example` for all available integrations.
