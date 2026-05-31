#!/usr/bin/env bash
# Build + install gst-wayland-display (Games-on-Whales): a Smithay headless Wayland
# compositor exposed as the GStreamer element `waylanddisplaysrc`. Hardware GL via the
# render node /dev/dri/renderD128 -- no X, no DRM master. Run on a vast instance AFTER
# `make egltest` confirms hardware EGL. First-cut; package set may need tweaks per box.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "== apt deps (GStreamer dev + Smithay/wayland build deps + XWayland for Slicer) =="
apt-get update
apt-get install -y --no-install-recommends \
  build-essential pkg-config git curl ca-certificates \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-tools gstreamer1.0-x \
  libwayland-dev wayland-protocols libxkbcommon-dev \
  libudev-dev libinput-dev libgbm-dev libdrm-dev \
  libegl1-mesa-dev libgles2-mesa-dev libseat-dev libdisplay-info-dev \
  xwayland \
  mesa-utils-extra weston   # eglinfo + weston-simple-egl as Wayland GL test clients

echo "== Rust via rustup (apt's rustc 1.75 is too old for current cargo-c, which needs 1.93) =="
if [ ! -x "$HOME/.cargo/bin/rustc" ] && ! command -v rustup >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
fi
. "$HOME/.cargo/env"
rustc --version

echo "== cargo-c (provides cargo cinstall) =="
command -v cargo-cinstall >/dev/null 2>&1 || cargo install cargo-c

echo "== build + install gst-wayland-display =="
SRC=/opt/gst-wayland-display
[ -d "$SRC/.git" ] || git clone --depth 1 https://github.com/games-on-whales/gst-wayland-display "$SRC"
cd "$SRC"
cargo cinstall --prefix=/usr/local
ldconfig

echo "== verify the element is registered =="
export GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0
gst-inspect-1.0 waylanddisplaysrc | sed -n '1,25p' \
  || { echo "FAIL: waylanddisplaysrc not found — check GST_PLUGIN_PATH and the build log"; exit 1; }
echo "provision-wayland.sh: done — waylanddisplaysrc is available"
