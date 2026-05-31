#!/usr/bin/env bash
# gst-wayland-display (Smithay) needs libwayland >= 1.23 (symbol wl_client_set_max_buffer_size),
# but Ubuntu 24.04 ships 1.22. Build a newer libwayland into /usr/local and verify the plugin
# loads against it. SONAME is unchanged (libwayland-server.so.0) and the ABI is forward-compatible,
# so no plugin rebuild is needed -- we just provide the newer lib at runtime via LD_LIBRARY_PATH.
# System services keep using the distro 1.22; only our pipeline points at /usr/local.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  meson ninja-build pkg-config git libffi-dev libexpat1-dev libxml2-dev

SRC=/opt/wayland
[ -d "$SRC/.git" ] || git clone https://gitlab.freedesktop.org/wayland/wayland.git "$SRC"
cd "$SRC"
git fetch --tags --quiet
TAG=$(git tag | grep -E '^1\.(2[3-9]|[3-9][0-9])\.[0-9]+$' | sort -V | tail -1)
echo "== building libwayland $TAG (>=1.23 for wl_client_set_max_buffer_size) =="
git checkout --quiet "$TAG"
rm -rf build
meson setup build --prefix=/usr/local -Ddocumentation=false -Dtests=false >/dev/null
ninja -C build
ninja -C build install
ldconfig

WLLIB=$(find /usr/local -name 'libwayland-server.so.0' 2>/dev/null | head -1)
WLDIR=$(dirname "$WLLIB")
echo "== installed: $WLLIB =="
pkg-config --modversion wayland-server 2>/dev/null || true
strings "$WLLIB" | grep -q wl_client_set_max_buffer_size \
  && echo "symbol wl_client_set_max_buffer_size: PRESENT" \
  || echo "symbol still MISSING (tag too old?)"

echo "== verify the plugin loads against the new libwayland =="
export LD_LIBRARY_PATH="$WLDIR:${LD_LIBRARY_PATH:-}"
export GST_REGISTRY_FORK=no
export GST_PLUGIN_PATH="/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0:/usr/local/lib/gstreamer-1.0:${GST_PLUGIN_PATH:-}"
echo "(runtime env our pipelines must use: LD_LIBRARY_PATH includes $WLDIR)"
gst-inspect-1.0 waylanddisplaysrc 2>&1 | sed -n '1,80p'
