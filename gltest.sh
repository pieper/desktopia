#!/usr/bin/env bash
# Sharp edge #1 go/no-go: can we start an NVIDIA-backed Xorg in this container and get a
# HARDWARE GL context? Run on a provisioned instance (vast.ai): `make gltest`.
# PASS = glxinfo reports an NVIDIA renderer. FAIL = llvmpipe (software) or Xorg won't start.
set -uo pipefail

echo "== nvidia-smi =="
nvidia-smi --query-gpu=name,driver_version,pci.bus_id --format=csv,noheader || {
  echo "FAIL: nvidia-smi unavailable — GPU/toolkit not injected. Check NVIDIA_VISIBLE_DEVICES."; exit 1; }

echo "== capabilities =="
echo "NVIDIA_DRIVER_CAPABILITIES=${NVIDIA_DRIVER_CAPABILITIES:-<unset>}  (need graphics,display,video)"
ls /usr/lib/x86_64-linux-gnu/libGLX_nvidia.so* 2>/dev/null \
  && echo "libGLX_nvidia present (graphics cap injected)" \
  || echo "WARN: libGLX_nvidia.so not found — 'graphics' capability likely missing -> llvmpipe"
ls /usr/lib/xorg/modules/drivers/nvidia_drv.so 2>/dev/null \
  && echo "nvidia_drv.so present (display cap injected)" \
  || echo "WARN: nvidia_drv.so not found — 'display' capability likely missing -> Xorg can't load nvidia"

RAW=$(nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader | head -n1 | tr -d ' ')
BUS=$(echo "$RAW" | awk -F: '{printf "PCI:%d:%d:%d", strtonum("0x"$2), strtonum("0x"$3), strtonum("0x"substr($4,1,index($4,".")-1))}')
echo "== xorg BusID: $BUS =="

cat > /etc/X11/xorg.conf <<EOF
Section "ServerLayout"
    Identifier "layout"
    Screen 0 "screen0"
EndSection
Section "Device"
    Identifier "nvidia"
    Driver     "nvidia"
    BusID      "$BUS"
    Option     "AllowEmptyInitialConfiguration" "true"
EndSection
Section "Screen"
    Identifier "screen0"
    Device     "nvidia"
    DefaultDepth 24
    SubSection "Display"
        Depth   24
        Virtual 1920 1080
    EndSubSection
EndSection
EOF

echo "== starting Xorg :0 =="
Xorg :0 -noreset -nolisten tcp -novtswitch vt1 > /tmp/xorg.log 2>&1 &
XPID=$!
for i in $(seq 1 50); do [ -e /tmp/.X11-unix/X0 ] && break; sleep 0.2; done

if [ ! -e /tmp/.X11-unix/X0 ]; then
  echo "FAIL: Xorg did not start. Last log lines:"; tail -n 25 /tmp/xorg.log
  echo "Fallbacks: headless Wayland (cage/wlroots)+pipewiresrc, or EGL offscreen (no X)."
  kill "$XPID" 2>/dev/null; exit 1
fi

export DISPLAY=:0
echo "== glxinfo =="
RENDERER=$(glxinfo 2>/dev/null | grep -i "OpenGL renderer" || true)
VENDOR=$(glxinfo 2>/dev/null | grep -i "OpenGL vendor" || true)
echo "$VENDOR"; echo "$RENDERER"

kill "$XPID" 2>/dev/null

if echo "$RENDERER" | grep -qi "nvidia"; then
  echo "PASS: hardware NVIDIA GL context in-container. Xorg path is GO."
  exit 0
elif echo "$RENDERER" | grep -qi "llvmpipe"; then
  echo "FAIL: llvmpipe (software). GLVND is routing to Mesa, not the injected NVIDIA libs."
  echo "Check NVIDIA_DRIVER_CAPABILITIES=all and that no libgl1-mesa-glx shadows libGLX_nvidia."
  exit 1
else
  echo "FAIL: no usable GL renderer (X started but glxinfo failed). See /tmp/xorg.log."
  exit 1
fi
