#!/usr/bin/env bash
# De-risk the ENCODER before wiring QUIC: capture the compositor through NVENC to an mp4 file,
# with a moving GL client (glxgears) for content. Confirms the nvcodec element name + that
# waylanddisplaysrc -> NVENC works. Produces repo/test.mp4 to pull and play. Run: make streamtest
set -uo pipefail
export LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export GST_PLUGIN_PATH=/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0:${GST_PLUGIN_PATH:-}
export GST_REGISTRY_FORK=no
export XDG_RUNTIME_DIR=/tmp/wl-rt; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export WAYLAND_DISPLAY=wayland-1
SOCK="$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY"
unset DISPLAY
NV="env GBM_BACKEND=nvidia-drm __GLX_VENDOR_LIBRARY_NAME=nvidia __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json"

echo "== find the NVENC encoder element =="
ENC=""
for e in nvh264enc nvcudah264enc nvautogpuh264enc nvv4l2h264enc; do
  if gst-inspect-1.0 "$e" >/dev/null 2>&1; then ENC="$e"; break; fi
done
[ -n "$ENC" ] || { echo "FAIL: no NVENC element found (need gstreamer1.0-plugins-bad + libnvidia-encode)"; gst-inspect-1.0 2>/dev/null | grep -i nvenc; exit 1; }
echo "encoder: $ENC"
gst-inspect-1.0 "$ENC" 2>/dev/null | grep -iE "bitrate|preset|tune|rc-mode|gop|Long-name" | head

OUT=/root/desktopia/test.mp4
rm -f "$SOCK" "$OUT"
echo "== compositor -> $ENC -> mp4 (12s) =="
timeout -s INT 12 gst-launch-1.0 -e -q \
  waylanddisplaysrc ! video/x-raw,width=1280,height=720,format=RGBx,framerate=30/1 \
  ! videoconvert ! "$ENC" bitrate=8000 ! h264parse ! mp4mux ! filesink location="$OUT" \
  >/tmp/stream-gst.log 2>&1 &
GSTPID=$!
for i in $(seq 1 60); do [ -S "$SOCK" ] && break; sleep 0.25; done
[ -S "$SOCK" ] || { echo "FAIL: no compositor socket"; tail -n 25 /tmp/stream-gst.log; kill "$GSTPID" 2>/dev/null; exit 1; }
echo "compositor up; starting Xwayland + glxgears content"
$NV Xwayland :2 -geometry 1280x720 >/tmp/stream-x.log 2>&1 & XPID=$!
for i in $(seq 1 40); do [ -e /tmp/.X11-unix/X2 ] && break; sleep 0.25; done
DISPLAY=:2 $NV glxgears >/tmp/stream-gears.log 2>&1 & GEARSPID=$!

wait "$GSTPID" 2>/dev/null     # ends at the 12s timeout with EOS so mp4 finalizes
kill "$XPID" "$GEARSPID" 2>/dev/null; wait 2>/dev/null

echo "== result =="
if [ -s "$OUT" ]; then
  echo "wrote $OUT ($(stat -c%s "$OUT") bytes)"
  (command -v ffprobe >/dev/null && ffprobe -hide_banner "$OUT" 2>&1 | grep -iE "Duration|Stream.*Video|h264") || echo "(ffprobe not installed)"
  echo "PASS: NVENC encode works. Pull + play:  make pull REMOTE=$OUT"
else
  echo "FAIL: no/empty mp4. gst log:"; tail -n 30 /tmp/stream-gst.log
  exit 1
fi
