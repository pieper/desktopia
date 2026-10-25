#!/usr/bin/env bash
# Desktopia <-> vast.ai helper. Drives the vastai CLI to rent a GPU, build the compositor,
# stream the desktop, and tear down.
#
#   ./vast.sh search                 # list cheap single-RTX-4090 offers
#   ./vast.sh best                   # print the single best OFFER_ID
#   ./vast.sh candidates [N]         # N offer ids from N different providers (what launch.py tests)
#   ./vast.sh up <OFFER_ID> [ghcr]   # create instance (default base image; 'ghcr' = built image)
#   ./vast.sh ls                     # show instances + mapped ports
#   ./vast.sh ssh [ssh-args...]      # ssh into the (first) instance; extra args pass to ssh,
#                                    #   e.g. ./vast.sh ssh -L 2027:localhost:2027  (port-forward)
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
# Slicer dev box, driven over MCP (.claude/skills/vast-slicer/SKILL.md):
#   ./vast.sh slicer-up              # launch.py with DESKTOPIA_MCP=1: rent, stream, Slicer stable + MCP :2026
#   ./vast.sh probe                  # rent+test N candidates (DESKTOPIA_CANDIDATES=3), record, destroy all
#   ./vast.sh tunnel [port]          # background ssh -L port:localhost:port (default 2026)
#   ./vast.sh tunnel-close           # end it
#   ./vast.sh mcp <args...>          # scripts/mcp_call.py: tools | py CODE | py -f FILE | call TOOL JSON
#                                    #   | screenshot OUT.png | wait-ready
#   ./vast.sh run '<command>'        # run one command on the box (no TTY; for scripts)
#   ./vast.sh push <local> <remote>  # rsync a file/dir to the box (owned by `user` under /home/user)
#   ./vast.sh push-module <dir> [name]  # a scripted module to ~/slicer-modules/<name>, loaded live
#   ./vast.sh slicer-restart         # kill Slicer and relaunch it in the desktop session
#
# Set DESKTOPIA_INSTANCE to pin a specific id; otherwise the first running one is used.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# the vastai CLI lives in the repo's venv (SETUP.md), as launch.py assumes
export PATH="$HERE/.venv/bin:$PATH"
# SSH identity. vast authenticates with a key you registered; if it's a NON-default name
# (e.g. ~/.ssh/vast-ai-rsa) ssh won't offer it unless it's in your agent. Set
# DESKTOPIA_SSH_KEY=~/.ssh/vast-ai-rsa to point ssh/rsync straight at it (-i, IdentitiesOnly).
SSH_OPTS="-o StrictHostKeyChecking=accept-new"
# launch.py defaults to ~/.ssh/vast-ai-rsa; so does this, when that key exists.
if [ -z "${DESKTOPIA_SSH_KEY:-}" ] && [ -f "$HOME/.ssh/vast-ai-rsa" ]; then DESKTOPIA_SSH_KEY=~/.ssh/vast-ai-rsa; fi
if [ -n "${DESKTOPIA_SSH_KEY:-}" ]; then
  SSH_OPTS="$SSH_OPTS -i ${DESKTOPIA_SSH_KEY/#\~/$HOME} -o IdentitiesOnly=yes"
fi
GHCR_IMAGE="ghcr.io/pieper/desktopia:latest"
# Bare base image for building the compositor on (./vast.sh wl-setup). A vast.ai-PRE-CACHED
# image loads in seconds, not minutes; it ships nvidia-smi/glxinfo/X/ffmpeg. The built image
# (GHCR_IMAGE, ./vast.sh up <id> ghcr) instead boots straight into the desktop stream.
BASE_IMAGE="vastai/linux-desktop:cuda-12.9-ubuntu24.04-2026-05-21"
ENVOPTS='-p 4433:4433/udp -p 4434:4434/tcp -e NVIDIA_DRIVER_CAPABILITIES=all -e NVIDIA_VISIBLE_DEVICES=all'
# Region matters for interactive latency (motion-to-photon is RTT-bound). Constrain to
# North America by default; override e.g. SEARCH_GEO='geolocation in [US]' for US-only.
SEARCH_GEO=${SEARCH_GEO:-'geolocation in [US,CA]'}
# inet_up = host UPLOAD (video flows host->browser); verified + reliability>0.98 bias toward
# stable hosts (coarse — the score is mostly historical uptime, so it won't catch every flaky
# port-mapping host; the launcher's retry-on-bad-host is the real backstop). DESKTOPIA_BLOCKLIST
# can carry host_ids we've seen fail, e.g. export DESKTOPIA_BLOCKLIST='host_id notin [392559,103782]'.
SEARCH_Q="gpu_name=RTX_4090 num_gpus=1 rentable=true verified=true reliability>0.98 disk_space>=40 inet_up>=100 $SEARCH_GEO ${DESKTOPIA_BLOCKLIST:-}"

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
# The same without a TTY, for commands whose output a script reads (and for non-interactive callers).
rsh() {
  local PORT USER_ HOST; read -r PORT USER_ HOST < <(ssh_parts)
  ssh $SSH_OPTS -p "$PORT" "$USER_@$HOST" "$@"
}
TUNNEL_SOCK="$HOME/.ssh/desktopia-tunnel.sock"
MCP_PORT=${DESKTOPIA_MCP_PORT:-2026}

cmd=${1:-help}; shift || true
case "$cmd" in
  search)  # price/geo/net/reliability/PCIe table, cheapest first
    vastai search offers "$SEARCH_Q" -o dph_total --raw | python3 "$HERE/scripts/offers.py" list ;;
  best)    # print the single best OFFER_ID (cheapest with high PCIe) for `make up`
    vastai search offers "$SEARCH_Q" -o dph_total --raw | python3 "$HERE/scripts/offers.py" best ;;
  candidates)  # print up to N offer ids from N different providers (launch.py rents + tests them in parallel)
    vastai search offers "$SEARCH_Q" -o dph_total --raw | python3 "$HERE/scripts/offers.py" candidates "${1:-3}" ;;
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
      # Vast base BY REFERENCE (vast manages sshd/keys/ports). Vast INTERMITTENTLY provisions
      # /root/.ssh/authorized_keys with perms sshd rejects ("bad ownership or modes" -> publickey
      # refused) on ~half of boxes -- confirmed in `vastai logs`. A *persistent* chmod loop keeps
      # the perms correct (a no-op when already fine, so safe everywhere; a finite loop stops too
      # soon and the box "breaks" later). Do NOT `vastai attach ssh` -- that triggers a key
      # re-write that can re-break the perms.
      echo "launching $BASE_IMAGE on offer $offer (vast base by reference + persistent ssh-perms fix)"
      # StrictModes stays ON (vast's `sed s/StrictModes yes/no/` misses the COMMENTED stock line), so
      # sshd enforces authorized_keys OWNERSHIP+modes. vast intermittently leaves it owned by a
      # non-root uid -> "bad ownership or modes" -> key auth refused. chmod alone can't fix it; chown too.
      # APPLIANCE: onstart auto-runs the full desktop stack if the code is on disk (persists across
      # vast stop/start) so restarts self-stream with no resume; first rent deploys then launch.py starts it.
      vastai create instance "$offer" --image "$BASE_IMAGE" --env "$ENVOPTS" --disk 40 --ssh --direct \
        --onstart-cmd '[ -f /root/desktopia/entrypoint-wayland.sh ] && setsid bash /root/desktopia/entrypoint-wayland.sh >/var/log/desktopia.log 2>&1 & while :; do chown root:root /root /root/.ssh /root/.ssh/authorized_keys 2>/dev/null; chmod 755 /root 2>/dev/null; chmod 700 /root/.ssh 2>/dev/null; chmod 600 /root/.ssh/authorized_keys 2>/dev/null; sleep 3; done'
    fi
    ;;
  ls|list) vastai show instances-v1 ;;
  url)  vastai ssh-url "$(instance_id)" ;;
  ssh)   # extra args pass straight to ssh -- BEFORE the host so they're options, not a remote command:
         #   ./vast.sh ssh -L 2027:localhost:2027        (port-forward)
         #   ./vast.sh ssh -L 8888:localhost:8888 -N     (forward only, no shell)
    PORT="" USER_="" HOST=""; read -r PORT USER_ HOST < <(ssh_parts)
    exec ssh $SSH_OPTS "$@" -p "$PORT" "$USER_@$HOST"
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
  slicer-up)  # rent + stream + Slicer with MCP; blocks serving the page (prints "=== READY ===")
    # launch.py's ssh is BatchMode: a passphrase-protected key not loaded in the agent is accepted by
    # the box and then never signed ("Connection closed by authenticating user ... [preauth]" in the
    # container log), and the run burns a rented box waiting. Catch it before renting.
    key=${DESKTOPIA_SSH_KEY:-~/.ssh/vast-ai-rsa}; key=${key/#\~/$HOME}
    if ! ssh-keygen -y -P "" -f "$key" >/dev/null 2>&1; then
      fp=$(ssh-keygen -lf "$key.pub" 2>/dev/null | awk '{print $2}')
      if [ -z "$fp" ] || ! ssh-add -l 2>/dev/null | grep -qF "$fp"; then
        echo "$key has a passphrase and is not in ssh-agent; first run:" >&2
        echo "  ssh-add --apple-use-keychain $key" >&2
        exit 1
      fi
    fi
    cd "$HERE" && DESKTOPIA_MCP=1 DESKTOPIA_MCP_PORT="$MCP_PORT" exec python3 launch.py ;;
  probe)      # rent N candidates (default 3, DESKTOPIA_CANDIDATES), measure + record them, destroy all
    cd "$HERE" && DESKTOPIA_PROBE=1 exec python3 launch.py ;;
  tunnel)     # localhost:PORT -> the box's PORT (Slicer MCP), backgrounded behind a control socket
    port=${1:-$MCP_PORT}
    PORT="" USER_="" HOST=""; read -r PORT USER_ HOST < <(ssh_parts)
    if ssh -S "$TUNNEL_SOCK" -O check "$USER_@$HOST" 2>/dev/null; then
      echo "tunnel already open ($TUNNEL_SOCK); ./vast.sh tunnel-close to end it"; exit 0
    fi
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      # e.g. a local Slicer with its own MCP server: ssh's localhost bind still wins over its wildcard
      # bind, but `mcp` calls would reach the local one the moment the tunnel drops. Say so.
      echo "warning: something already listens on local port $port:" >&2
      lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tail -n +2 >&2
      echo "  (to avoid it: DESKTOPIA_MCP_PORT=<other> for slicer-up, tunnel and mcp)" >&2
    fi
    ssh $SSH_OPTS -f -N -M -S "$TUNNEL_SOCK" -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=15 -L "$port:localhost:$port" -p "$PORT" "$USER_@$HOST"
    echo "tunnel: http://localhost:$port/mcp -> $HOST:$PORT" ;;
  tunnel-close)
    PORT="" USER_="" HOST=""; read -r PORT USER_ HOST < <(ssh_parts)
    ssh -S "$TUNNEL_SOCK" -O exit "$USER_@$HOST" ;;
  run)        # run '<command>': one command on the box, output to stdout (`ssh` takes ssh options instead)
    rsh "${1:?need a command}" ;;
  mcp)        # scripts/mcp_call.py against the tunnel
    exec python3 "$HERE/scripts/mcp_call.py" --url "http://localhost:$MCP_PORT/mcp" "$@" ;;
  push)       # push LOCAL REMOTE: rsync to the box; anything under /home/user is handed to `user`
    src=${1:?need LOCAL path}; dst=${2:?need REMOTE path}
    PORT="" USER_="" HOST=""; read -r PORT USER_ HOST < <(ssh_parts)
    case "$dst" in /home/user/*) rsh "sudo -u user mkdir -p '$(dirname "$dst")'";;
                   *) rsh "mkdir -p '$(dirname "$dst")'";; esac
    rsync -a --exclude '.git' --exclude '__pycache__' --exclude '.venv' --exclude '.pytest_cache' \
      -e "ssh $SSH_OPTS -p $PORT" "$src" "$USER_@$HOST:$dst"
    case "$dst" in /home/user/*) rsh "chown -R user:user '$dst'";; esac ;;
  push-module)  # push-module DIR [NAME]: a scripted module (DIR/NAME.py) into ~/slicer-modules, loaded now
    dir=${1:?need a module directory}; dir=${dir%/}; name=${2:-$(basename "$dir")}
    [ -f "$dir/$name.py" ] || { echo "$dir has no $name.py - not a scripted module directory" >&2; exit 1; }
    "$0" push "$dir/" "/home/user/slicer-modules/$name/"
    "$0" mcp py "import desktopia_dev; __result = desktopia_dev.load_modules()" ;;
  slicer-restart)  # as `user`, in the session's own environment (saved by session-wayland.sh)
    # The pkill pattern must not match this bash -c's own command line (which says /opt/Slicer-*/), or
    # it kills itself before the relaunch; [0-9] only matches a real versioned install path.
    rsh "sudo -u user -H bash -c 'pkill -f \"/opt/Slicer-[0-9][^ ]*/(Slicer|bin/SlicerApp-real)\" ; sleep 2; . /home/user/.desktopia-session.env; \
      S=\$(ls -d /opt/Slicer-*/ | head -1); setsid \"\$S/Slicer\" --no-splash >/tmp/slicer.log 2>&1 </dev/null &'"
    echo "Slicer restarting; ./vast.sh mcp wait-ready" ;;
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
  *) sed -n '2,33p' "$0" ;;
esac
