#!/usr/bin/env bash
# Desktopia finale — ROOT phase: install deps, create the unprivileged 'user' account, then
# hand off to session-wayland.sh which runs the whole desktop+stream AS that user.
# Run on the box: make stream
set -uo pipefail
cd "$(dirname "$0")"
export DEBIAN_FRONTEND=noninteractive

# --- vast ssh-mode (onstart): vast injects our SSH key into /root/.ssh, but some bases leave it
# with perms sshd refuses ("bad ownership or modes for /root/.ssh/authorized_keys"). Fix it
# repeatedly for the first minute (vast may write the key slightly after onstart begins). No-op in
# the dev flow / where /root/.ssh is absent. ---
( for _ in $(seq 1 20); do
    [ -d /root/.ssh ] && { chmod 700 /root/.ssh; chmod 600 /root/.ssh/authorized_keys; chmod go-w /root; } 2>/dev/null
    sleep 3
  done ) &

# --- kill leftovers from a previous/crashed session so this run starts clean ---
pkill -f session-wayland.sh   2>/dev/null || true
pkill -f 'server.py --cert'   2>/dev/null || true
pkill -x Xwayland             2>/dev/null || true
pkill -x openbox              2>/dev/null || true
pkill -f '/opt/Slicer-'       2>/dev/null || true
sleep 1

# --- deps (root) ---
need=()
gst-inspect-1.0 x264enc >/dev/null 2>&1 || need+=(gstreamer1.0-plugins-ugly gstreamer1.0-libav)
command -v openbox       >/dev/null 2>&1 || need+=(openbox)
command -v wmctrl        >/dev/null 2>&1 || need+=(wmctrl)
command -v xterm         >/dev/null 2>&1 || need+=(xterm)
command -v obconf        >/dev/null 2>&1 || need+=(obconf)
command -v vulkaninfo    >/dev/null 2>&1 || need+=(vulkan-tools libvulkan1)
command -v sudo          >/dev/null 2>&1 || need+=(sudo)
command -v gcc           >/dev/null 2>&1 || need+=(gcc)
python3 -c 'import Xlib'  2>/dev/null     || need+=(python3-xlib)
if [ ${#need[@]} -gt 0 ]; then apt-get update -qq; apt-get install -y --no-install-recommends "${need[@]}" >/dev/null 2>&1; fi
python3 -c 'import aioquic' 2>/dev/null || pip3 install --break-system-packages "aioquic>=1.0" >/dev/null 2>&1

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

# --- unprivileged desktop user with passwordless sudo ---
id user >/dev/null 2>&1 || useradd -m -s /bin/bash -U user 2>/dev/null || useradd -m -s /bin/bash user
# The GPU render node's group varies per host (render / video / netdev on vast); add 'user' to
# whatever group actually owns it, or the headless compositor can't open it (empty DMA formats).
RNODE_GRPS=$(ls /dev/dri/renderD* 2>/dev/null | xargs -r -n1 stat -c %G 2>/dev/null | sort -u | paste -sd, -)
usermod -aG "sudo,video,render,audio${RNODE_GRPS:+,$RNODE_GRPS}" user 2>/dev/null || true
echo 'user ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/desktopia-user; chmod 440 /etc/sudoers.d/desktopia-user

# --- stage the scripts where 'user' can read them (avoids /root being root-only) ---
RUN_DIR=/home/user/desktopia
mkdir -p "$RUN_DIR"; cp -rf "$PWD/." "$RUN_DIR/" 2>/dev/null || true; chown -R user "$RUN_DIR"

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

# --- 3D Slicer on demand: the image doesn't bake it (it's a ~5 s download). Fetch into /opt
# (where session-wayland.sh looks for it) if absent; the curl|tar pipe overlaps download+extract. ---
if ! ls -d /opt/Slicer-*/ >/dev/null 2>&1; then
  echo "fetching 3D Slicer..."
  mkdir -p /opt
  curl -L --retry 3 "https://download.slicer.org/download?os=linux&stability=release" \
    | tar -xz -C /opt 2>/dev/null || echo "Slicer fetch failed (session will show glxgears)"
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
chown user "$CERT" "$KEY" 2>/dev/null || true

# --- run the session as 'user' from the staged copy (keeps the TTY for make stream / Ctrl-C) ---
exec sudo -u user -H \
  DESKTOPIA_PRELOAD="$PRELOAD" DESKTOPIA_CERT="$CERT" DESKTOPIA_KEY="$KEY" \
  bash "$RUN_DIR/session-wayland.sh"
