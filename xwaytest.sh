#!/usr/bin/env bash
# Cleanest X11 path: ROOTFUL Xwayland as a DIRECT native Wayland client of gst-wayland-display
# (no nested cage/wlroots, so no nested-output swapchain to fail). Xwayland hosts X apps; its
# single root surface is composited+captured (the native-client path we proved works). X11 3D
# apps get hardware GLX via the render node. Run: make xwaytest
set -uo pipefail
export LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export GST_PLUGIN_PATH=/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0:${GST_PLUGIN_PATH:-}
export GST_REGISTRY_FORK=no
export GBM_BACKEND=nvidia-drm
export __GLX_VENDOR_LIBRARY_NAME=nvidia
export __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json
export XDG_RUNTIME_DIR=/tmp/wl-rt; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export WAYLAND_DISPLAY=wayland-1
SOCK="$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY"
unset DISPLAY

command -v Xwayland >/dev/null || { export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y --no-install-recommends xwayland mesa-utils x11-apps; }

rm -f "$SOCK" /tmp/xway-*.png /root/desktopia/last-frame.png
echo "== outer compositor (gst-wayland-display) -> PNG =="
gst-launch-1.0 -e -q \
  waylanddisplaysrc ! video/x-raw,width=1280,height=720,format=RGBx,framerate=30/1 \
  ! videoconvert ! pngenc ! multifilesink location=/tmp/xway-%05d.png max-files=10 \
  >/tmp/xway-gst.log 2>&1 &
GSTPID=$!
for i in $(seq 1 60); do [ -S "$SOCK" ] && break; sleep 0.25; done
[ -S "$SOCK" ] || { echo "FAIL: no compositor socket"; tail -n 20 /tmp/xway-gst.log; kill "$GSTPID" 2>/dev/null; exit 1; }
echo "compositor socket up: $SOCK"

echo "== rootful Xwayland :2 as a native Wayland client =="
Xwayland :2 -rootful -geometry 1280x720 >/tmp/xway-x.log 2>&1 &
XPID=$!
for i in $(seq 1 40); do [ -e /tmp/.X11-unix/X2 ] && break; sleep 0.25; done
[ -e /tmp/.X11-unix/X2 ] || { echo "FAIL: Xwayland :2 did not start"; tail -n 30 /tmp/xway-x.log; kill "$GSTPID" "$XPID" 2>/dev/null; exit 1; }
echo "Xwayland up on :2"
echo "-- Xwayland glamor/EGL init --"; grep -iE "glamor|egl|nvidia|llvmpipe|render|gbm" /tmp/xway-x.log | head -8

echo "== glxgears on :2 (hardware GLX through Xwayland?) =="
DISPLAY=:2 timeout 9 glxgears -info >/tmp/xway-gears.log 2>&1 &
sleep 7
grep -iE "GL_RENDERER|GL_VENDOR|FPS" /tmp/xway-gears.log | head -8

kill "$GSTPID" "$XPID" 2>/dev/null; wait 2>/dev/null
echo "== captured frames (blank ~2.7KB; visible gears compress much larger) =="
ls -lS /tmp/xway-*.png 2>/dev/null | head -5
BIG=$(ls -S /tmp/xway-*.png 2>/dev/null | head -1)
if [ -n "$BIG" ]; then
  SZ=$(stat -c%s "$BIG"); echo "largest: $BIG ($SZ bytes)"
  cp "$BIG" /root/desktopia/last-frame.png
  echo "saved -> repo/last-frame.png  (pull to view:  make pull REMOTE=/root/desktopia/last-frame.png)"
  [ "$SZ" -gt 8000 ] && echo "PASS: rootful-Xwayland content was composited+captured. Slicer path is clear." \
                     || echo "PARTIAL: frame small — see Xwayland log above (glamor/buffer)."
fi
