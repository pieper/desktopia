#!/usr/bin/env python3
"""Desktopia launcher + live progress poller.

Rents a vast.ai GPU, brings up the streamed desktop, and drives client/status.json with TWO live
log streams (host-daemon image-pull + container/desktop stdout) plus a one-line status headline.

ALL vast API access happens in THIS single process: the high-frequency polling goes straight to the
vast REST API, and the one-shot lifecycle ops (pick offer / create / destroy) shell out to `vastai`
serially. That serialization is the whole point -- the previous launcher ran a background loop AND
foreground checks that hit the `vastai` CLI concurrently, which intermittently returned empty output
and tripped the retry loop into destroying good hosts. One owner, no race.

Run:  python3 launch.py        (serves client/, opens Chrome, rents+streams, retries bad hosts)
"""
import base64, http.server, json, os, re, socketserver, ssl, subprocess, sys, threading, time
import urllib.request, urllib.error

HERE     = os.path.dirname(os.path.abspath(__file__))
os.environ["PATH"] = os.path.join(HERE, ".venv", "bin") + os.pathsep + os.environ.get("PATH", "")
CLIENT   = os.path.join(HERE, "client")
STATUS   = os.path.join(CLIENT, "status.json")
API      = os.environ.get("VAST_URL", "https://console.vast.ai")
KEY      = open(os.path.expanduser("~/.config/vastai/vast_api_key")).read().strip()
SSH_KEY  = os.path.expanduser(os.environ.get("DESKTOPIA_SSH_KEY", "~/.ssh/vast-ai-rsa"))
# Thin managed base (~1.31 GB) vs the old linux-desktop (~5.48 GB): same managed-ssh-by-reference,
# 24.04 (matches compositor ABI), GPU/NVENC/GL come from the host-injected driver (stock just omits
# the unused CUDA dev toolkit). py312 variant guarantees a system python3 the onstart needs.
BASE_IMG = "vastai/base-image:stock-ubuntu24.04-py312-2026-06-04"
ENVOPTS  = "-p 4433:4433/udp -p 4434:4434/tcp -e NVIDIA_DRIVER_CAPABILITIES=all -e NVIDIA_VISIBLE_DEVICES=all"
# vast's image tries `sed s/StrictModes yes/StrictModes no/` but the stock line is COMMENTED, so
# StrictModes stays ON and sshd enforces authorized_keys ownership+modes. vast intermittently leaves
# /root/.ssh/authorized_keys owned by a non-root uid -> "bad ownership or modes" -> every key auth
# refused forever. chmod alone can't fix it; we must chown too. Leading echo lands in the CONTAINER
# log (which we poll) so we can SEE the onstart actually ran.
# APPLIANCE BOOT: auto-launch the whole desktop stack (compositor, Xwayland, openbox WM, Slicer, QUIC
# server) on every boot/restart IF the code is on disk. The disk persists across vast stop/start, so
# after the first launch (which rsyncs the repo to /root/desktopia) every restart self-streams with no
# resume needed. First rent: the file isn't there yet -> skipped, and launch.py deploys + starts it.
ONSTART  = ("echo DESKTOPIA-ONSTART-UP; "
            "[ -f /root/desktopia/entrypoint-wayland.sh ] && "
            "setsid bash /root/desktopia/entrypoint-wayland.sh >/var/log/desktopia.log 2>&1 & "
            "while :; do "
            "chown root:root /root /root/.ssh /root/.ssh/authorized_keys 2>/dev/null; "
            "chmod 755 /root 2>/dev/null; chmod 700 /root/.ssh 2>/dev/null; "
            "chmod 600 /root/.ssh/authorized_keys 2>/dev/null; sleep 3; done")
PORT_WEB = 8765
SOPTS    = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2", "-o", "IdentitiesOnly=yes",
            "-i", SSH_KEY]

# ---- status.json (atomic write so the page never reads a half-written file) ----
_state = {"phase": "Renting a GPU…", "ready": False, "ip_port": "", "cert": "", "ws": "",
          "transport": os.environ.get("DESKTOPIA_TRANSPORT", "webtransport"),
          "headline": "", "log_daemon": "", "log_container": ""}
def write_status(**kw):
    _state.update(kw)
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as f: json.dump(_state, f)
    os.replace(tmp, STATUS)
def say(phase): print("•", phase, flush=True); write_status(phase=phase)

# ---- vast REST ----
def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + KEY, "Accept": "application/json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

def instance(iid):
    try:
        for d in api("GET", "/api/v0/instances/?owner=me").get("instances", []):
            if d.get("id") == iid: return d
    except Exception as e:
        print("  (status fetch failed:", type(e).__name__, e, ")", flush=True)
    return None

_logcache = {}   # stream -> last good text, so a transient fetch failure doesn't blank the pane
def get_log(iid, daemon, tail=400):
    body = {"tail": str(tail)}
    if daemon: body["daemon_logs"] = "true"
    key = "d" if daemon else "c"
    try:
        url = api("PUT", f"/api/v0/instances/request_logs/{iid}/", body).get("result_url")
    except Exception:
        return _logcache.get(key, "")
    if url:
        # the S3 object is generated async: 403/404 for a few seconds, then 200.
        for _ in range(14):
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    if r.status == 200:
                        _logcache[key] = r.read().decode("utf-8", "replace"); return _logcache[key]
            except urllib.error.HTTPError:
                pass
            except Exception:
                pass
            time.sleep(0.4)
    return _logcache.get(key, "")

def refresh_logs(iid, st=None):
    """Pull both streams + headline into status.json. Returns the instance dict (fetched if not given)."""
    inst = st if st is not None else instance(iid)
    if inst:
        msg = (inst.get("status_msg") or "").strip().splitlines()
        head = inst.get("actual_status", "?")
        if msg: head += " — " + msg[-1][:90]
        write_status(headline=head)
    write_status(log_daemon=get_log(iid, True)[-7000:], log_container=get_log(iid, False)[-7000:])
    return inst

# ---- vastai CLI one-shots (serial) ----
def cli(args, **kw):
    return subprocess.run(["vastai", *args], capture_output=True, text=True, timeout=120, **kw)
def best_offer():
    out = subprocess.run(["bash", os.path.join(HERE, "vast.sh"), "best"],
                         capture_output=True, text=True, timeout=120).stdout.strip().splitlines()
    return out[-1] if out else ""
def create(offer):
    r = cli(["create", "instance", offer, "--image", BASE_IMG, "--env", ENVOPTS,
             "--disk", "40", "--ssh", "--direct", "--onstart-cmd", ONSTART])
    m = re.search(r"new_contract['\"]?:\s*(\d+)", r.stdout)
    return int(m.group(1)) if m else None
def destroy(iid):
    cli(["destroy", "instance", str(iid)], input="y\n")

# ---- SSH (control channel) via the PROVEN ssh-url, not a hand-built direct IP:port ----
def ssh_target(iid):
    out = cli(["ssh-url", str(iid)]).stdout.strip()
    m = re.match(r"ssh://(?:([^@]+)@)?([^:]+)(?::(\d+))?", out)
    if not m: return None
    return (m.group(1) or "root"), m.group(2), (m.group(3) or "22")
def sshk(tgt, cmd, timeout=40):
    user, host, port = tgt
    return subprocess.run(["ssh", *SOPTS, "-p", port, f"{user}@{host}", cmd],
                          capture_output=True, text=True, timeout=timeout)
def stream_endpoint(inst):
    p = (inst.get("ports") or {}).get("4433/udp")
    ip = inst.get("public_ipaddr")
    return f"{ip}:{p[0]['HostPort']}" if (p and ip) else None

def ws_endpoint(inst):              # the WS/TCP transport (wss with our self-signed cert on vast direct)
    p = (inst.get("ports") or {}).get("4434/tcp")
    ip = inst.get("public_ipaddr")
    return f"wss://{ip}:{p[0]['HostPort']}" if (p and ip) else ""

# ---- web server (serves client/ so the page + status.json are reachable) + open Chrome ----
def serve():
    os.chdir(CLIENT)
    h = http.server.SimpleHTTPRequestHandler
    socketserver.TCPServer.allow_reuse_address = True
    socketserver.TCPServer(("127.0.0.1", PORT_WEB), h).serve_forever()

def main():
    write_status()  # reset to Renting
    threading.Thread(target=serve, daemon=True).start()
    url = f"http://localhost:{PORT_WEB}/index.html"
    for _ in range(20):
        try:
            urllib.request.urlopen(url, timeout=2); break
        except Exception: time.sleep(0.3)
    subprocess.run(["open", "-a", "Google Chrome", url])
    print("opened", url, flush=True)

    resume = os.environ.get("DESKTOPIA_RESUME")   # attach to an already-running instance, skip renting
    for attempt in (1, 2, 3):
        if resume:
            iid = int(resume); say(f"Resuming instance {iid}…")
        else:
            say(f"Renting a GPU (try {attempt})…")
            offer = best_offer()
            if not offer:
                say("No offers matched, retrying…"); time.sleep(5); continue
            iid = create(offer)
            if not iid:
                say("Create failed, retrying…"); time.sleep(5); continue
        print(f"[attempt {attempt}] instance={iid}" + ("" if resume else f" offer={offer}"), flush=True)
        t0 = time.time()

        # Booting: host pulls the image -> watch the DAEMON stream.
        say("Booting the machine (pulling image)…")
        inst = None
        for _ in range(120):
            inst = refresh_logs(iid)
            if inst and inst.get("actual_status") == "running": break
            time.sleep(2)
        if not inst or inst.get("actual_status") != "running":
            say("Host never started, retrying…"); destroy(iid); continue

        # Ports
        say("Allocating network ports…")
        for _ in range(30):
            inst = refresh_logs(iid)
            if stream_endpoint(inst): break
            time.sleep(3)
        endpoint = stream_endpoint(inst)
        if not endpoint:
            say("Bad host (no ports), retrying…"); destroy(iid); continue

        # SSH up. A perms failure here is NOT a bad host -- it's vast's StrictModes/ownership quirk,
        # the same on every box, which the onstart chown loop heals within a few seconds. So we wait
        # LONGER here and, on failure, do NOT cycle hosts (cycling can't help) -- keep the box so the
        # cause is inspectable.
        say("Connecting to the machine (waiting for vast ssh perms to settle)…")
        tgt = ssh_target(iid); ok = False
        for _ in range(40):
            if tgt and sshk(tgt, "echo ok").returncode == 0: ok = True; break
            refresh_logs(iid); time.sleep(3); tgt = ssh_target(iid)
        if not ok:
            write_status(phase=f"SSH never authorized (perms) — keeping box {iid} for inspection")
            print(f"SSH FAIL on {iid}; not cycling. Inspect: vastai logs {iid}", flush=True)
            return

        # Provision: rsync repo, start the stack, watch the CONTAINER stream until the cert appears.
        say("Setting up the desktop…")
        subprocess.run(["rsync", "-a", "--exclude", ".git", "--exclude", "__pycache__",
                        "--exclude", ".venv", "-e", "ssh " + " ".join(SOPTS) + " -p " + tgt[2],
                        "./", f"{tgt[0]}@{tgt[1]}:/root/desktopia/"], cwd=HERE, timeout=180)
        # Launch the stack detached. `setsid` makes entrypoint a new session leader, so even though
        # this ssh won't cleanly return (the daemons keep the channel open) and we time out, killing
        # the local ssh does NOT kill the remote stack -- so a timeout here just means "launched".
        try:
            sshk(tgt, "cd /root/desktopia && setsid bash entrypoint-wayland.sh "
                      "> /tmp/desktopia-stream.log 2>&1 </dev/null & echo launched", timeout=20)
        except subprocess.TimeoutExpired:
            print("  launch ssh didn't return (entrypoint detached via setsid) — watching for cert",
                  flush=True)
        say("Provisioning the desktop (compositor + Slicer)…")
        cert = ""
        for _ in range(150):
            # One ssh per poll: grab the real stack log tail (shown in the container pane -- far more
            # useful than vast's sshd-verbose container log) AND the cert. cut -d= -f2- keeps the full
            # base64 incl. its trailing '=' pad; a greedy `sed s/.*=//` would strip to empty.
            r = sshk(tgt, "tail -25 /tmp/desktopia-stream.log 2>/dev/null; echo __CERTLINE__; "
                          "grep -a CERT_SHA256_BASE64 /tmp/desktopia-stream.log 2>/dev/null | tail -1 | cut -d= -f2-")
            tail, _, c = r.stdout.rpartition("__CERTLINE__")
            cert = c.strip()
            inst = instance(iid)
            head = ""
            if inst:
                msg = (inst.get("status_msg") or "").strip().splitlines()
                head = inst.get("actual_status", "?") + (" — " + msg[-1][:90] if msg else "")
            write_status(headline=head, log_container=tail.strip()[-7000:], log_daemon=get_log(iid, True)[-7000:])
            if cert: break
            time.sleep(2)
        if not cert:
            say("Stream never came up, retrying…"); destroy(iid); continue

        dt = int(time.time() - t0)
        refresh_logs(iid)
        write_status(phase="Connecting to the live stream…", ready=True, ip_port=endpoint, cert=cert,
                     ws=ws_endpoint(inst), transport=os.environ.get("DESKTOPIA_TRANSPORT", "webtransport"))
        print(f"=== READY === id={iid} stream={endpoint} cert={cert} ready=+{dt}s ({dt//60}m{dt%60}s)",
              flush=True)
        # keep serving so the page can reach status.json / reload during the session
        try:
            while True: refresh_logs(iid); time.sleep(10)
        except KeyboardInterrupt:
            pass
        return
    say("Gave up after 3 hosts — check the logs.")

if __name__ == "__main__":
    main()
