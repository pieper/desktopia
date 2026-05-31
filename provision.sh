#!/usr/bin/env bash
# Install Desktopia's runtime dependencies.
#   - run by the Dockerfile at build time (Phase 2 image)
#   - run by `make provision` on a bare nvidia/cuda vast.ai instance (Phase 1 debugging)
# Single source of truth for deps so the live-debug box and the baked image never drift.
#
# GLVND dispatch ONLY — the NVIDIA GL/EGL/encode libs and the X driver module are injected
# by the NVIDIA Container Toolkit at runtime (needs NVIDIA_DRIVER_CAPABILITIES=all). Do NOT
# install nvidia-driver-* here; a userspace driver mismatches the host kernel module and
# breaks GLX/NVENC, and Mesa's libgl1-mesa-glx can shadow the injected libGLX_nvidia.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
    libglvnd0 libgl1 libglx0 libegl1 libgles2 \
    xserver-xorg-core xinit x11-xserver-utils \
    mesa-utils \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-x \
    gir1.2-gst-plugins-base-1.0 gir1.2-gstreamer-1.0 python3-gi python3-gst-1.0 \
    python3 python3-pip openssl ca-certificates
rm -rf /var/lib/apt/lists/*

pip3 install --break-system-packages "aioquic>=1.0" cryptography

echo "provision.sh: done"
