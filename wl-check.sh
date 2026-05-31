#!/usr/bin/env bash
# Locate the installed gst-wayland-display plugin and inspect it directly (no registry
# scanner), revealing the real element name + properties. Run after wl-build: make wl-check.
set -uo pipefail
export GST_REGISTRY_FORK=no   # scan in-process; avoids "External plugin loader failed"

echo "== locate installed plugin .so (cargo-c may use a multiarch libdir) =="
mapfile -t SOS < <(find /usr/local /usr/lib /root/.local /root/.cargo -iname 'libgst*wayland*display*.so' 2>/dev/null | sort -u)
if [ "${#SOS[@]}" -eq 0 ]; then
  echo "  none found; widening search to any new gstreamer plugin under /usr/local:"
  find /usr/local -path '*gstreamer-1.0*' -name '*.so' 2>/dev/null
fi
printf '  %s\n' "${SOS[@]:-<none>}"

echo "== inspect each plugin file directly (shows element name + properties) =="
for so in "${SOS[@]:-}"; do
  [ -e "$so" ] || continue
  echo "--- gst-inspect-1.0 $so ---"
  gst-inspect-1.0 "$so" 2>&1 | sed -n '1,60p'
done

echo "== try element by name across likely plugin paths =="
export GST_PLUGIN_PATH="/usr/local/lib/gstreamer-1.0:/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0:${GST_PLUGIN_PATH:-}"
for name in waylanddisplaysrc waylandsrc waylanddisplay; do
  echo "--- gst-inspect-1.0 $name ---"
  gst-inspect-1.0 "$name" 2>&1 | sed -n '1,40p'
done
