#!/usr/bin/env bash
# Probe a running vastai/linux-desktop instance to learn how to capture+stream it.
# Run ON the instance (e.g. DESKTOPIA_INSTANCE=<id> make inspect). Read-only; installs nothing.
#
# The make-or-break question: is the desktop HARDWARE GL (NVIDIA renderer on a GPU Xorg) or
# SOFTWARE (llvmpipe via Xvfb/VNC)? Software would defeat the hardware-accel goal.
set -uo pipefail
echo "===== OS / GPU ====="
. /etc/os-release 2>/dev/null && echo "os: ${PRETTY_NAME:-?}"
nvidia-smi -L 2>/dev/null || echo "no nvidia-smi"
echo "NVIDIA_DRIVER_CAPABILITIES=${NVIDIA_DRIVER_CAPABILITIES:-<unset>}"

echo; echo "===== display servers / desktop ====="
ls -1 /tmp/.X11-unix/ 2>/dev/null | sed 's/^X/  display :/' || echo "  no X sockets"
for p in Xorg Xvfb Xtigervnc vncserver x11vnc tigervnc kwin_x11 xfwm4 gnome-shell mutter labwc sway cage weston; do
  pgrep -a "$p" 2>/dev/null
done
echo "shell DISPLAY=${DISPLAY:-<unset>}"

echo; echo "===== Selkies / vast desktop services (the stack we'd reuse) ====="
pgrep -af -i "selkies|webrtc|virtualgl|vglrun|pulseaudio" 2>/dev/null || echo "  no selkies/webrtc/vgl processes"
echo "  vast desktop ports expected: 5900 VNC, 6100 Selkies-WebRTC, 6200 noVNC"
command -v vglrun >/dev/null && echo "  VirtualGL present: $(vglrun 2>&1 | head -1)" || echo "  VirtualGL (vglrun) not found"
# If Selkies runs a GStreamer pipeline, capture its command line to fork NVENC settings from:
pgrep -af gst-launch 2>/dev/null
ps -eo args 2>/dev/null | grep -i "selkies\|nvh264\|nvenc\|ximagesrc" | grep -v grep | head

echo; echo "===== GL renderer per display (the decisive bit) ====="
command -v glxinfo >/dev/null || echo "  glxinfo not installed (mesa-utils); install to confirm"
for d in 0 1 2; do
  [ -e "/tmp/.X11-unix/X$d" ] || continue
  r=$(DISPLAY=:$d glxinfo -B 2>/dev/null | grep -iE "OpenGL renderer|OpenGL vendor")
  echo ":$d -> ${r:-<no GL / glxinfo missing>}"
done

echo; echo "===== listening ports (VNC 5900/5901, noVNC 6080, etc.) ====="
(ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null) | grep -iE "LISTEN|vnc" | head -20

echo; echo "===== streaming toolchain present? ====="
for t in gst-launch-1.0 gst-inspect-1.0 ffmpeg glxinfo glxgears xdotool python3; do
  printf "  %-16s %s\n" "$t" "$(command -v "$t" || echo MISSING)"
done
echo "  nvenc element:" ; gst-inspect-1.0 nvh264enc 2>/dev/null | grep -m1 -i "Factory Details\|nvh264enc" || echo "    nvh264enc MISSING (need gstreamer1.0-plugins-bad + libnvidia-encode)"
echo "  aioquic:" ; python3 -c "import aioquic,sys; print('   ', aioquic.__version__)" 2>/dev/null || echo "    aioquic MISSING"
echo; echo "===== done ====="
