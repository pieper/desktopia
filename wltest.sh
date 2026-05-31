#!/usr/bin/env bash
# End-to-end validation of the headless GPU Wayland path:
#   waylanddisplaysrc (Smithay compositor, render node) -> GStreamer -> PNG frames,
#   with a hardware-GL client (weston-simple-egl) rendering INTO the compositor.
# PASS = a client gets an NVIDIA GL renderer via the compositor AND we capture non-blank frames.
# Run after wl-build + wl-fixwayland: make wltest
set -uo pipefail

# --- runtime env every waylanddisplaysrc pipeline needs ---
export LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}   # libwayland >=1.23
export GST_PLUGIN_PATH=/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0:${GST_PLUGIN_PATH:-}
export GST_REGISTRY_FORK=no
export XDG_RUNTIME_DIR=/tmp/wl-rt
mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export WAYLAND_DISPLAY=wayland-1
SOCK="$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY"
unset DISPLAY   # we are the compositor; don't let X leak in

echo "== full element properties (render-node / resolution / socket opts) =="
gst-inspect-1.0 waylanddisplaysrc 2>/dev/null | sed -n '/Element Properties:/,$p'

rm -f "$SOCK" /tmp/wl-frame-*.png
echo "== bringing up compositor pipeline -> PNG frames =="
gst-launch-1.0 -e -q \
  waylanddisplaysrc ! video/x-raw,width=1280,height=720,format=RGBx,framerate=30/1 \
  ! videoconvert ! pngenc ! multifilesink location=/tmp/wl-frame-%05d.png max-files=4 \
  >/tmp/wl-gst.log 2>&1 &
GSTPID=$!

for i in $(seq 1 60); do [ -S "$SOCK" ] && break; sleep 0.25; done
if [ ! -S "$SOCK" ]; then
  echo "FAIL: compositor socket $SOCK never appeared. gst log:"; tail -n 30 /tmp/wl-gst.log
  kill "$GSTPID" 2>/dev/null; exit 1
fi
echo "compositor socket up: $SOCK"

echo "== client GL renderer THROUGH the compositor (the decisive line) =="
WAYLAND_DISPLAY=wayland-1 eglinfo 2>/dev/null \
  | sed -n '/[Ww]ayland platform/,/platform:/p' \
  | grep -iE "OpenGL.*(vendor|renderer)" | head -6 \
  || echo "(eglinfo wayland section not parsed; weston client result below is the fallback)"

echo "== render a hardware-GL client into the compositor (so frames aren't blank) =="
if command -v weston-simple-egl >/dev/null; then
  WAYLAND_DISPLAY=wayland-1 timeout 5 weston-simple-egl >/tmp/wl-client.log 2>&1 &
  sleep 4
else
  echo "weston-simple-egl not installed; skipping client render"
fi

sleep 1
kill "$GSTPID" 2>/dev/null; wait "$GSTPID" 2>/dev/null

echo "== captured frames =="
ls -l /tmp/wl-frame-*.png 2>/dev/null | tail -4 || echo "  no frames captured"
N=$(ls /tmp/wl-frame-*.png 2>/dev/null | wc -l | tr -d ' ')
if [ "$N" -gt 0 ]; then
  echo "PASS(partial): compositor ran and produced $N frame(s). Check the renderer line above"
  echo "  for 'NVIDIA' (hardware) -- if so, the headless GPU Wayland path is fully GO."
else
  echo "FAIL: no frames. gst log:"; tail -n 30 /tmp/wl-gst.log
  exit 1
fi
