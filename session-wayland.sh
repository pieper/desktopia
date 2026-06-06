#!/usr/bin/env bash
# Desktopia desktop session — runs as the unprivileged 'user' (started by entrypoint-wayland.sh).
# Brings up the GPU Wayland compositor + H.264/QUIC server, then Xwayland + openbox + Slicer.
set -uo pipefail
cd "$(dirname "$0")"

# Force HOME so every app (Slicer settings, pcmanfm, xterm, ~/Data) uses /home/user, not /root.
# The base's `user` acct + sudo -H proved unreliable here. (cwd stays the script dir for server.py.)
export HOME=/home/user

# --- compositor-side env (NOT the NVIDIA GBM env — that is for X clients only) ---
export LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}   # libwayland 1.25
export GST_PLUGIN_PATH=/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0:${GST_PLUGIN_PATH:-}
export GST_REGISTRY_FORK=no
export XDG_RUNTIME_DIR=/tmp/wl-rt-$(id -u); mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export WAYLAND_DISPLAY=wayland-1
SOCK="$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY"
SLICER_DIR=$(ls -d /opt/Slicer-*/ 2>/dev/null | head -1)
CERT=${DESKTOPIA_CERT:-/home/user/desktopia-cert.pem}
KEY=${DESKTOPIA_KEY:-/home/user/desktopia-key.pem}

cleanup() { kill $(jobs -p) 2>/dev/null; }
trap cleanup EXIT INT TERM

# --- compositor pipeline + QUIC server ---
rm -f "$SOCK"
python3 server.py --cert "$CERT" --key "$KEY" --port 4433 ${DESKTOPIA_WS_PLAIN:+--ws-plain} >/tmp/server.log 2>&1 &
for i in $(seq 1 100); do [ -S "$SOCK" ] && break; sleep 0.25; done
if [ ! -S "$SOCK" ]; then echo "FAIL: compositor socket never appeared. server.log:"; tail -n 40 /tmp/server.log; exit 1; fi
echo "compositor + QUIC server up as $(whoami) (socket $SOCK)"

# --- X-client environment, inherited by openbox AND every app it launches ---
export GBM_BACKEND=nvidia-drm
export __GLX_VENDOR_LIBRARY_NAME=nvidia
export __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json
[ -f "${DESKTOPIA_PRELOAD:-}" ] && export LD_PRELOAD="$DESKTOPIA_PRELOAD"   # close_range g_spawn fix
export DISPLAY=:2
ulimit -n 65536 2>/dev/null || true

# --- user folders for downloads / data to drag into Slicer (HOME is /home/user) ---
mkdir -p ~/Data ~/Downloads

# --- openbox menu: Terminal, Files, Chrome, Slicer, WM settings (no exit) ---
mkdir -p ~/.config/openbox
cat > ~/.config/openbox/menu.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_menu xmlns="http://openbox.org/3.4/menu">
  <menu id="root-menu" label="Desktopia">
    <item label="Terminal"><action name="Execute"><command>xterm</command></action></item>
    <item label="Files"><action name="Execute"><command>pcmanfm /home/user</command></action></item>
    <item label="Google Chrome"><action name="Execute"><command>google-chrome --no-sandbox --no-first-run --no-default-browser-check</command></action></item>
    <item label="3D Slicer"><action name="Execute"><command>sh -c 'D=$(ls -d /opt/Slicer-*/ 2>/dev/null | head -1); exec "$D/Slicer" --no-splash'</command></action></item>
    <separator/>
    <item label="Window Manager Settings"><action name="Execute"><command>obconf</command></action></item>
  </menu>
</openbox_menu>
EOF

Xwayland :2 -geometry 1920x1080 >/tmp/xway.log 2>&1 &   # Xwayland IS the compositor's Wayland client
for i in $(seq 1 40); do [ -e /tmp/.X11-unix/X2 ] && break; sleep 0.25; done
# From here, everything is a pure X11 client of :2. Unset WAYLAND_DISPLAY so X11 apps (Slicer,
# Chrome, and esp. wgpu's EGL backend) don't try the Wayland platform and crash (wl_drm BadAccess).
unset WAYLAND_DISPLAY
openbox >/tmp/wm.log 2>&1 &

# --- loading splash (pre-rendered in CI; the box only needs feh): the Slicer logo + "please wait"
# on the X root, shown the instant the desktop is up so a browser that connects sees branded content
# while Slicer is still downloading. We swap to the no-text background once Slicer launches. ---
SPLASH=/usr/local/share/desktopia/splash.png       # logo + "Loading 3D Slicer... please wait"
BG=/usr/local/share/desktopia/background.png        # logo only (steady wallpaper)
setbg() {
  if [ -f "$1" ] && command -v xwallpaper >/dev/null 2>&1; then xwallpaper --zoom "$1" 2>/dev/null
  else xsetroot -solid '#15151f' 2>/dev/null || true; fi
}
setbg "$SPLASH"

# --- launch Slicer as soon as its background download lands (over the splash); the desktop + stream
# are already live by now (the cert is printed below before this returns). Clear the "please wait"
# afterward. Falls back to glxgears if Slicer never arrives. ---
(
  for _ in $(seq 1 150); do
    SDIR=$(ls -d /opt/Slicer-*/ 2>/dev/null | head -1)
    [ -n "$SDIR" ] && [ -x "$SDIR/Slicer" ] && break
    sleep 2
  done
  if [ -n "${SDIR:-}" ] && [ -x "$SDIR/Slicer" ]; then
    "$SDIR/Slicer" --no-splash >/tmp/slicer.log 2>&1 &
    sleep 8; wmctrl -r :ACTIVE: -b add,maximized_vert,maximized_horz 2>/dev/null || true
    setbg "$BG"
  else
    glxgears >/tmp/glxgears.log 2>&1 &
  fi
) &

echo "=================================================================="
echo -n "CERT_SHA256_BASE64="
openssl x509 -in "$CERT" -outform der | openssl dgst -sha256 -binary | base64
echo "Now: 'make port' for the public IP:PORT, paste both into client/index.html, open in Chrome."
echo "(logs: /tmp/server.log /tmp/slicer.log /tmp/xway.log)"
echo "=================================================================="

# NRP/appliance: serve the client page + a websocket-transport status.json so a browser can just open
# the ingress URL (the page connects over WSS through the ingress to server.py's --ws-plain WS port).
if [ -n "${DESKTOPIA_HTTP_PORT:-}" ]; then
  printf '{"ready":true,"transport":"websocket"}' > client/status.json 2>/dev/null || true
  python3 -m http.server "$DESKTOPIA_HTTP_PORT" --directory client >/tmp/httpd.log 2>&1 &
fi
wait
