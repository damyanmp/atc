# `atc init dockhand` — end-to-end run against the sandbox

Scripted answers, in-memory keychain, temp config.yaml (`init_wizard_e2e.py`). Tokens redacted.

```
No config found — will create <tmp>/config.yaml

Dockhand (Docker management — Portainer successor, preview)
Token: Dockhand UI → avatar → API tokens (needs your password; tokens start with dh_). After saving, the 
wizard checks the token and can register Docker hosts in Dockhand for you (local socket, Hawser agent, or 
direct TCP).
    stored in keychain as 'dockhand-api-token'

✓ Wrote dockhand to <tmp>/config.yaml
  ✓ Dockhand connected, 2 environment(s)
    • hawser-loop (hawser-standard, hw-spike)
    • local (socket, /var/run/docker.sock)

New Docker host
  1. Hawser agent, standard mode
     Agent listens on the host (port 2376, token-protected); Dockhand connects in. Simplest on a LAN.
  2. Hawser agent, edge mode
     Agent dials out to Dockhand over a WebSocket; the host opens no ports. Use for hosts behind NAT or 
off-LAN.
  3. Local socket on the Dockhand server
     The Docker daemon Dockhand itself runs on (socket mounted into its container).
  4. Direct Docker API over TCP
     An already-exposed Docker API (e.g. tcp://host:2375). No agent; plain TCP is unauthenticated, so prefer 
Hawser.
  ✓ reachable, Docker 29.10.0
  ✓ Registered t-std in Dockhand (id 6)

New Docker host
  1. Hawser agent, standard mode
     Agent listens on the host (port 2376, token-protected); Dockhand connects in. Simplest on a LAN.
  2. Hawser agent, edge mode
     Agent dials out to Dockhand over a WebSocket; the host opens no ports. Use for hosts behind NAT or 
off-LAN.
  3. Local socket on the Dockhand server
     The Docker daemon Dockhand itself runs on (socket mounted into its container).
  4. Direct Docker API over TCP
     An already-exposed Docker API (e.g. tcp://host:2375). No agent; plain TCP is unauthenticated, so prefer 
Hawser.
  ✓ Registered t-edge in Dockhand (id 7)

  Run this on the Docker host (it dials out; no ports to open):

docker run -d --name hawser --restart unless-stopped \
  -e DOCKHAND_SERVER_URL=ws://127.0.0.1:3900/api/hawser/connect \
  -e TOKEN=<redacted> \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --tmpfs /tmp \
  --label dockhand.update=false \
  ghcr.io/finsys/hawser:latest

  This token is shown once. Dockhand keeps it; atc doesn't.
  ✓ Agent connected

New Docker host
  1. Hawser agent, standard mode
     Agent listens on the host (port 2376, token-protected); Dockhand connects in. Simplest on a LAN.
  2. Hawser agent, edge mode
     Agent dials out to Dockhand over a WebSocket; the host opens no ports. Use for hosts behind NAT or 
off-LAN.
  3. Local socket on the Dockhand server
     The Docker daemon Dockhand itself runs on (socket mounted into its container).
  4. Direct Docker API over TCP
     An already-exposed Docker API (e.g. tcp://host:2375). No agent; plain TCP is unauthenticated, so prefer 
Hawser.
  ✗ Connection test failed: fetch failed
  1. Test again
  2. Save it anyway (fix later in Dockhand)
  3. Skip this host

--- run 1 transcript ---
? Dockhand host/IP -> 127.0.0.1
? Port -> 3900
? Use HTTPS?? -> False
? API token (dh_...) -> ***
? Verify SSL?? -> False
? Register a Docker host in Dockhand now? -> True
? Connection type -> 1
? Name (shown in atc and Dockhand) -> t-std
? Host/IP of the Docker host -> hw-spike
? Port -> 2376
? Use TLS? -> False
? Is a Hawser agent already running on that host? -> True
? Agent TOKEN -> ***
? Add another Docker host? -> True
? Connection type -> 2
? Name (shown in atc and Dockhand) -> t-edge
? Press Enter once the agent is running -> (started edge agent)
? Add another Docker host? -> True
? Connection type -> 4
? Name (shown in atc and Dockhand) -> t-direct
? Host/IP of the Docker host -> hw-spike
? Port -> 2399
? Use TLS? -> False
? What now -> 3
? Add another Docker host? -> False
config.yaml:
dockhand:
  host: 127.0.0.1
  port: 3900
  https: false
  api_token: keychain:dockhand-api-token
  verify_ssl: false

keychain keys written: ['dockhand-api-token']
Found existing config at <tmp>/config.yaml
Dockhand is already configured (127.0.0.1).
  1. Add Docker hosts to it
  2. Reconfigure the connection
  ✓ Dockhand connected, 4 environment(s)
    • hawser-loop (hawser-standard, hw-spike)
    • local (socket, /var/run/docker.sock)
    • t-edge (hawser-edge)
    • t-std (hawser-standard, hw-spike)

New Docker host
  1. Hawser agent, standard mode
     Agent listens on the host (port 2376, token-protected); Dockhand connects in. Simplest on a LAN.
  2. Hawser agent, edge mode
     Agent dials out to Dockhand over a WebSocket; the host opens no ports. Use for hosts behind NAT or 
off-LAN.
  3. Local socket on the Dockhand server
     The Docker daemon Dockhand itself runs on (socket mounted into its container).
  4. Direct Docker API over TCP
     An already-exposed Docker API (e.g. tcp://host:2375). No agent; plain TCP is unauthenticated, so prefer 
Hawser.
    an environment named 't-std' already exists in Dockhand
  ✓ reachable, Docker 29.10.0
  ✓ Registered t-sock in Dockhand (id 8)

--- run 2 transcript ---
? What now -> 1
? Register a Docker host in Dockhand now? -> True
? Connection type -> 3
? Name (shown in atc and Dockhand) -> t-std
? Name (shown in atc and Dockhand) -> t-sock
? Socket path (inside the Dockhand container) -> /var/run/docker.sock
? Add another Docker host? -> False

Dockhand environments now:
  2 hawser-loop  hawser-standard  test=ok
  1 local        socket           test=ok
  7 t-edge       hawser-edge      test=ok
  8 t-sock       socket           test=ok
  6 t-std        hawser-standard  test=ok
```
