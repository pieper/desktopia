#!/usr/bin/env bash
# Desktopia finale — ROOT phase: install deps, create the unprivileged 'user' account, then
# hand off to session-wayland.sh which runs the whole desktop+stream AS that user.
# Run on the box: make stream
set -uo pipefail
cd "$(dirname "$0")"
export DEBIAN_FRONTEND=noninteractive

# NOTE: do NOT touch /root/.ssh here. vast injects + maintains the SSH key itself; chmod'ing it
# (or `vastai attach ssh`) races vast's key management and leaves authorized_keys in a state sshd
# rejects ("bad ownership or modes"). Leave it alone and vanilla SSH works.

# --- kill leftovers from a previous/crashed session so this run starts clean ---
pkill -f session-wayland.sh   2>/dev/null || true
pkill -f 'server.py --cert'   2>/dev/null || true
pkill -x Xwayland             2>/dev/null || true
pkill -x openbox              2>/dev/null || true
pkill -f '/opt/Slicer-'       2>/dev/null || true
sleep 1

# --- deps (root) ---
need=()
# Thin-base essentials: vastai/base-image:stock is much smaller than linux-desktop and may lack these.
# The onstart itself uses curl+python3 to fetch the compositor and pip3 for aioquic, so they must land
# in this first apt pass (the desktop image shipped them preinstalled).
command -v curl    >/dev/null 2>&1 || need+=(curl ca-certificates)
command -v tar     >/dev/null 2>&1 || need+=(tar)
command -v python3 >/dev/null 2>&1 || need+=(python3)
command -v pip3    >/dev/null 2>&1 || need+=(python3-pip)
gst-inspect-1.0 x264enc >/dev/null 2>&1 || need+=(gstreamer1.0-plugins-ugly gstreamer1.0-libav)
command -v Xwayland      >/dev/null 2>&1 || need+=(xwayland)
command -v openbox       >/dev/null 2>&1 || need+=(openbox)
command -v wmctrl        >/dev/null 2>&1 || need+=(wmctrl)
command -v xwallpaper    >/dev/null 2>&1 || need+=(xwallpaper)           # set the (CI-rendered) splash PNG (lighter than feh)
command -v xsetroot      >/dev/null 2>&1 || need+=(x11-xserver-utils)    # splash fallback (solid bg)
command -v xterm         >/dev/null 2>&1 || need+=(xterm)
command -v pcmanfm       >/dev/null 2>&1 || need+=(pcmanfm)             # lightweight file manager; drag-drop files into Slicer
command -v xclip         >/dev/null 2>&1 || need+=(xclip)               # explicit clipboard push/pull (X11 CLIPBOARD on :2)
command -v obconf        >/dev/null 2>&1 || need+=(obconf)
command -v vulkaninfo    >/dev/null 2>&1 || need+=(vulkan-tools libvulkan1)
command -v sudo          >/dev/null 2>&1 || need+=(sudo)
command -v gcc           >/dev/null 2>&1 || need+=(gcc)
python3 -c 'import Xlib'  2>/dev/null     || need+=(python3-xlib)
# GStreamer runtime + Python/GI bindings + the prebuilt compositor's shared-lib deps. The old build
# path (provision-wayland.sh) installed these as a side effect; now that we FETCH the prebuilt
# compositor instead of building, install them here or server.py fails ("Namespace Gst not available")
# and waylanddisplaysrc won't load. apt skips whatever the base already has.
python3 -c 'import gi; gi.require_version("Gst","1.0")' 2>/dev/null || need+=(python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0)
gst-inspect-1.0 videoconvert >/dev/null 2>&1 || need+=(gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-tools gstreamer1.0-x)
need+=(libgbm1 libdrm2 libinput10 libseat1 libxkbcommon0 libdisplay-info-dev libegl1 libgles2)
# Slicer is an X11/GLX client: it needs the GLVND GLX dispatch (libGL.so.1 / libGLX.so.0) to reach
# the NVIDIA driver's libGLX_nvidia. The deps above are only the compositor's EGL/GLES. The OLD baked
# image installed these GLX libs; trimming the onstart deps dropped them -> Qt spews
# "composeAndFlush: makeCurrent() failed" and Slicer's window never flushes. (regression fix 2026-06-04)
need+=(libgl1 libglx0 libglvnd0 libopengl0)
# 3D Slicer (Qt5) runtime system libs the fat desktop base preinstalled but stock-ubuntu24.04 lacks.
# The Qt xcb PLATFORM-plugin cluster is mandatory (without it Slicer dies at startup:
# "error while loading shared libraries: libxcb-icccm.so.4" / "could not load the Qt platform plugin
# xcb"); the trailing four (GLU, ODBC/PG SQL plugins, pulse) are optional plugin deps -- found by
# ldd-scanning all 958 Slicer .so. (libhwloc.so.5 is also missing but optional + no 24.04 package.)
need+=(libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0
       libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0 libxcb-cursor0
       libxcb-util1 libglu1-mesa libodbc2 libpq5 libpulse-mainloop-glib0 libpcre2-16-0)
# ^ libpcre2-16-0 is the Slicer LAUNCHER executable's Qt5Core dep (UTF-16 PCRE2); without it the
# launcher dies "libpcre2-16.so.0 not found" before it can even exec SlicerApp-real. (Missed by an
# earlier *.so-only scan because it's an EXECUTABLE dep, not a .so dep.) ldd of all execs + 958 .so
# is now clean except optional libhwloc.so.5 (a perf/TBB dep with no 24.04 package; non-fatal).
# Install straight from the base image's existing package lists (fast); only fall back to a slow
# `apt-get update` if that fails (stale/cleaned lists). The vast base's lists are usually fresh.
if [ ${#need[@]} -gt 0 ]; then
  apt-get install -y --no-install-recommends "${need[@]}" >/dev/null 2>&1 \
    || { apt-get update -qq && apt-get install -y --no-install-recommends "${need[@]}" >/dev/null 2>&1; }
fi
python3 -c 'import aioquic' 2>/dev/null || pip3 install --break-system-packages "aioquic>=1.0" >/dev/null 2>&1
python3 -c 'import websockets' 2>/dev/null || pip3 install --break-system-packages websockets >/dev/null 2>&1   # WS/TCP transport

# --- prebuilt compositor: fetch + extract the gst-wayland-display plugin + libwayland (~10 MB) from
# the public GHCR artifact image instead of building it (~13 min). Skipped if already present (a dev
# box that ran `make wl-setup`). The artifact is a FROM-scratch image whose layers untar to /. ---
COMPOSITOR_SO=/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0/libgstwaylanddisplaysrc.so
if [ ! -f "$COMPOSITOR_SO" ]; then
  echo "fetching prebuilt compositor..."
  REPO=${DESKTOPIA_COMPOSITOR_REPO:-pieper/desktopia}; TAG=${DESKTOPIA_COMPOSITOR_TAG:-compositor}
  TOK=$(curl -fsSL "https://ghcr.io/token?scope=repository:${REPO}:pull" 2>/dev/null \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))' 2>/dev/null)
  for DIG in $(curl -fsSL -H "Authorization: Bearer $TOK" \
                 -H "Accept: application/vnd.oci.image.manifest.v1+json" \
                 -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
                 "https://ghcr.io/v2/${REPO}/manifests/${TAG}" 2>/dev/null \
               | python3 -c 'import sys,json
for l in json.load(sys.stdin).get("layers",[]): print(l["digest"])' 2>/dev/null); do
    curl -fsSL -H "Authorization: Bearer $TOK" "https://ghcr.io/v2/${REPO}/blobs/${DIG}" 2>/dev/null | tar -xz -C / 2>/dev/null
  done
  ldconfig
  [ -f "$COMPOSITOR_SO" ] && echo "compositor installed" || echo "WARN: compositor fetch failed (run 'make wl-setup' to build it)"
fi

# --- unprivileged desktop user with passwordless sudo. The stock base ships a half-baked 'user'
# acct whose PRIMARY group is root (gid 0) -> /home/user ends up group-root and HOME-based writes
# (settings, ~/Data) misbehave. Force a real 'user' group and let 'user' OWN its home. ---
id user >/dev/null 2>&1 || useradd -m -s /bin/bash user
getent group user >/dev/null 2>&1 || groupadd user
usermod -g user user 2>/dev/null || true                 # primary group 'user', NOT root(0)
mkdir -p /home/user && chown -R user:user /home/user
# The GPU render node's group varies per host (render / video / netdev on vast); add 'user' to
# whatever group actually owns it, or the headless compositor can't open it (empty DMA formats).
RNODE_GRPS=$(ls /dev/dri/renderD* 2>/dev/null | xargs -r -n1 stat -c %G 2>/dev/null | sort -u | paste -sd, -)
usermod -aG "sudo,video,render,audio${RNODE_GRPS:+,$RNODE_GRPS}" user 2>/dev/null || true
# On some hosts (NRP/K8s) the GPU render node has a numeric GID with no named group in the container,
# so the group-add above can't grant it (compositor then gets EACCES on /dev/dri/renderD*). We run as
# root here (before dropping to 'user'), so just make the device nodes world-rw — robust everywhere,
# harmless on hosts where the group already worked (vast). sudo -u user drops supplemental groups, so a
# k8s supplementalGroups wouldn't survive it anyway; chmod on the node does.
chmod a+rw /dev/dri/renderD* /dev/dri/card* 2>/dev/null || true
echo 'user ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/desktopia-user; chmod 440 /etc/sudoers.d/desktopia-user

# --- stage the scripts where 'user' can read them (avoids /root being root-only) ---
RUN_DIR=/home/user/desktopia
mkdir -p "$RUN_DIR"; cp -rf "$PWD/." "$RUN_DIR/" 2>/dev/null || true; chown -R user:user "$RUN_DIR"

# --- Chrome: no sign-in prompts / promos (managed policy applies to every launch) ---
mkdir -p /etc/opt/chrome/policies/managed
cat > /etc/opt/chrome/policies/managed/desktopia.json <<'EOF'
{
  "BrowserSignin": 0,
  "SyncDisabled": true,
  "PromotionalTabsEnabled": false,
  "BrowserAddPersonEnabled": false,
  "MetricsReportingEnabled": false,
  "DefaultBrowserSettingEnabled": false
}
EOF

# --- close_range() shim: vast seccomp denies close_range with EPERM, breaking GLib g_spawn
# (openbox menu -> "Failed to close file descriptor"). Make it report ENOSYS so GLib falls back. ---
PRELOAD=/usr/local/lib/noclose_range.so
if [ ! -f "$PRELOAD" ] && command -v gcc >/dev/null 2>&1; then
  printf '%s\n' '#define _GNU_SOURCE' '#include <errno.h>' \
    'int close_range(unsigned int a, unsigned int b, int c){ (void)a;(void)b;(void)c; errno=ENOSYS; return -1; }' > /tmp/ncr.c
  gcc -shared -fPIC -o "$PRELOAD" /tmp/ncr.c 2>/dev/null || true
fi

# --- 3D Slicer: fetch into /opt in the BACKGROUND so the desktop + QUIC stream come up immediately.
# session-wayland.sh shows a "Loading 3D Slicer..." splash (the logo on the X root) and launches
# Slicer the moment the download lands, so the browser gets a page right away. ---
if ! ls -d /opt/Slicer-*/ >/dev/null 2>&1; then
  ( echo "fetching 3D Slicer..."; mkdir -p /opt
    curl -L --retry 3 "https://download.slicer.org/download?os=linux&stability=release" \
      | tar -xz -C /opt && echo "Slicer ready" || echo "Slicer fetch failed" ) >/tmp/slicer-fetch.log 2>&1 &
fi

# --- Google Chrome: the thin base ships only a chromium SNAP stub (won't run in-container); the old
# desktop base bundled real Chrome. Install the .deb in the background, but WAIT until Slicer is up
# first (user: don't slow Slicer for Chrome) so the Slicer download + first render get all the
# bandwidth/CPU. This subshell is backgrounded before the exec below, so it outlives it and gates on
# the SlicerApp process that session-wayland.sh launches. Menu "Google Chrome" works once it lands. ---
if ! command -v google-chrome >/dev/null 2>&1; then
  ( for _ in $(seq 1 600); do pgrep -f SlicerApp-real >/dev/null 2>&1 && break; sleep 2; done
    sleep 10                                 # let Slicer settle past its heavy startup render
    echo "installing google-chrome..."
    curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
      && apt-get install -y --no-install-recommends /tmp/chrome.deb && rm -f /tmp/chrome.deb \
      && echo "chrome ready" || echo "chrome install failed" ) >/tmp/chrome-install.log 2>&1 &
fi

# --- persistent WebTransport cert (ECDSA P-256, <=14d). Migrate the old /root cert so the
# pasted hash stays stable; regenerate only when missing/near-expiry. Owned by 'user'. ---
CERT=/home/user/desktopia-cert.pem; KEY=/home/user/desktopia-key.pem
if [ ! -f "$CERT" ] && [ -f /root/desktopia-cert.pem ]; then
  cp /root/desktopia-cert.pem "$CERT"; cp /root/desktopia-key.pem "$KEY"
fi
if [ ! -f "$CERT" ] || ! openssl x509 -in "$CERT" -checkend 86400 >/dev/null 2>&1; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout "$KEY" -out "$CERT" -days 13 -nodes -subj "/CN=desktopia" 2>/dev/null
fi
chown user:user "$CERT" "$KEY" 2>/dev/null || true

# --- run the session as 'user' from the staged copy (keeps the TTY for make stream / Ctrl-C).
# Pass HOME=/home/user explicitly: -H alone proved unreliable on this base (apps fell back to /root). ---
exec sudo -u user -H \
  HOME=/home/user DESKTOPIA_PRELOAD="$PRELOAD" DESKTOPIA_CERT="$CERT" DESKTOPIA_KEY="$KEY" \
  bash "$RUN_DIR/session-wayland.sh"
