# atc — Air Traffic Control

Homelab CLI for Proxmox, VMs, LXCs, Home Assistant, and the surrounding
service stack (Portainer, Pi-hole, UniFi, Plex, Seerr, Grafana/Prometheus,
Radarr/Sonarr, etc).

## Why this exists

Running a homelab with a dozen-plus services across Proxmox, Docker, and a
few bare-metal boxes means the same handful of questions come up constantly
— "is everything up?", "which node is this VM on?", "is this cert about to
expire?", "what's using all the bandwidth?" — each normally answered by
opening a different web UI, or SSHing in and remembering a different
command for each service. `atc` collapses all of that into one CLI (and an
optional TUI): one alert digest (`atc acars`) that correlates monitoring,
TLS expiry, and device health across every integration in a single call
instead of checking ten dashboards one at a time; one place secrets live
(OS keychain, never plaintext); one command shape across wildly different
services (Proxmox, Pi-hole, UniFi, Plex, Radarr/Sonarr, Grafana...) instead
of a different mental model per vendor. It's built for one specific
homelab, but the integration clients, config-driven design, and
keychain-backed secrets handling generalize to anyone running a similar
mixed Proxmox/Docker/service-stack setup.

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
   This launches an interactive wizard. First, if you're on a headless
   Linux box still using the plaintext keyring fallback, it walks you
   through the same encrypted-backend upgrade described in step 4 above —
   explains what's happening and why, then asks a single yes/no question.
   Skip it once and it's still one `atc secrets migrate-encryption` away
   any time later. Already encrypted (or on macOS/GNOME-Keyring/KWallet)?
   It's a silent one-line confirmation and moves straight on.

   Then it lists every supported integration — pick which ones to
   configure (comma-separated numbers, `all`, or `q` to skip). Secrets you
   enter are stored in the OS keychain automatically, never written to
   `config.yaml` in plaintext. Safe to re-run any time to add or update a
   section — existing sections are left untouched unless you select them
   again.

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
atc radar               # interactive TUI
atc get hosts            # node/VM/LXC status
atc acars                # alert digest (Prometheus/HA/etc)
atc secrets              # manage keychain secrets
atc run [script]         # browse and run repo scripts
```

## Command tree

Full command tree, including every subcommand and its flags. Regenerate with
`atc tree` any time the CLI changes — don't hand-edit this out of sync:

```
atc  Air Traffic Control — homelab management CLI
├── acars  --certs-warn <integer> --certs-crit <integer> --beszel-cpu-warn
│   <float> --beszel-cpu-crit <float> --beszel-mem-warn <float>
│   --beszel-mem-crit <float> --beszel-disk-warn <float> --beszel-disk-crit
│   <float> --beszel-token-warn-days <integer> -o/--output <text>  Active alerts
│   across all configured infrastructure.
├── auth  Manage PIN-based access control for atc.
│   ├── clear  Remove the PIN, disabling auth entirely.
│   ├── set-pin  Set or change the PIN required to run atc.
│   └── status  Show whether a PIN is currently configured.
├── backup  List and trigger Proxmox backups
│   ├── jobs  List scheduled backup jobs.
│   ├── list (ls)  -l/--label <text> -o <choice> -j  List backup files across
│   │   all cluster nodes.
│   ├── run  <selector> -l/--label <text> --storage/-s <text> --yes/-y  Create
│   │   an immediate backup of guest(s).
│   ├── snapshot  Compute per-VM backup freshness and write it to InfluxDB for
│   │   Grafana.
│   └── trigger  <job_id>  Trigger an immediate run of a scheduled backup job.
├── blackbox  --hours <integer> --all-containers --out <path>  Collect a
│   full-stack diagnostic snapshot and write it to a file.
├── certs  Show TLS certificate expiry for all configured endpoints.
│   └── check  Show TLS certificate expiry for all configured endpoints. (same
│       as bare 'atc certs')
├── docker  Manage Docker containers and Compose stacks across all Portainer
│   environments
│   ├── compose  Deploy, update, or remove Compose stacks via Portainer
│   │   ├── down  <name> --target/-t <text> --yes/-y  Remove a Portainer-managed
│   │   │   Compose stack and its containers.
│   │   └── up  <compose_file> --target/-t <text> --name/-n <text> --env-file/-f
│   │       <path> --pull --dry-run --yes/-y  Deploy or update a Compose stack
│   │       on a Portainer environment.
│   ├── images  --env/-e <text> --dangling/-d  List images across all Portainer
│   │   environments.
│   ├── logs  <selector> --env/-e <text> --tail/-n <integer> --since <integer>
│   │   Show recent logs for a container.
│   ├── ls  --env/-e <text> --all/-a  List containers across all Portainer
│   │   environments.
│   ├── prune  --env/-e <text> --volumes --images --all-images --yes/-y  Remove
│   │   orphaned volumes and dangling images across all Portainer environments.
│   ├── restart  <selector> --env/-e <text> --yes/-y  Restart a container or
│   │   Compose stack (matched by name).
│   ├── stacks  --env/-e <text>  List Compose stacks across all Portainer
│   │   environments.
│   ├── start  <selector> --env/-e <text> --yes/-y  Start a container or Compose
│   │   stack (matched by name).
│   ├── stop  <selector> --env/-e <text> --yes/-y  Stop a container or Compose
│   │   stack (matched by name).
│   └── volumes  --env/-e <text>  List volumes across all Portainer
│       environments.
├── get  Query cluster state — nodes, VMs, LXCs, and all hosts
│   ├── hosts (ls)  -l/--label <text> -o <choice> -j  List all hosts — Proxmox
│   │   VMs, LXCs, and standalone Beszel-only hosts.
│   ├── lxcs  -l/--label <text> -o <choice> -j  List LXC containers.
│   ├── nodes  -o <choice> -j  List Proxmox hypervisor nodes.
│   ├── storage  -o <choice> -j  List Proxmox storage pool capacity/usage (the
│   │   datastores backing VM/LXC disks).
│   └── vms  -l/--label <text> -o <choice> -j  List virtual machines.
├── ha  Manage Home Assistant
│   ├── call  <service> [entity_id] --data/-d <text>  Call a Home Assistant
│   │   service.
│   ├── get  <entity_id>  Get the current state and attributes of an entity.
│   ├── reload  [domain]  Reload YAML configuration — all or a specific domain.
│   ├── restart  --yes/-y  Restart the Home Assistant core.
│   ├── stats  Show configured sensor stats (power, temperature, humidity,
│   │   etc.).
│   ├── status  Show Home Assistant version and state.
│   ├── turn-off  <entity_id>  Turn off an entity (switch, light, fan, etc.).
│   ├── turn-on  <entity_id>  Turn on an entity (switch, light, fan, etc.).
│   ├── update  [entity_id] --all --yes/-y  Install a specific update or all
│   │   pending updates.
│   └── updates  List pending updates (core, supervisor, OS, HACS, integrations,
│       add-ons).
├── init  [section] --config <path>  Interactive setup wizard — pick which
│   integrations to configure.
├── media  Media pipeline: Seerr · Radarr · Sonarr · Plex · Streams
│   ├── calendar  [days] --radarr --sonarr  Show Radarr and Sonarr releases.
│   │   Positive days = upcoming, negative = past.
│   ├── pipeline  <query>  Show the complete media pipeline: request → *arr →
│   │   download → Plex.
│   ├── radarr  Manage Radarr (movies)
│   │   ├── scan  Trigger a full library rescan.
│   │   ├── search-missing  --yes/-y  Search for all missing movies.
│   │   └── status  Show Radarr version and connectivity.
│   ├── search-today  --yes/-y --dry-run  Search for today's calendar releases
│   │   that haven't been downloaded yet.
│   ├── seerr  Search and request media via Seerr
│   │   ├── list (ls)  --all/-a --tv/-t --movies/-m --processing/-p -o <choice>
│   │   │   -j  List media requests. Unfulfilled only by default.
│   │   ├── request  <tmdb_id> --type/-t <text> --yes/-y  Request a movie or TV
│   │   │   show by TMDB ID.
│   │   └── search  <query> -o <choice> -j  Search for movies and TV shows.
│   ├── sonarr  Manage Sonarr (TV shows)
│   │   ├── scan  Trigger a full library rescan.
│   │   ├── search-missing  --yes/-y  Search for all missing monitored episodes.
│   │   └── status  Show Sonarr version and connectivity.
│   ├── status  --radarr --sonarr --seerr --plex --tautulli  Show connectivity
│   │   status for all media services.
│   └── today  Today's media — scheduled releases, requests, and downloads with
│       file details.
├── metrics  Run ad-hoc PromQL queries against Prometheus.
│   ├── names  [match]  List available metric names (discovery — 'what can I
│   │   query?').
│   ├── query  <promql> -o <choice> -j  Instant PromQL query — current value(s)
│   │   of an expression.
│   └── range  <promql> --minutes/-m <integer> --step/-s <integer> -o <choice>
│       -j  Range PromQL query — samples over a time window (for trend/history,
│       not just a snapshot).
├── npm  Manage Nginx Proxy Manager (NPM) reverse-proxy hosts
│   ├── certs  -o/--output <text>  List SSL certificates known to NPM.
│   ├── create  <domain> <forward_host> <forward_port> --scheme <text>
│   │   --websocket --block-exploits --certificate-id <integer>
│   │   --certificate-name <text> --ssl-forced --yes/-y  Create a new proxy
│   │   host. Fails loudly if the domain already exists — use 'update' instead.
│   ├── delete  <domain> --yes/-y  Delete a proxy host by domain.
│   ├── list  -o/--output <text>  List all configured proxy hosts.
│   └── update  <domain> --forward-host <text> --forward-port <integer> --scheme
│       <text> --certificate-id <integer> --certificate-name <text> --ssl-forced
│       --websocket --block-exploits --yes/-y  Update an existing proxy host
│       (forward target, cert, SSL enforcement, etc).
├── pihole  Manage Pi-hole instances
│   ├── audit  -o <choice>  Flag connected UniFi clients that Pi-hole has never
│   │   seen a query from.
│   ├── dns  Manage custom local DNS records (primary instance, sync propagates)
│   │   ├── add  <domain> <ip> --yes/-y  Add a custom local DNS record (primary
│   │   │   instance; sync propagates).
│   │   ├── list  -o <choice> -j  List custom local DNS records (reads from
│   │   │   primary instance).
│   │   └── remove  <domain> <ip> --yes/-y  Remove a custom local DNS record
│   │       (primary instance).
│   ├── gravity  --yes/-y  Trigger a gravity (blocklist) update on ALL
│   │   instances.
│   ├── pause  --yes/-y  Pause DNS blocking indefinitely on ALL instances.
│   ├── status  Show status of all Pi-hole instances.
│   └── unpause  --yes/-y  Re-enable DNS blocking on ALL instances.
├── plex  Manage Plex Media Server, plus stream monitoring via Tautulli
│   ├── analyze  [library]  Trigger media analysis (thumbnails, chapter images)
│   │   on one or all libraries.
│   ├── clean-bundles  Clean up orphaned metadata bundles (server-wide).
│   ├── clients  --user/-u <text> --platform/-p <text> --search/-s <text>  List
│   │   distinct devices/players seen in Plex play history, most recent first.
│   ├── empty-trash  [library]  Empty the Plex trash for one or all libraries.
│   ├── history  --count/-n <integer> --user/-u <text> -o <choice> -j  Show
│   │   recent Plex play history.
│   ├── libraries  List all Plex library sections.
│   ├── scan  [library]  Trigger a filesystem scan on one or all Plex libraries.
│   ├── sessions  Show active Plex streams.
│   ├── stats  Show Plex server stats: library scan times, running tasks, and
│   │   butler schedule.
│   ├── status  Show Plex server version and connectivity.
│   └── terminate  <session_key> --message/-m <text> --yes/-y  Terminate an
│       active Plex stream.
├── ps  -l/--label <text> -o <choice> -j  List all hosts — VMs, LXCs, and
│   standalone hosts. (alias: get hosts)
├── push-key  [selector] --key-file/-k <text> --yes/-y  Push your SSH public key
│   to guests without needing existing SSH access.
├── quick  Publish quick throwaway files/pages via your configured 'quick'
│   stack
│   ├── bootstrap  --target <text> --dry-run --yes/-y  Set up (or upgrade) the
│   │   'quick' stack, Pi-hole A record, and NPM proxy host.
│   ├── list  List what's currently published (fetches the live /directory
│   │   page).
│   └── publish  <path> --name <text> --target <text>  Upload a file or
│       directory into the 'quick' stack and print its live URL.
├── radar  Launch the interactive TUI dashboard.
├── restart  <selector> -l/--label <text> --yes/-y  Restart running guest(s).
├── run  [name] [path_arg] --verbose/-v  Run a script from the configured
│   scripts folder.
├── secrets  Manage secrets stored in the system keychain
│   ├── delete (rm)  <key> --yes/-y  Delete a secret from the keychain.
│   ├── flag  <key> --local-only  Set or clear the local_only flag on an
│   │   existing secret, without touching its value.
│   ├── list (ls)  List all secret keys stored in the keychain.
│   ├── migrate-encryption  Migrate secrets from the plaintext keyring to an
│   │   AES-256 encrypted one.
│   ├── repair  <key>  Re-index a secret by verifying it matches the stored
│   │   value via SHA-256 hash.
│   ├── scan  Scan config.yaml for keychain: references and repair the index.
│   ├── set  <key> [value] --local-only  Set or update a secret in the keychain.
│   └── verify  <key>  Verify a value matches the stored secret by comparing
│       SHA-256 hashes.
├── service  Cross-cutting service deployment helpers (DNS + reverse proxy in
│   one step)
│   └── publish  <domain> <forward_host> <forward_port> --dns-ip <text> --scheme
│       <text> --websocket --yes/-y  One-shot: add the local DNS record
│       (Pi-hole) AND the reverse-proxy host (NPM) for a new service.
├── speedtest  --duration/-d <integer> --parallel/-p <integer> --ping-only
│   --samples/-s <integer> -o <choice> -j  Run a speed test against your
│   OpenSpeedTest server.
├── start  <selector> -l/--label <text> --yes/-y  Start stopped guest(s).
├── stop  <selector> -l/--label <text> --yes/-y  Gracefully shut down guest(s).
├── tree  Show all commands and their arguments as a tree.
├── unifi  Query the UniFi Network controller
│   ├── clients  -o <choice>  List currently-connected UniFi clients.
│   ├── ports  -o <choice> --device <text> --up-only  Live per-switch-port
│   │   throughput + LAG membership.
│   └── stats  -o <choice> --min-mbps <float>  Live per-client throughput
│       (current tx/rx Mbps + link speed) from the UniFi controller.
├── update  Run apt updates on nodes, VMs, or LXCs
│   ├── hosts  [selector] -l/--label <text> --yes/-y --verbose/-v  Run apt
│   │   upgrade on VMs and LXC containers.
│   ├── nodes  [name] --yes/-y --verbose/-v  Run apt upgrade on Proxmox
│   │   hypervisor nodes.
│   ├── remove-cmd  <guest>  Remove a guest's custom post-apt update command.
│   └── set-cmd  <guest> <cmd> --headless --no-sudo  Register a custom post-apt
│       update command for a guest.
├── upgrade  --check --yes/-y --remote-url <text>  Self-update atc by pulling
│   the latest commits from git.
├── uptimekuma  Show Uptime Kuma monitor statuses. (up)
│   └── not  -o <choice>  Show only down monitors.
└── versions  Docker image version tracking — n-2 supply-chain policy.
    ├── check  <image> --lag/-n <integer>  Show available semver tags and
    │   recommend the n-2 pin for an image.
    └── list  --lag/-n <integer> --floating/-f  List all Portainer-managed
        containers with their version status.
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
Mac Keychain→host secret sync helper (`atc-sync-secrets.sh`) live in a
separate private automation repo — they're homelab-specific automation,
not part of the `atc` CLI itself.

## License

MIT — see [LICENSE](LICENSE).
