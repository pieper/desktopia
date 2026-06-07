#!/usr/bin/env bash
# Real-Slicer smoke test for the SOFTWARE path: build an amd64 image (so the x86_64-only Slicer runs;
# on an Apple-silicon Mac this uses Rosetta under Colima), launch Slicer under Xvfb + Mesa llvmpipe,
# load a sample volume, and grab a screenshot to OUT/ so you can eyeball that it actually rendered.
# This is the no-GPU rendering proof that `make soft-local` (stub client) doesn't cover.
#
#   scripts/amd64-slicer-test.sh         # build + run; writes screenshots/logs to ./.amd64-out
#
# Heavy + slow under emulation (Slicer download + Qt startup). Expect several minutes.
#
# !!! Apple Silicon caveat: this WON'T run Slicer under Colima's Rosetta. Slicer's launcher re-execs a
# child (SlicerApp-real) and Rosetta fails it with "failed to open elf at /lib64/ld-linux-x86-64.so.2".
# llvmpipe itself works under emulation (see glxinfo.txt), but real Slicer needs NATIVE amd64 -- run this
# on a real x86_64 Linux host, or validate Slicer on Colab (colab/desktopia_colab.ipynb). Pure-QEMU
# Colima (`colima start --vm-type qemu`, no Rosetta) may run it, but very slowly.
set -euo pipefail
cd "$(dirname "$0")/.."

CONTAINER=${CONTAINER:-docker}
# Default OUT under the repo: Colima/Lima shares /Users by default but NOT arbitrary /tmp, so a /tmp
# bind mount silently writes into the VM instead of reaching the Mac. Keep it under the project tree.
OUT=${OUT:-$PWD/.amd64-out}; mkdir -p "$OUT"
IMG=desktopia-amd64-slicer-test
W=${W:-1280}; H=${H:-720}

echo "[1/2] building amd64 image (Slicer baked in -- slow first time)..."
"$CONTAINER" build --platform linux/amd64 -t "$IMG" -f - . <<'DOCKERFILE'
FROM --platform=linux/amd64 ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
# Xvfb + Mesa llvmpipe + Slicer's Qt/xcb runtime deps + imagemagick (screenshot) -- mirrors the
# software-mode deps from entrypoint-wayland.sh.
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb libgl1-mesa-dri mesa-utils x11-apps imagemagick curl ca-certificates tar \
      libgl1 libglx0 libglvnd0 libopengl0 libglu1-mesa \
      libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
      libxcb-sync1 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0 libxcb-cursor0 \
      libxcb-util1 libodbc2 libpq5 libpulse-mainloop-glib0 libpcre2-16-0 \
 && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt && curl -L --retry 3 \
      "https://download.slicer.org/download?os=linux&stability=release" | tar -xz -C /opt \
 && ls -d /opt/Slicer-*/
DOCKERFILE

echo "[2/2] running Slicer under Xvfb+llvmpipe, capturing screenshot to $OUT ..."
# (no --platform on run: the image is already amd64 via FROM --platform; passing it again makes
#  Docker try to PULL a platform-tagged image instead of using the local build.)
"$CONTAINER" run --rm -v "$OUT:/out" -e W="$W" -e H="$H" "$IMG" bash -c '
  set -e
  export DISPLAY=:2 LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe \
         QT_QPA_PLATFORM=xcb HOME=/root
  Xvfb :2 -screen 0 ${W}x${H}x24 +extension GLX +render -noreset >/out/xvfb.log 2>&1 &
  for i in $(seq 1 80); do [ -e /tmp/.X11-unix/X2 ] && break; sleep 0.25; done
  echo "Xvfb up; GL renderer check:"; (glxinfo -B 2>/dev/null | grep -iE "renderer|opengl" || true) | tee /out/glxinfo.txt
  SDIR=$(ls -d /opt/Slicer-*/ | head -1); echo "Slicer: $SDIR"
  # Load a sample volume, lay out the standard 4-up view, render, and save a screenshot of the app
  # window (proves the slice + 3D actually rasterized under llvmpipe), plus an X-root capture as backup.
  "$SDIR/Slicer" --no-splash --python-code "
import slicer, SampleData
slicer.app.layoutManager().setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
SampleData.SampleDataLogic().downloadMRHead()
for _ in range(20): slicer.app.processEvents()
slicer.util.mainWindow().showMaximized()
for _ in range(20): slicer.app.processEvents()
slicer.app.mainWindow().grab().save(\"/out/slicer-app.png\")
print(\"SCREENSHOT_SAVED\", flush=True)
slicer.util.quit()
" >/out/slicer.log 2>&1 || echo "Slicer exited non-zero (see /out/slicer.log)"
  import -window root /out/slicer-root.png 2>/dev/null || true
  echo "outputs:"; ls -l /out
'
echo "done. screenshots + logs in $OUT"
if grep -q "rosetta error" "$OUT/slicer.log" 2>/dev/null; then
  echo
  echo ">>> Slicer hit a ROSETTA limitation (re-exec of SlicerApp-real), not a software-path bug."
  echo ">>> llvmpipe works (see $OUT/glxinfo.txt). Validate real Slicer on native amd64 / Colab."
fi
