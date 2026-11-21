#!/bin/sh
# Throwaway Dockhand sandbox for the atc API spike.
# Everything binds to 127.0.0.1 on the host running it, so auth being off at first launch
# never reaches the LAN. Tear down with: sandbox.sh down
set -eu
DH_VER="${DH_VER:-v1.0.58}"
HW_VER="${HW_VER:-latest}"
NET=dh-spike
case "${1:-up}" in
  up)
    docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
    # Dockhand server, managing the local socket ("local" environment)
    docker run -d --name dh-spike --network "$NET" -p 127.0.0.1:3900:3000 \
      -e FEAT_API_DOCS=true \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v dh-spike-data:/app/data \
      --label dockhand.update=false \
      "fnsys/dockhand:${DH_VER}" >/dev/null
    # Hawser in standard mode, as a stand-in for a remote host (same daemon, agent code path)
    HW_TOKEN_FILE="${HW_TOKEN_FILE:?set HW_TOKEN_FILE to a 0600 file holding the agent token}"
    docker run -d --name hw-spike --network "$NET" \
      --env-file "$HW_TOKEN_FILE" \
      -v /var/run/docker.sock:/var/run/docker.sock \
      --tmpfs /tmp \
      --label dockhand.update=false \
      "ghcr.io/finsys/hawser:${HW_VER}" >/dev/null
    docker ps --filter name=spike --format '{{.Names}} {{.Image}} {{.Status}}'
    ;;
  down)
    docker rm -f dh-spike hw-spike >/dev/null 2>&1 || true
    docker ps -a --filter label=com.docker.compose.project=atc-spike-demo -q | xargs -r docker rm -f >/dev/null
    docker volume rm dh-spike-data >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    echo "sandbox removed"
    ;;
esac
