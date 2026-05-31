#!/usr/bin/env bash
# Desktopia <-> vast.ai helper. Drives the vastai CLI for the dev/test loop.
#
#   ./vast.sh search                 # list cheap single-RTX-4090 offers
#   ./vast.sh up <OFFER_ID> [image]  # create instance (default: CUDA base for Phase-1 debugging)
#   ./vast.sh up <OFFER_ID> ghcr     # create instance from the built GHCR image (Phase 2)
#   ./vast.sh ls                     # show instances + mapped ports
#   ./vast.sh ssh                    # ssh into the (first) instance
#   ./vast.sh sync                   # rsync this repo to /root/desktopia on the instance
#   ./vast.sh provision              # sync, then install deps (provision.sh) on the instance
#   ./vast.sh run                    # sync, then run entrypoint.sh on the instance
#   ./vast.sh gltest                 # sync+provision, then Xorg go/no-go (glxinfo renderer)
#   ./vast.sh status                 # detailed lifecycle state (actual/intended/status_msg)
#   ./vast.sh logs                   # tail instance logs (shows docker pull progress)
#   ./vast.sh port                   # print the public IP:PORT mapped to 4433/udp
#   ./vast.sh stop                   # stop (pause GPU billing, keep disk for a fast restart)
#   ./vast.sh start                  # restart a stopped instance (seconds if GPU still free)
#   ./vast.sh down                   # destroy the instance (deletes disk, stops all billing)
#
# Set DESKTOPIA_INSTANCE to pin a specific id; otherwise the first running one is used.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# SSH identity. vast authenticates with a key you registered; if it's a NON-default name
# (e.g. ~/.ssh/vast-ai-rsa) ssh won't offer it unless it's in your agent. Set
# DESKTOPIA_SSH_KEY=~/.ssh/vast-ai-rsa to point ssh/rsync straight at it (-i, IdentitiesOnly).
SSH_OPTS="-o StrictHostKeyChecking=accept-new"
if [ -n "${DESKTOPIA_SSH_KEY:-}" ]; then
  SSH_OPTS="$SSH_OPTS -i ${DESKTOPIA_SSH_KEY/#\~/$HOME} -o IdentitiesOnly=yes"
fi
GHCR_IMAGE="ghcr.io/pieper/desktopia:latest"
# Phase-1 test base. Use a vast.ai-PRE-CACHED image so it loads in seconds, not minutes.
# (nvidia/cuda:*-ubuntu24.04 exists only for CUDA >=12.5 AND isn't vast-cached -> slow/typo-prone.)
# This is the same family as the user's existing desktop box; has nvidia-smi/glxinfo/X/ffmpeg.
BASE_IMAGE="vastai/linux-desktop:cuda-12.9-ubuntu24.04-2026-05-21"
ENVOPTS='-p 4433:4433/udp -e NVIDIA_DRIVER_CAPABILITIES=all -e NVIDIA_VISIBLE_DEVICES=all'
# Region matters for interactive latency (motion-to-photon is RTT-bound). Constrain to
# North America by default; override e.g. SEARCH_GEO='geolocation in [US]' for US-only.
SEARCH_GEO=${SEARCH_GEO:-'geolocation in [US,CA]'}
# inet_up = host UPLOAD (video flows host->browser); reliability/verified for stable hosts.
SEARCH_Q="gpu_name=RTX_4090 num_gpus=1 rentable=true verified=true disk_space>=40 inet_up>=100 $SEARCH_GEO"

need() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 1; }; }
need vastai

instance_id() {
  [ -n "${DESKTOPIA_INSTANCE:-}" ] && { echo "$DESKTOPIA_INSTANCE"; return; }
  vastai show instances-v1 --raw \
    | python3 "$HERE/scripts/pick_instance.py" "$BASE_IMAGE" "$GHCR_IMAGE" desktopia
}

# Parse `vastai ssh-url` (e.g. ssh://root@host:port) into "PORT USER HOST", robust to the
# user@/host:port/bare-host variants. Read with: read -r PORT USER_ HOST < <(ssh_parts)
ssh_parts() {
  local url; url=$(vastai ssh-url "$(instance_id)"); url=${url#ssh://}
  local user=root host=$url port=22
  case "$host" in *@*) user=${host%%@*}; host=${host#*@};; esac
  case "$host" in *:*) port=${host##*:}; host=${host%:*};; esac
  echo "$port $user $host"
}

# Run a command on the instance over ssh (allocates a TTY for live output).
remote() {
  local PORT USER_ HOST; read -r PORT USER_ HOST < <(ssh_parts)
  ssh -t $SSH_OPTS -p "$PORT" "$USER_@$HOST" "$@"
}

cmd=${1:-help}; shift || true
case "$cmd" in
  search)  # price/geo/net/reliability/PCIe table, cheapest first
    vastai search offers "$SEARCH_Q" -o dph_total --raw | python3 "$HERE/scripts/offers.py" list ;;
  best)    # print the single best OFFER_ID (cheapest with high PCIe) for `make up`
    vastai search offers "$SEARCH_Q" -o dph_total --raw | python3 "$HERE/scripts/offers.py" best ;;
  up-best) "$0" up "$("$0" best)" "${1:-base}" ;;
  up)
    offer=${1:?need OFFER_ID}; sel=${2:-base}
    img=$BASE_IMAGE; [ "$sel" = "ghcr" ] && img=$GHCR_IMAGE
    echo "launching $img on offer $offer"
    vastai create instance "$offer" --image "$img" --env "$ENVOPTS" --disk 40 --ssh --direct
    ;;
  ls|list) vastai show instances-v1 ;;
  url)  vastai ssh-url "$(instance_id)" ;;
  ssh)
    PORT="" USER_="" HOST=""; read -r PORT USER_ HOST < <(ssh_parts)
    exec ssh $SSH_OPTS -p "$PORT" "$USER_@$HOST"
    ;;
  sync)
    PORT="" USER_="" HOST=""; read -r PORT USER_ HOST < <(ssh_parts)
    rsync -av --exclude '.git' --exclude '__pycache__' --exclude '.venv' \
      -e "ssh $SSH_OPTS -p $PORT" ./ "$USER_@$HOST:/root/desktopia/"
    ;;
  provision) "$0" sync; remote 'cd /root/desktopia && bash provision.sh' ;;
  run)       "$0" sync; remote 'cd /root/desktopia && bash entrypoint.sh' ;;
  gltest)    # sharp edge #1 go/no-go: must print an NVIDIA renderer, not llvmpipe
    "$0" sync; remote 'cd /root/desktopia && bash provision.sh && bash gltest.sh' ;;
  egltest)   # confirm hardware GL via EGL (no X, no DRM master) -- the clean render path
    "$0" sync; remote 'cd /root/desktopia && bash egltest.sh' ;;
  wl-build)  # build+install gst-wayland-display (Smithay headless compositor) on the box
    "$0" sync; remote 'cd /root/desktopia && bash provision-wayland.sh' ;;
  wl-check)  # locate the installed plugin + inspect it (real element name + properties)
    "$0" sync; remote 'cd /root/desktopia && bash wl-check.sh' ;;
  wl-fixwayland) # build libwayland>=1.23 (distro 1.22 lacks wl_client_set_max_buffer_size)
    "$0" sync; remote 'cd /root/desktopia && bash wl-fixwayland.sh' ;;
  wl-setup)  # one-shot rebuild-from-scratch on a fresh box: compositor + libwayland 1.25
    "$0" sync; remote 'cd /root/desktopia && bash provision-wayland.sh && bash wl-fixwayland.sh' ;;
  wltest)    # bring up the compositor + a GL client; confirm NVIDIA renderer + capture frames
    "$0" sync; remote 'cd /root/desktopia && bash wltest.sh' ;;
  gametest)  # gamescope nested -> XWayland -> X11 GL app (glxgears); prove the Slicer path
    "$0" sync; remote 'cd /root/desktopia && bash gametest.sh' ;;
  inspect)   # probe a vastai/linux-desktop box: display, GL renderer, Selkies, toolchain
    "$0" sync; remote 'cd /root/desktopia && bash scripts/inspect_desktop.sh' ;;
  status)
    vastai show instance "$(instance_id)" --raw | python3 -c '
import sys, json
d = json.load(sys.stdin)
for k in ("actual_status","intended_status","cur_state","next_state","status_msg",
          "gpu_name","machine_id","image_uuid","disk_space","inet_down"):
    v = d.get(k)
    if v not in (None, ""):
        print(f"{k:16}: {v}")'
    ;;
  logs) vastai logs "$(instance_id)" ;;
  port)
    vastai show instance "$(instance_id)" --raw | python3 -c '
import sys,json; d=json.load(sys.stdin); pm=d.get("ports") or {}
m=pm.get("4433/udp");
print(f"{d.get(\"public_ipaddr\")}:{m[0][\"HostPort\"]}") if m else print("4433/udp not mapped yet", file=sys.stderr)'
    ;;
  stop)  vastai stop instance "$(instance_id)" ;;
  start) vastai start instance "$(instance_id)" ;;
  down|destroy) vastai destroy instance "$(instance_id)" ;;
  *) sed -n '2,30p' "$0" ;;
esac
