# Desktopia compositor artifact. We CANNOT bake a runnable image for vast (vast's managed ssh-mode
# only recognizes its own base images by reference; a derived image is treated as blind/unmanaged).
# So instead of baking, we publish the *prebuilt compositor* (+ a pre-rendered loading splash) as a
# tiny FROM-scratch image, and the onstart/entrypoint downloads + extracts it (~few MB, seconds)
# onto the vast base. The vast base (ubuntu 24.04) is ABI-compatible with this ubuntu:24.04 build.

# ---------- builder: compile gst-wayland-display + libwayland, render the splash (discarded) ----------
FROM ubuntu:24.04 AS builder
ENV DEBIAN_FRONTEND=noninteractive
COPY provision-wayland.sh wl-fixwayland.sh /tmp/build/
COPY resources/slicer-logo.svg /tmp/build/slicer-logo.svg
RUN bash /tmp/build/provision-wayland.sh && bash /tmp/build/wl-fixwayland.sh \
 && apt-get install -y --no-install-recommends librsvg2-bin imagemagick fonts-dejavu-core \
 # compositor plugin + the libwayland it needs
 && mkdir -p /artifact/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0 \
 && cp -a /usr/local/lib/x86_64-linux-gnu/gstreamer-1.0/libgstwaylanddisplaysrc.so \
          /artifact/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0/ \
 && cp -a /usr/local/lib/x86_64-linux-gnu/libwayland-*.so* \
          /artifact/usr/local/lib/x86_64-linux-gnu/ \
 # pre-render TWO backgrounds HERE so the runtime box needs only feh (not imagemagick/librsvg):
 #   background.png = logo on a dark field (the steady desktop wallpaper)
 #   splash.png     = background.png + "Loading 3D Slicer... please wait" (shown until Slicer is up)
 && mkdir -p /artifact/usr/local/share/desktopia \
 && rsvg-convert -w 360 -h 360 /tmp/build/slicer-logo.svg -o /tmp/logo.png \
 && convert -size 1920x1080 xc:'#15151f' /tmp/logo.png -gravity center -geometry +0-40 -composite \
      /artifact/usr/local/share/desktopia/background.png \
 && convert /artifact/usr/local/share/desktopia/background.png \
      -gravity center -fill '#d8d8e0' -font /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
      -pointsize 40 -annotate +0+160 'Loading 3D Slicer...  please wait' \
      /artifact/usr/local/share/desktopia/splash.png

# ---------- artifact: a single tiny layer (compositor + libwayland + splash) at install paths ------
FROM scratch
COPY --from=builder /artifact/ /
