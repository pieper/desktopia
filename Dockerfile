# Desktopia (phase B) — the headless Wayland compositor + streaming stack baked ON TOP OF the
# vast.ai base image, so vast's own sshd / portal / port-mapping stay intact and our stack runs
# alongside them, launched via an `--onstart` script (vast's ssh mode discards image ENTRYPOINTs).
# The 5.88 GB base is cached on vast hosts, so only our small delta pulls. 3D Slicer is NOT baked
# (it downloads in ~5 s on demand); Chrome is on-demand too. NVIDIA driver libs are injected at
# runtime by the Container Toolkit. Built/validated by CI.

# ---------- builder: compile gst-wayland-display + libwayland on matching ubuntu (discarded) ----------
FROM ubuntu:24.04 AS builder
ENV DEBIAN_FRONTEND=noninteractive
COPY provision-wayland.sh wl-fixwayland.sh /tmp/build/
RUN bash /tmp/build/provision-wayland.sh && bash /tmp/build/wl-fixwayland.sh

# ---------- runtime: the vast base (ubuntu 24.04, cached, carries vast's sshd/tooling) ----------
FROM vastai/linux-desktop:cuda-12.9-ubuntu24.04-2026-05-21
ENV DEBIAN_FRONTEND=noninteractive

# the compiled compositor plugin + the newer libwayland it needs (runtime points LD_LIBRARY_PATH here)
COPY --from=builder /usr/local/lib/x86_64-linux-gnu/gstreamer-1.0/libgstwaylanddisplaysrc.so \
     /usr/local/lib/x86_64-linux-gnu/gstreamer-1.0/
COPY --from=builder /usr/local/lib/x86_64-linux-gnu/libwayland-*.so* \
     /usr/local/lib/x86_64-linux-gnu/
RUN ldconfig

# runtime deps the compositor + desktop session need that the base may lack (most are already on the
# KDE/desktop base; the small lib*-dev are a robust way to pull the exact runtime .so the plugin links)
RUN apt-get update && apt-get install -y --no-install-recommends \
      openbox wmctrl xterm obconf xwayland \
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
      gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-tools gstreamer1.0-x \
      libgbm1 libdrm2 libinput10 libseat1 libxkbcommon0 libdisplay-info-dev \
      python3-xlib python3-pip sudo gcc openssl ca-certificates curl \
 && pip3 install --break-system-packages "aioquic>=1.0" \
 && rm -rf /var/lib/apt/lists/*

# our stream server + desktop session + onstart entrypoint
COPY server.py session-wayland.sh entrypoint-wayland.sh /opt/desktopia/

EXPOSE 4433/udp
# No ENTRYPOINT override: launched in vast ssh mode via
#   --onstart-cmd 'setsid bash /opt/desktopia/entrypoint-wayland.sh >/var/log/desktopia.log 2>&1 </dev/null &'
