#!/usr/bin/env bash
# Desktopia <-> vast.ai helper. Drives the vastai CLI for the dev/test loop.
#
#   ./vast.sh search                 # list cheap single-RTX-4090 offers
#   ./vast.sh up <OFFER_ID> [image]  # create instance (default: CUDA base for Phase-1 debugging)
#   ./vast.sh up <OFFER_ID> ghcr     # create instance from the built GHCR image (Phase 2)
#   ./vast.sh ls                     # show instances + mapped ports
#   ./vast.sh ssh                    # ssh into the (first) instance
#   ./vast.sh sync                   # rsync this repo to /root/desktopia on the instance
#   ./vast.sh run                    # sync, then run entrypoint.sh on the instance
#   ./vast.sh logs                   # tail instance logs
#   ./vast.sh port                   # print the public IP:PORT mapped to 4433/udp
#   ./vast.sh down                   # destroy the (first) instance
#
# Set DESKTOPIA_INSTANCE to pin a specific id; otherwise the first running one is used.
set -euo pipefail

GHCR_IMAGE="ghcr.io/pieper/desktopia:latest"
BASE_IMAGE="nvidia/cuda:12.4.1-runtime-ubuntu24.04"
ENVOPTS='-p 4433:4433/udp -e NVIDIA_DRIVER_CAPABILITIES=all -e NVIDIA_VISIBLE_DEVICES=all'
SEARCH_Q='gpu_name=RTX_4090 num_gpus=1 rentable=true'

need() { command -v "$1" >/dev/null || { echo "missing: $1" >&2; exit 1; }; }
need vastai

instance_id() {
  [ -n "${DESKTOPIA_INSTANCE:-}" ] && { echo "$DESKTOPIA_INSTANCE"; return; }
  vastai show instances --raw | python3 -c 'import sys,json; xs=json.load(sys.stdin); print(xs[0]["id"]) if xs else sys.exit("no instances")'
}

cmd=${1:-help}; shift || true
case "$cmd" in
  search) vastai search offers "$SEARCH_Q" -o dph | head -25 ;;
  up)
    offer=${1:?need OFFER_ID}; sel=${2:-base}
    img=$BASE_IMAGE; [ "$sel" = "ghcr" ] && img=$GHCR_IMAGE
    echo "launching $img on offer $offer"
    vastai create instance "$offer" --image "$img" --env "$ENVOPTS" --disk 40 --ssh --direct
    ;;
  ls|list) vastai show instances ;;
  ssh)  exec bash -c "$(vastai ssh-url "$(instance_id)")" ;;
  url)  vastai ssh-url "$(instance_id)" ;;
  sync)
    url=$(vastai ssh-url "$(instance_id)")
    # ssh-url is like ssh://root@host:port ; split for rsync -e
    host=$(echo "$url" | sed -E 's#ssh://([^:]+@[^:]+):([0-9]+)#\1#'); port=$(echo "$url" | sed -E 's#.*:([0-9]+)$#\1#')
    rsync -av --exclude '.git' --exclude '__pycache__' -e "ssh -p $port" ./ "$host:/root/desktopia/"
    ;;
  run)
    "$0" sync
    url=$(vastai ssh-url "$(instance_id)")
    bash -c "$url" -t 'cd /root/desktopia && bash entrypoint.sh'
    ;;
  logs) vastai logs "$(instance_id)" ;;
  port)
    vastai show instance "$(instance_id)" --raw | python3 -c '
import sys,json; d=json.load(sys.stdin); pm=d.get("ports") or {}
m=pm.get("4433/udp");
print(f"{d.get(\"public_ipaddr\")}:{m[0][\"HostPort\"]}") if m else print("4433/udp not mapped yet", file=sys.stderr)'
    ;;
  down|destroy) vastai destroy instance "$(instance_id)" ;;
  *) sed -n '2,30p' "$0" ;;
esac
