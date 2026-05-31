#!/usr/bin/env bash
set -euo pipefail

# xorg.conf is generated at boot because the PCI BusID differs per rented host.
# nvidia-smi is injected by the NVIDIA Container Toolkit, so we derive it from there.

# --- derive Xorg BusID from nvidia-smi (00000000:C1:00.0 -> PCI:193:0:0) ---
# Pure bash (no gawk strtonum; Ubuntu's default awk is mawk). bash printf accepts 0x.. hex.
RAW=$(nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader | head -n1 | tr -d ' ')
rest=${RAW#*:}; bus=${rest%%:*}; df=${rest#*:}; dev=${df%%.*}; func=${df#*.}
BUS=$(printf "PCI:%d:%d:%d" "0x$bus" "0x$dev" "0x$func")

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

# --- start the GPU X server (see README "sharp edges" re: VT/permissions on vast.ai) ---
Xorg :0 -noreset -nolisten tcp -novtswitch vt1 &
for i in $(seq 1 50); do [ -e /tmp/.X11-unix/X0 ] && break; sleep 0.2; done
export DISPLAY=:0

# Sanity: must print "NVIDIA ...", NOT "llvmpipe". If llvmpipe, GLVND routing is wrong.
glxinfo | grep -i "OpenGL renderer" || echo "WARN: glxinfo failed"

# --- workload: swap glxgears for Slicer once the pipeline is proven ---
glxgears -fullscreen &

# --- self-signed ECDSA cert, <=14 days, for WebTransport serverCertificateHashes ---
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout /tmp/key.pem -out /tmp/cert.pem -days 13 -nodes -subj "/CN=stream" 2>/dev/null

echo "=================================================================="
echo -n "CERT_SHA256_BASE64="
openssl x509 -in /tmp/cert.pem -outform der | openssl dgst -sha256 -binary | base64
echo "  (paste this into the browser client's serverCertificateHashes)"
echo "=================================================================="

exec python3 /opt/stream/server.py --cert /tmp/cert.pem --key /tmp/key.pem --port 4433
