#!/usr/bin/env bash
# Desktopia finale: bring up the whole stack and stream Slicer to the browser over QUIC.
#   server.py runs the GPU Wayland compositor + H.264 encode + WebTransport server;
#   then Xwayland + openbox + Slicer render INTO the compositor as clients.
# Run on the box: make stream   (then open client/index.html with the printed cert hash + port)
set -uo pipefail
cd "$(dirname "$0")"

# --- compositor-side env (NOT the NVIDIA GBM env — that is for X clients only) ---
export LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}   # libwayland 1.25
export GST_PLUGIN_PATH=/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0:${GST_PLUGIN_PATH:-}
export GST_REGISTRY_FORK=no
export XDG_RUNTIME_DIR=/tmp/wl-rt; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export WAYLAND_DISPLAY=wayland-1
SOCK="$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY"
NV="env GBM_BACKEND=nvidia-drm __GLX_VENDOR_LIBRARY_NAME=nvidia __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
SLICER_DIR=$(ls -d /opt/Slicer-*/ 2>/dev/null | head -1)

cleanup() { kill $(jobs -p) 2>/dev/null; }
trap cleanup EXIT INT TERM

# --- ensure runtime deps (idempotent; covers a fresh box that only ran wl-setup) ---
export DEBIAN_FRONTEND=noninteractive
gst-inspect-1.0 x264enc >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y --no-install-recommends gstreamer1.0-plugins-ugly gstreamer1.0-libav >/dev/null 2>&1; }
command -v openbox  >/dev/null 2>&1 || apt-get install -y --no-install-recommends openbox wmctrl >/dev/null 2>&1
python3 -c 'import aioquic' 2>/dev/null || pip3 install --break-system-packages "aioquic>=1.0" >/dev/null 2>&1
python3 -c 'import Xlib'   2>/dev/null || apt-get install -y --no-install-recommends python3-xlib >/dev/null 2>&1

# --- self-signed ECDSA P-256 cert, <=14 days (WebTransport serverCertificateHashes). ---
# Persist it (survives restarts/stop-start) and only regenerate when missing/near-expiry,
# so the cert hash you paste into the client stays stable between `make stream` runs.
CERT=/root/desktopia-cert.pem; KEY=/root/desktopia-key.pem
if [ ! -f "$CERT" ] || ! openssl x509 -in "$CERT" -checkend 86400 >/dev/null 2>&1; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout "$KEY" -out "$CERT" -days 13 -nodes -subj "/CN=desktopia" 2>/dev/null
fi

# --- start the streaming server (compositor pipeline + QUIC) ---
rm -f "$SOCK"
python3 server.py --cert "$CERT" --key "$KEY" --port 4433 >/tmp/server.log 2>&1 &
for i in $(seq 1 100); do [ -S "$SOCK" ] && break; sleep 0.25; done
if [ ! -S "$SOCK" ]; then echo "FAIL: compositor socket never appeared. server.log:"; tail -n 40 /tmp/server.log; exit 1; fi
echo "compositor + QUIC server up (socket $SOCK)"

# --- close_range() shim: vast's seccomp denies close_range with EPERM (not ENOSYS), which
# breaks GLib g_spawn (openbox menu -> "Failed to close file descriptor ... Operation not
# permitted"). Make close_range report ENOSYS so GLib falls back to its normal fd-close path. ---
PRELOAD=/tmp/noclose_range.so
if [ ! -f "$PRELOAD" ] && command -v gcc >/dev/null 2>&1; then
  printf '%s\n' '#define _GNU_SOURCE' '#include <errno.h>' \
    'int close_range(unsigned int a, unsigned int b, int c){ (void)a;(void)b;(void)c; errno=ENOSYS; return -1; }' \
    > /tmp/ncr.c
  gcc -shared -fPIC -o "$PRELOAD" /tmp/ncr.c 2>/dev/null || true
fi

# --- X-client environment, inherited by openbox AND every app it launches: hardware GL via
# the NVIDIA render node + the spawn fix. This makes it a normal GPU desktop. ---
export GBM_BACKEND=nvidia-drm
export __GLX_VENDOR_LIBRARY_NAME=nvidia
export __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json
[ -f "$PRELOAD" ] && export LD_PRELOAD="$PRELOAD"
export DISPLAY=:2
ulimit -n 65536 2>/dev/null || true

Xwayland :2 -geometry 1920x1080 >/tmp/xway.log 2>&1 &
for i in $(seq 1 40); do [ -e /tmp/.X11-unix/X2 ] && break; sleep 0.25; done
openbox >/tmp/wm.log 2>&1 &
if [ -n "$SLICER_DIR" ]; then
  "$SLICER_DIR/Slicer" --no-splash >/tmp/slicer.log 2>&1 &
  sleep 8
  wmctrl -r :ACTIVE: -b add,maximized_vert,maximized_horz 2>/dev/null || true
else
  echo "NOTE: Slicer not found in /opt; run make slicertest once to download it. Showing glxgears."
  glxgears >/tmp/glxgears.log 2>&1 &
fi

echo "=================================================================="
echo -n "CERT_SHA256_BASE64="
openssl x509 -in "$CERT" -outform der | openssl dgst -sha256 -binary | base64
echo "Now: 'make port' for the public IP:PORT, paste both into client/index.html, open in Chrome."
echo "(server log: /tmp/server.log   slicer log: /tmp/slicer.log)"
echo "=================================================================="
wait
