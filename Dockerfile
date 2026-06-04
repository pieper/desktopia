# Desktopia compositor artifact. We CANNOT bake a runnable image for vast (vast's managed ssh-mode
# only recognizes its own base images by reference; a derived image is treated as blind/unmanaged).
# So instead of baking, we publish the *prebuilt compositor* as a tiny FROM-scratch image, and the
# onstart/entrypoint downloads + extracts it (~10 MB, seconds) onto the vast base — replacing the
# ~13-min runtime build. The vast base (ubuntu 24.04) is ABI-compatible with this ubuntu:24.04 build.

# ---------- builder: compile gst-wayland-display + libwayland (discarded) ----------
FROM ubuntu:24.04 AS builder
ENV DEBIAN_FRONTEND=noninteractive
COPY provision-wayland.sh wl-fixwayland.sh /tmp/build/
RUN bash /tmp/build/provision-wayland.sh && bash /tmp/build/wl-fixwayland.sh \
 && mkdir -p /artifact/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0 \
 && cp -a /usr/local/lib/x86_64-linux-gnu/gstreamer-1.0/libgstwaylanddisplaysrc.so \
          /artifact/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0/ \
 && cp -a /usr/local/lib/x86_64-linux-gnu/libwayland-*.so* \
          /artifact/usr/local/lib/x86_64-linux-gnu/

# ---------- artifact: a single tiny layer of the compositor files at their install paths ----------
FROM scratch
COPY --from=builder /artifact/ /
