# atc — Air Traffic Control

Homelab CLI for Proxmox, VMs, LXCs, Home Assistant, and the surrounding
service stack (Portainer, Pi-hole, UniFi, Plex, Seerr, Grafana/Prometheus,
Radarr/Sonarr, etc).

Extracted from `pentagon-management` (2026-09-08) into its own repo since it
had become the load-bearing tool for day-to-day homelab operations.

## Setup

```bash
cp config.yaml.example config.yaml   # fill in your values
ln -s "$(pwd)/atc" ~/bin/atc         # or wherever is on your PATH
```

Requires [uv](https://astral.sh/uv/). Secrets can be stored in the OS
keychain via `atc secrets set <key>` instead of plaintext in config.yaml.

## Usage

```
atc tui               # interactive TUI
atc get hosts          # node/VM/LXC status
atc acars               # alert digest (Prometheus/HA/etc)
atc secrets             # manage keychain secrets
atc run [script]        # browse and run repo scripts
```

See `config.yaml.example` for all available integrations.
