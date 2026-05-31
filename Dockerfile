FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# Dependencies live in provision.sh so the Phase-1 live-debug box (bare nvidia/cuda image,
# `make provision`) and this baked image install the exact same set and never drift.
COPY provision.sh /usr/local/bin/provision.sh
RUN chmod +x /usr/local/bin/provision.sh && /usr/local/bin/provision.sh

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY server.py /opt/stream/server.py
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV DISPLAY=:0
EXPOSE 4433/udp
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
