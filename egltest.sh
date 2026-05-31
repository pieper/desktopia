#!/usr/bin/env bash
# Confirm HARDWARE OpenGL via EGL with NO X server and NO DRM master -- the VirtualGL-free,
# GPU-Xorg-free path. Needs only /dev/nvidia* + /dev/dri/renderD128 (both present on vast).
# PASS = an EGL device reports an NVIDIA GL renderer. Run: make egltest
set -uo pipefail

if ! command -v eglinfo >/dev/null; then
  echo "installing mesa-utils-extra (eglinfo)..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq mesa-utils-extra >/dev/null 2>&1 || true
fi

echo "== EGL device platform (surfaceless, no X) =="
if command -v eglinfo >/dev/null; then
  # eglinfo enumerates platforms; the "Device platform" section is the headless GPU path.
  OUT=$(eglinfo 2>/dev/null)
  echo "$OUT" | grep -iE "Device platform|EGL_EXT_platform_device|EGL_MESA_platform|device .*:|OpenGL (vendor|renderer|core)" \
    | grep -iE "platform|renderer|vendor|device" | head -40
  echo "-- renderer lines --"
  REND=$(echo "$OUT" | grep -iE "OpenGL.*renderer")   # eglinfo prints "OpenGL core profile renderer:"
  echo "$REND"
  if echo "$REND" | grep -qi "nvidia"; then
    echo "PASS: hardware NVIDIA GL via EGL, no X / no DRM master needed. This is the render path."
    exit 0
  elif echo "$REND" | grep -qi "llvmpipe" && ! echo "$REND" | grep -qi "nvidia"; then
    echo "WARN: only llvmpipe shown — check that libEGL_nvidia is injected (graphics cap)."
  fi
else
  echo "eglinfo unavailable; would need a VTK/Python EGL probe instead."
fi

echo "== injected EGL libs (should include nvidia) =="
ls /usr/lib/x86_64-linux-gnu/libEGL_nvidia.so* 2>/dev/null \
  && echo "libEGL_nvidia present" || echo "libEGL_nvidia MISSING (no graphics cap -> EGL would be software)"
echo "(If eglinfo didn't clearly show NVIDIA but libEGL_nvidia is present, a direct VTK EGL"
echo " render is the definitive next check — Slicer/VTK is our actual EGL consumer.)"
