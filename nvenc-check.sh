#!/usr/bin/env bash
# Is hardware NVENC available on this box, and if so why isn't GStreamer's nvcodec registering?
# ffmpeg h264_nvenc is the ground-truth test. Run: make nvenc-check
set -uo pipefail
export LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export GST_REGISTRY_FORK=no

echo "== NVENC/CUDA driver libs (NVENC needs the 'video' capability injected) =="
ls -1 /usr/lib/x86_64-linux-gnu/libnvidia-encode.so* 2>/dev/null || echo "  libnvidia-encode MISSING -> 'video' cap not injected = no NVENC"
ls -1 /usr/lib/x86_64-linux-gnu/libcuda.so* 2>/dev/null || echo "  libcuda MISSING"
ls -1 /usr/lib/x86_64-linux-gnu/libnvidia-encode.so* >/dev/null 2>&1 && echo "NVIDIA_DRIVER_CAPABILITIES=${NVIDIA_DRIVER_CAPABILITIES:-<unset>}"

echo "== nvidia-smi encoder sessions table (exists if NVENC present) =="
nvidia-smi -q 2>/dev/null | grep -iA3 "Encoder Stats\|FBC Stats" | head -8 || echo "  (no encoder stats)"

echo "== gstreamer nvcodec plugin file + load reason =="
ls -1 /usr/lib/x86_64-linux-gnu/gstreamer-1.0/libgstnvcodec.so 2>/dev/null || echo "  libgstnvcodec.so MISSING (need gstreamer1.0-plugins-bad)"
rm -f ~/.cache/gstreamer-1.0/registry.* 2>/dev/null
GST_DEBUG=default:3 gst-inspect-1.0 nvcodec 2>&1 | grep -iE "nvcodec|cuda|encode|fail|error|no such|version" | head -15

echo "== ffmpeg: does it even have h264_nvenc? =="
ffmpeg -hide_banner -encoders 2>/dev/null | grep -i nvenc || echo "  ffmpeg lists no *nvenc* encoder"

echo "== ffmpeg functional NVENC test (the ground truth) =="
ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc=size=640x480:rate=30 -t 1 \
  -c:v h264_nvenc -f null - 2>/tmp/nvenc-ff.log \
  && echo "PASS: h264_nvenc encoded successfully -> NVENC IS available (GStreamer side is fixable)" \
  || { echo "FAIL: h264_nvenc could not encode -> NVENC not usable on this host. Error:"; cat /tmp/nvenc-ff.log; }
