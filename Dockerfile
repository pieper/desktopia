# Desktopia — bakes the full headless-Wayland desktop-streaming stack so the container boots
# straight into a GPU desktop streamed over QUIC. The NVIDIA driver libraries (GL/EGL/NVENC)
# are injected at RUNTIME by the NVIDIA Container Toolkit (needs NVIDIA_DRIVER_CAPABILITIES=all),
# so only the GLVND dispatch + our own build/runtime deps are installed here. Never install
# nvidia-driver-* — a userspace driver would mismatch the host kernel module.
#
# Build is validated by CI (.github/workflows/build.yml); the same compositor build scripts run
# on a bare instance via `make wl-setup`. entrypoint-wayland.sh is idempotent and backfills any
# missing runtime dependency on first boot, so this image only needs to bake the expensive parts.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# 1. Headless Wayland compositor (gst-wayland-display, Smithay) + a newer libwayland.
#    These are the exact scripts `make wl-setup` runs on a bare instance. The Rust toolchain
#    and git checkouts are removed afterward to keep the layer small (compositor installs to
#    /usr/local).
COPY provision-wayland.sh wl-fixwayland.sh /tmp/build/
RUN bash /tmp/build/provision-wayland.sh \
 && bash /tmp/build/wl-fixwayland.sh \
 && rm -rf /tmp/build /root/.cargo /root/.rustup /opt/gst-wayland-display /opt/wayland

# 2. Desktop runtime: window manager, GLVND dispatch, encoders, and the tools the entrypoint
#    expects (baked so the first boot does not have to apt-install anything).
RUN apt-get update && apt-get install -y --no-install-recommends \
      openbox wmctrl xterm obconf \
      gstreamer1.0-plugins-ugly gstreamer1.0-libav \
      vulkan-tools libvulkan1 \
      libglvnd0 libgl1 libglx0 libegl1 libgles2 \
      python3-xlib python3-pip \
      sudo gcc openssl ca-certificates curl gnupg \
 && pip3 install --break-system-packages "aioquic>=1.0" \
 && rm -rf /var/lib/apt/lists/*

# 3. Google Chrome (not in the Ubuntu archive; needs Google's apt source).
RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
      | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
 && rm -rf /var/lib/apt/lists/*

# 4. 3D Slicer (the default app) into /opt, where session-wayland.sh looks for it, plus the
#    X/runtime libraries its prebuilt tarball needs on a minimal Ubuntu.
RUN curl -L --retry 3 -o /tmp/Slicer.tar.gz \
      "https://download.slicer.org/download?os=linux&stability=release" \
 && mkdir -p /opt && tar -xf /tmp/Slicer.tar.gz -C /opt && rm /tmp/Slicer.tar.gz \
 && apt-get update && apt-get install -y --no-install-recommends \
      libpulse-mainloop-glib0 libnss3 libxcomposite1 libxdamage1 libxrandr2 libxtst6 \
      libxkbcommon0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
      libxcb-render-util0 libxcb-shape0 libxcb-util1 libxcb-xfixes0 libxcb-xinerama0 \
      libxcb-xkb1 libfontconfig1 libdbus-1-3 \
 && rm -rf /var/lib/apt/lists/*

# 5. The stream server, desktop session, and entrypoint. (The browser client, client/index.html,
#    is opened on the viewer's own machine — the box never serves it — so it is not baked in.)
WORKDIR /opt/desktopia
COPY server.py session-wayland.sh entrypoint-wayland.sh ./

EXPOSE 4433/udp
ENTRYPOINT ["bash", "/opt/desktopia/entrypoint-wayland.sh"]
