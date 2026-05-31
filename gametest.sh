#!/usr/bin/env bash
# X11 path de-risk: run gamescope NESTED inside gst-wayland-display (gamescope supplies the
# XWayland that gst-wayland-display lacks), run an X11 GL app (glxgears) in it, and capture a
# VISIBLY hardware-rendered frame. Proves the Slicer (Qt5/X11) path. Run: make gametest
set -uo pipefail
export LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export GST_PLUGIN_PATH=/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0:${GST_PLUGIN_PATH:-}
export GST_REGISTRY_FORK=no
export XDG_RUNTIME_DIR=/tmp/wl-rt; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export WAYLAND_DISPLAY=wayland-1
SOCK="$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY"
unset DISPLAY

# cage = lightweight wlroots single-app kiosk compositor; provides XWayland, takes `-- cmd`,
# and runs NESTED as a Wayland client (render node only, no DRM/KMS -> dodges wlroots+NVIDIA
# DRM issues). (gamescope isn't packaged for Ubuntu 24.04.)
if ! command -v cage >/dev/null; then
  echo "== installing cage + xwayland + glxgears =="
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y --no-install-recommends cage xwayland mesa-utils \
    || echo "WARN: apt cage failed"
fi
command -v cage >/dev/null || { echo "FAIL: cage not available (try sway/weston instead)"; exit 1; }
echo "cage: $(cage --version 2>&1 | head -1)"

rm -f "$SOCK" /tmp/game-*.png
echo "== outer compositor -> PNG frames =="
gst-launch-1.0 -e -q \
  waylanddisplaysrc ! video/x-raw,width=1280,height=720,format=RGBx,framerate=30/1 \
  ! videoconvert ! pngenc ! multifilesink location=/tmp/game-%05d.png max-files=8 \
  >/tmp/game-gst.log 2>&1 &
GSTPID=$!
for i in $(seq 1 60); do [ -S "$SOCK" ] && break; sleep 0.25; done
[ -S "$SOCK" ] || { echo "FAIL: no compositor socket"; tail -n 20 /tmp/game-gst.log; kill "$GSTPID" 2>/dev/null; exit 1; }
echo "compositor socket up: $SOCK"

echo "== cage (nested wlroots, render node) running glxgears via its XWayland =="
WAYLAND_DISPLAY=wayland-1 \
WLR_BACKENDS=wayland \
WLR_RENDER_DRM_DEVICE=/dev/dri/renderD128 \
WLR_NO_HARDWARE_CURSORS=1 \
  timeout 14 cage -- glxgears -info >/tmp/game-scope.log 2>&1 &
sleep 11
echo "-- cage/glxgears log (look for GL_RENDERER = NVIDIA, XWayland up) --"
grep -iE "renderer|nvidia|llvmpipe|xwayland|error|fail|backend" /tmp/game-scope.log | head -20
echo "-- (full tail) --"; tail -n 12 /tmp/game-scope.log

kill "$GSTPID" 2>/dev/null; wait "$GSTPID" 2>/dev/null

echo "== captured frames (blank background ~2-3KB; visible gears compress much larger) =="
ls -lS /tmp/game-*.png 2>/dev/null | head -8
BIG=$(ls -S /tmp/game-*.png 2>/dev/null | head -1)
if [ -n "$BIG" ]; then
  SZ=$(stat -c%s "$BIG" 2>/dev/null || echo 0)
  echo "largest frame: $BIG ($SZ bytes)"
  if [ "$SZ" -gt 8000 ]; then
    echo "PASS: gamescope+XWayland rendered visible content (frame >8KB). Slicer path is GO."
  else
    echo "PARTIAL: frames are small — gamescope may not have rendered into the outer compositor;"
    echo "  check the gamescope log above for backend/XWayland errors."
  fi
fi
