FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    # GLVND dispatch ONLY — NVIDIA GL/EGL/encode libs are injected by the
    # NVIDIA Container Toolkit at runtime. Do NOT install nvidia-driver-*;
    # a userspace driver here will mismatch the host kernel module and break
    # GLX/NVENC, and Mesa's libgl1-mesa-glx can shadow the injected libGLX_nvidia
    # (-> llvmpipe software rendering even with NVIDIA_DRIVER_CAPABILITIES=all).
    libglvnd0 libgl1 libglx0 libegl1 libgles2 \
    # X server (the nvidia X driver module is injected with the 'display' capability)
    xserver-xorg-core xinit x11-xserver-utils \
    mesa-utils \
    # GStreamer + NVENC (nvcodec lives in plugins-bad; it dlopens libnvidia-encode)
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-x \
    gir1.2-gst-plugins-base-1.0 gir1.2-gstreamer-1.0 python3-gi python3-gst-1.0 \
    python3 python3-pip openssl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages "aioquic>=1.0" cryptography

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY server.py /opt/stream/server.py
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV DISPLAY=:0
EXPOSE 4433/udp
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
