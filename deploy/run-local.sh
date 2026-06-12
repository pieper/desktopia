#!/usr/bin/env bash
# Run the Desktopia CPU (software-render) server locally under Docker/colima and expose it to your
# browser at http://localhost:PORT. Real Slicer on a real X workstation (Xvfb + Mesa llvmpipe),
# streamed over plain WebSocket. No GPU needed.
#
# Fast-iteration design:
#   * image  = heavy apt deps only, built once (deploy/Dockerfile.cpu)
#   * Slicer = a PERSISTENT volume mounted at /opt -> downloaded ONCE on first run, reused after
#   * code   = the repo bind-mounted at /opt/desktopia -> edit on the host, `docker restart` to apply
#
#   ./deploy/run-local.sh            # build (first time) + run, prints the URL
#   ./deploy/run-local.sh logs       # follow container + server logs
#   ./deploy/run-local.sh restart    # re-stage code + restart the session (after editing)
#   ./deploy/run-local.sh stop       # stop & remove the container (keeps Slicer volume)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE=desktopia:cpu
NAME=desktopia-cpu
PORT="${PORT:-4434}"
SLICER_DIR="${DESKTOPIA_SLICER_DIR:-/tmp/desktopia-opt}"   # persistent /opt (holds Slicer-*), in the VM's /tmp

cmd="${1:-up}"
case "$cmd" in
  logs)    exec docker logs -f "$NAME" ;;
  slog)    exec docker exec "$NAME" tail -f /tmp/server.log ;;
  restart) exec docker restart "$NAME" ;;
  stop)    docker rm -f "$NAME" desktopia-proxy >/dev/null 2>&1 || true; echo "stopped (Slicer volume $SLICER_DIR kept)"; exit 0 ;;
  shell)   exec docker exec -it "$NAME" bash ;;
esac

# build the deps image once (cached thereafter)
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo ">> building $IMAGE (deps only, one-time)…"
  docker build -t "$IMAGE" -f "$REPO/deploy/Dockerfile.cpu" "$REPO"
fi

# persistent Slicer dir in the VM's /tmp: survives `docker rm`/`run` (the iteration loop). A
# `colima stop/start` reboots the VM and clears /tmp -> Slicer re-downloads once; switch SLICER_DIR
# to a path under $HOME (always mounted) or a `docker volume` if you want it to survive that too.
docker exec "$NAME" true 2>/dev/null && docker rm -f "$NAME" >/dev/null 2>&1 || true
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo ">> starting $NAME (Slicer persists in $SLICER_DIR; code bind-mounted from $REPO)…"
docker run -d --name "$NAME" \
  -p "$PORT:4434" \
  -p 2027:2027 \
  -p 2028:2028 \
  -p 2126:2026 \
  -e DESKTOPIA_OFFLOAD=1 \
  -e DESKTOPIA_MCP=1 \
  -e DESKTOPIA_FPS="${DESKTOPIA_FPS:-60}" \
  -e DESKTOPIA_BITRATE="${DESKTOPIA_BITRATE:-8000}" \
  --shm-size=2g \
  -v "$SLICER_DIR:/opt" \
  -v "$REPO:/opt/desktopia" \
  "$IMAGE" >/dev/null

# Thin single-port reverse proxy (mirrors the Cloud Run topology: one origin for page + video-WS + offload).
# Local dev runs it as a sidecar over the container's mapped ports; the Cloud Run image bakes nginx instead.
PROXY_PORT="${PROXY_PORT:-8080}"
docker rm -f desktopia-proxy >/dev/null 2>&1 || true
docker run -d --name desktopia-proxy -p "$PROXY_PORT:80" \
  -v "$REPO/deploy/proxytest:/etc/nginx/conf.d:ro" nginx:alpine >/dev/null 2>&1 || echo "   (proxy sidecar failed to start)"

cat <<EOF
>> up. First run downloads Slicer into $SLICER_DIR (~minutes); the desktop/page is live immediately.
   Open:   http://localhost:$PROXY_PORT/     (single-port proxy; the offload needs this same-origin entry)
   Direct: http://localhost:$PORT/  + :2027/:2028 still exposed for dev/harness
   Logs:   ./deploy/run-local.sh logs        (container)
           ./deploy/run-local.sh slog        (server.py)
   Shell:  ./deploy/run-local.sh shell
   After editing code:  ./deploy/run-local.sh restart
EOF
