#!/usr/bin/env bash
# Local smoke test of the SOFTWARE (Xvfb + ximagesrc + x264 + WebSocket) path -- no GPU required, so
# it runs on an arm64 Mac (native) or any Docker host. It brings up the streaming pipeline against a
# lightweight X client (a clock / glxgears) INSTEAD of Slicer, so you can validate Xvfb capture,
# software H.264 encode, the WebSocket transport, the served page, and XTEST input without the
# x86_64-only Slicer download or any emulation. For the real Slicer render, run the full image with
# `--platform linux/amd64` (Rosetta) or on a CPU Linux host / Colab.
#
#   scripts/soft-local.sh            # build + run; serves http://localhost:8080
#   APP=glxgears scripts/soft-local.sh   # use glxgears (llvmpipe) instead of xclock
#
# Then open http://localhost:8080 in Chrome. The page auto-selects the WebSocket transport.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT=${PORT:-8080}
W=${W:-1280}; H=${H:-720}; FPS=${FPS:-15}; BR=${BR:-4000}
APP=${APP:-xclock}                          # xclock (cheap, always moving) or glxgears (exercises GL)
CONTAINER=${CONTAINER:-docker}              # docker (Colima) or podman -- both work

IMG=desktopia-soft-test
# Native-arch ubuntu image with just the software streaming deps + the test X client.
"$CONTAINER" build -t "$IMG" -f - . <<DOCKERFILE
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-gi python3-xlib \
      gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      gstreamer1.0-plugins-ugly gstreamer1.0-tools gstreamer1.0-x \
      xvfb x11-apps mesa-utils libgl1-mesa-dri openssl ca-certificates \
 && pip3 install --break-system-packages --no-cache-dir "aioquic>=1.0" websockets \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py /app/server.py
COPY client /app/client
DOCKERFILE

echo "running software path: ${W}x${H}@${FPS} ${BR}k, app=$APP, page at http://localhost:${PORT}"
exec "$CONTAINER" run --rm -it -p "${PORT}:4434" -e APP="$APP" \
  -e W="$W" -e H="$H" -e FPS="$FPS" -e BR="$BR" "$IMG" bash -c '
    set -e
    export DISPLAY=:2 LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe
    Xvfb :2 -screen 0 ${W}x${H}x24 +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
    for i in $(seq 1 80); do [ -e /tmp/.X11-unix/X2 ] && break; sleep 0.25; done
    # a moving X client to capture (no Slicer needed for a pipeline smoke test)
    ( "$APP" >/tmp/app.log 2>&1 || xterm >/tmp/app.log 2>&1 ) &
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
      -keyout /tmp/k.pem -out /tmp/c.pem -days 1 -nodes -subj /CN=desktopia >/dev/null 2>&1
    printf "{\"ready\":true,\"transport\":\"websocket\"}" > client/status.json
    # --ws-plain: the browser hits ws:// directly on the mapped port (no TLS terminator in front).
    exec python3 server.py --cert /tmp/c.pem --key /tmp/k.pem \
      --source xvfb --width "$W" --height "$H" --fps "$FPS" --bitrate "$BR" \
      --ws-plain --serve-dir client
  '
