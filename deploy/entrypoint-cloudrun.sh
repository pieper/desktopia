#!/usr/bin/env bash
# Cloud Run entrypoint: bring up the single-port nginx proxy on $PORT, then hand off to the normal CPU
# desktopia session (Xvfb + Slicer + offload servers + the WS stream server). nginx accepts on $PORT
# immediately, so Cloud Run marks the instance ready; requests 502 for the few seconds until server.py is
# up (or are instant on a warm/min-instance). Internal services: :4434 page+video-WS, :2027/:2028 offload.
set -uo pipefail
cd /opt/desktopia

export PORT="${PORT:-8080}"
# render the proxy config (only ${PORT} is substituted; nginx's own $vars are preserved)
envsubst '${PORT}' < /etc/nginx/proxy.conf.tmpl > /etc/nginx/conf.d/desktopia.conf
rm -f /etc/nginx/conf.d/default.conf /etc/nginx/sites-enabled/default 2>/dev/null || true
nginx -t
nginx                                   # master daemonizes; workers proxy to the internal services
echo "desktopia: nginx listening on :$PORT -> :4434 (page/video) /offload/ -> :2027 /offload-ws -> :2028"

# Cloud Run has no exec + Slicer/server logs go to /tmp -> stream them to stdout so they show in Cloud Run logs.
( tail -n +1 -F /tmp/slicer.log /tmp/server.log /tmp/xvfb.log 2>/dev/null | sed -u 's/^/[svc] /' ) &

# Run the existing CPU session orchestrator (creates 'user', stages code, Xvfb + Slicer + server.py).
exec bash /opt/desktopia/entrypoint-wayland.sh
