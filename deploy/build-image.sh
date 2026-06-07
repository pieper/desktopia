#!/usr/bin/env bash
# Build-time install for the NRP/Kubernetes image (deploy/Dockerfile.nrp): bake ALL runtime deps +
# the compositor + 3D Slicer + the unprivileged 'user' into a vanilla ubuntu:24.04, so the
# container starts fast with no runtime downloads. The NVIDIA driver itself is injected at runtime by
# the container runtime (NVIDIA_DRIVER_CAPABILITIES=all) -- only the GLVND dispatch libs are baked.
#
# Keep the apt list in sync with entrypoint-wayland.sh's `need` array (the entrypoint still guards/skips
# at runtime, so a baked image just no-ops those checks).
set -eux
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  curl ca-certificates tar python3 python3-pip sudo gcc \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-tools gstreamer1.0-x \
  python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 python3-xlib \
  xwayland openbox wmctrl xwallpaper x11-xserver-utils xterm obconf pcmanfm xclip \
  vulkan-tools libvulkan1 \
  libgbm1 libdrm2 libinput10 libseat1 libxkbcommon0 libdisplay-info-dev libegl1 libgles2 \
  libgl1 libglx0 libglvnd0 libopengl0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
  libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0 libxcb-cursor0 \
  libxcb-util1 libglu1-mesa libodbc2 libpq5 libpulse-mainloop-glib0 libpcre2-16-0
pip3 install --break-system-packages "aioquic>=1.0" websockets

# Prebuilt compositor (.so + libwayland 1.25) from the public GHCR artifact (same one the vast path
# fetches at runtime). Untars to / (into /usr/local/lib/...).
REPO=${DESKTOPIA_COMPOSITOR_REPO:-pieper/desktopia}; TAG=${DESKTOPIA_COMPOSITOR_TAG:-compositor}
TOK=$(curl -fsSL "https://ghcr.io/token?scope=repository:${REPO}:pull" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
for DIG in $(curl -fsSL -H "Authorization: Bearer $TOK" \
               -H "Accept: application/vnd.oci.image.manifest.v1+json" \
               -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
               "https://ghcr.io/v2/${REPO}/manifests/${TAG}" \
             | python3 -c 'import sys,json
for l in json.load(sys.stdin).get("layers",[]): print(l["digest"])'); do
  curl -fsSL -H "Authorization: Bearer $TOK" "https://ghcr.io/v2/${REPO}/blobs/${DIG}" | tar -xz -C /
done
ldconfig
test -f /usr/local/lib/x86_64-linux-gnu/gstreamer-1.0/libgstwaylanddisplaysrc.so

# 3D Slicer (release) into /opt
mkdir -p /opt
curl -L --retry 3 "https://download.slicer.org/download?os=linux&stability=release" | tar -xz -C /opt
ls -d /opt/Slicer-*/

# Chrome is NOT baked in (kept lean) -- the openbox menu's scripts/chrome-launch.sh installs it on
# first use. See entrypoint-wayland.sh / session-wayland.sh.

# Unprivileged 'user' (real 'user' group, owns its home) + close_range seccomp shim, matching what
# entrypoint-wayland.sh expects so its runtime guards all skip.
id user >/dev/null 2>&1 || useradd -m -s /bin/bash user
getent group user >/dev/null 2>&1 || groupadd user
usermod -g user user || true
mkdir -p /home/user/Data /home/user/Downloads && chown -R user:user /home/user
echo 'user ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/desktopia-user; chmod 440 /etc/sudoers.d/desktopia-user
printf '%s\n' '#define _GNU_SOURCE' '#include <errno.h>' \
  'int close_range(unsigned int a, unsigned int b, int c){ (void)a;(void)b;(void)c; errno=ENOSYS; return -1; }' > /tmp/ncr.c
gcc -shared -fPIC -o /usr/local/lib/noclose_range.so /tmp/ncr.c || true
rm -f /tmp/ncr.c

rm -rf /var/lib/apt/lists/*
echo "desktopia image build complete"
