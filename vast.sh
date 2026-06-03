#!/usr/bin/env bash
# Desktopia <-> vast.ai helper. Drives the vastai CLI to rent a GPU, build the compositor,
# stream the desktop, and tear down.
#
#   ./vast.sh search                 # list cheap single-RTX-4090 offers
#   ./vast.sh best                   # print the single best OFFER_ID
#   ./vast.sh up <OFFER_ID> [ghcr]   # create instance (default base image; 'ghcr' = built image)
#   ./vast.sh ls                     # show instances + mapped ports
#   ./vast.sh ssh                    # ssh into the (first) instance
#   ./vast.sh sync                   # rsync this repo to /root/desktopia on the instance
#   ./vast.sh wl-setup               # build the compositor + libwayland on a bare instance
#   ./vast.sh stream                 # run the desktop + QUIC stream (entrypoint-wayland.sh)
#   ./vast.sh port                   # print the public IP:PORT mapped to 4433/udp
#   ./vast.sh status                 # detailed lifecycle state (actual/intended/status_msg)
#   ./vast.sh logs                   # tail instance logs (shows docker pull progress)
#   ./vast.sh stop                   # stop (pause GPU billing, keep disk for a fast restart)
#   ./vast.sh start                  # restart a stopped instance (seconds if GPU still free)
#   ./vast.sh down                   # destroy the instance (deletes disk, stops all billing)
#   ./vast.sh debug <name>           # run debug_utils/<name>.sh on the instance
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
# Bare base image for building the compositor on (./vast.sh wl-setup). A vast.ai-PRE-CACHED
# image loads in seconds, not minutes; it ships nvidia-smi/glxinfo/X/ffmpeg. The built image
# (GHCR_IMAGE, ./vast.sh up <id> ghcr) instead boots straight into the desktop stream.
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
    if [ "$sel" = "ghcr" ]; then
      # vast ssh-mode discards the image ENTRYPOINT, so launch our baked stack via an onstart that
      # backgrounds it (vast's own sshd/portal keep running alongside; entrypoint fixes ssh perms,
      # fetches Slicer, and starts the compositor+QUIC stream).
      echo "launching $GHCR_IMAGE on offer $offer (ssh-mode + onstart)"
      vastai create instance "$offer" --image "$GHCR_IMAGE" --env "$ENVOPTS" --disk 40 --ssh --direct \
        --onstart-cmd 'setsid bash /opt/desktopia/entrypoint-wayland.sh >/var/log/desktopia.log 2>&1 </dev/null &'
    else
      echo "launching $BASE_IMAGE on offer $offer"
      vastai create instance "$offer" --image "$BASE_IMAGE" --env "$ENVOPTS" --disk 40 --ssh --direct
    fi
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
  wl-build)  # build+install gst-wayland-display (Smithay headless compositor) on the box
    "$0" sync; remote 'cd /root/desktopia && bash provision-wayland.sh' ;;
  wl-fixwayland) # build libwayland>=1.23 (distro 1.22 lacks wl_client_set_max_buffer_size)
    "$0" sync; remote 'cd /root/desktopia && bash wl-fixwayland.sh' ;;
  wl-setup)  # one-shot setup on a fresh box: compositor + libwayland 1.25
    "$0" sync; remote 'cd /root/desktopia && bash provision-wayland.sh && bash wl-fixwayland.sh' ;;
  stream)    # compositor + encoder + QUIC server + Xwayland + desktop; stream to the browser
    "$0" sync; remote 'cd /root/desktopia && bash entrypoint-wayland.sh' ;;
  debug)     # run a diagnostic from debug_utils/:  ./vast.sh debug <name>  (e.g. nvenc-check)
    "$0" sync; remote "cd /root/desktopia && bash debug_utils/${1:?need a debug_utils script name}.sh" ;;
  pull)      # pull a file from the instance: make pull REMOTE=/path [LOCAL=./]
    PORT="" USER_="" HOST=""; read -r PORT USER_ HOST < <(ssh_parts)
    rsync -av -e "ssh $SSH_OPTS -p $PORT" "$USER_@$HOST:${1:?need REMOTE path}" "${2:-./}" ;;
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
    vastai show instance "$(instance_id)" --raw | python3 "$HERE/scripts/port.py" ;;
  stop)  vastai stop instance "$(instance_id)" ;;
  start) vastai start instance "$(instance_id)" ;;
  down|destroy) vastai destroy instance "$(instance_id)" ;;
  *) sed -n '2,30p' "$0" ;;
esac
