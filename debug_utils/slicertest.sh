#!/usr/bin/env bash
# The real target: run 3D Slicer (Qt5/VTK, X11) on rootful Xwayland inside gst-wayland-display,
# with a tiny WM so it maximizes, and capture a frame. VTK's 3D viewport should be hardware GLX
# (same path glxgears proved); Qt 2D is software-cheap. Run: make slicertest
# NOTE: first run downloads ~1.5GB Slicer + installs X libs; cached after (persists across stop/start).
set -uo pipefail
export LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export GST_PLUGIN_PATH=/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0:${GST_PLUGIN_PATH:-}
export GST_REGISTRY_FORK=no
export XDG_RUNTIME_DIR=/tmp/wl-rt; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export WAYLAND_DISPLAY=wayland-1
SOCK="$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY"
unset DISPLAY
# NVIDIA GBM/EGL for CLIENTS ONLY (never the compositor).
NV="env GBM_BACKEND=nvidia-drm __GLX_VENDOR_LIBRARY_NAME=nvidia __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json"

export DEBIAN_FRONTEND=noninteractive
command -v openbox >/dev/null || { echo "== installing openbox (tiny WM) + X libs =="; apt-get update -qq && apt-get install -y --no-install-recommends openbox wmctrl xterm; }

# --- Slicer runtime deps (common set for the prebuilt tarball on minimal Ubuntu) ---
echo "== ensuring Slicer X/runtime deps =="
apt-get install -y --no-install-recommends \
  libpulse0 libnss3 libglu1-mesa libxcb-xinerama0 libxcb-icccm4 libxcb-image0 \
  libxcb-keysyms1 libxcb-render-util0 libxcb-cursor0 libxcb-randr0 libxcb-shape0 \
  libxcb-xkb1 libxkbcommon-x11-0 libxrender1 libxi6 libsm6 libxtst6 libxrandr2 \
  libxcomposite1 libxcursor1 libxdamage1 libxfixes3 libfontconfig1 libdbus-1-3 \
  libxcb-util1 libxcb-xfixes0 curl >/dev/null 2>&1 || echo "WARN: some deps may be missing"

# --- fetch Slicer (cached) ---
SLICER_DIR=$(ls -d /opt/Slicer-*/ 2>/dev/null | head -1)
if [ -z "$SLICER_DIR" ]; then
  echo "== downloading Slicer (~1.5GB, one-time) =="
  curl -L --retry 3 -o /tmp/Slicer.tar.gz "https://download.slicer.org/download?os=linux&stability=release"
  echo "== extracting =="
  tar -xf /tmp/Slicer.tar.gz -C /opt/
  SLICER_DIR=$(ls -d /opt/Slicer-*/ 2>/dev/null | head -1)
fi
[ -n "$SLICER_DIR" ] && [ -x "$SLICER_DIR/Slicer" ] || { echo "FAIL: Slicer not found/extracted"; exit 1; }
echo "Slicer: $SLICER_DIR"

rm -f "$SOCK" /tmp/slicer-*.png /root/desktopia/last-frame.png
echo "== compositor -> PNG =="
gst-launch-1.0 -e -q \
  waylanddisplaysrc ! video/x-raw,width=1920,height=1080,format=RGBx,framerate=30/1 \
  ! videoconvert ! pngenc ! multifilesink location=/tmp/slicer-%05d.png max-files=12 \
  >/tmp/slicer-gst.log 2>&1 &
GSTPID=$!
for i in $(seq 1 60); do [ -S "$SOCK" ] && break; sleep 0.25; done
[ -S "$SOCK" ] || { echo "FAIL: no compositor socket"; tail -n 20 /tmp/slicer-gst.log; kill "$GSTPID" 2>/dev/null; exit 1; }

echo "== Xwayland :2 + openbox WM =="
$NV Xwayland :2 -geometry 1920x1080 >/tmp/slicer-x.log 2>&1 &
XPID=$!
for i in $(seq 1 40); do [ -e /tmp/.X11-unix/X2 ] && break; sleep 0.25; done
[ -e /tmp/.X11-unix/X2 ] || { echo "FAIL: Xwayland :2"; tail -n 20 /tmp/slicer-x.log; kill "$GSTPID" "$XPID" 2>/dev/null; exit 1; }
DISPLAY=:2 openbox >/tmp/slicer-wm.log 2>&1 &
WMPID=$!

echo "== launching Slicer on :2 (give it ~30s to start) =="
DISPLAY=:2 $NV "$SLICER_DIR/Slicer" --no-splash >/tmp/slicer-app.log 2>&1 &
APPID=$!
sleep 28
DISPLAY=:2 wmctrl -r :ACTIVE: -b add,maximized_vert,maximized_horz 2>/dev/null || true
sleep 3
echo "-- Slicer log tail --"; tail -n 15 /tmp/slicer-app.log

kill "$APPID" "$WMPID" "$GSTPID" "$XPID" 2>/dev/null; wait 2>/dev/null
echo "== frames =="
ls -lS /tmp/slicer-*.png 2>/dev/null | head -5
BIG=$(ls -S /tmp/slicer-*.png 2>/dev/null | head -1)
if [ -n "$BIG" ]; then
  cp "$BIG" /root/desktopia/last-frame.png
  echo "largest: $BIG ($(stat -c%s "$BIG") bytes) -> repo/last-frame.png"
  echo "view it:  make pull REMOTE=/root/desktopia/last-frame.png"
fi
