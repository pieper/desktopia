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
import base64, http.server, json, os, re, shlex, socket, socketserver, ssl, subprocess, sys, threading, time
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
# The session settings this run hands the box, written to /root/desktopia/.desktopia.env (which
# entrypoint-wayland.sh sources) so they also survive the onstart's reboot after a vast stop/start.
# DESKTOPIA_MCP=1 is `vast.sh slicer-up`: Slicer's MCP server on :2026 for the ssh tunnel.
BOX_ENV  = ("DESKTOPIA_MCP", "DESKTOPIA_MCP_PORT", "DESKTOPIA_OFFLOAD", "DESKTOPIA_SOURCE",
            "DESKTOPIA_WIDTH", "DESKTOPIA_HEIGHT", "DESKTOPIA_FPS", "DESKTOPIA_BITRATE")
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
# Candidate hosts are qualified on worker threads (see qualify), but every vast API call and `vastai`
# CLI run still goes through this one lock, one at a time -- the single-owner rule above. Instance
# status comes from one shared, briefly cached listing, so N candidates don't poll N times (vast 429s).
_VAST = threading.RLock()
def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + KEY, "Accept": "application/json",
                 "Content-Type": "application/json"})
    with _VAST, urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

_inst_cache = {"t": 0.0, "by_id": {}}
def instance(iid, max_age=3.0):
    with _VAST:
        if time.time() - _inst_cache["t"] > max_age:
            try:
                ds = api("GET", "/api/v0/instances/?owner=me").get("instances", [])
                _inst_cache.update(t=time.time(), by_id={d.get("id"): d for d in ds})
            except Exception as e:
                print("  (status fetch failed:", type(e).__name__, e, ")", flush=True)
                return None
        return _inst_cache["by_id"].get(iid)

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
    with _VAST:
        return subprocess.run(["vastai", *args], capture_output=True, text=True, timeout=120, **kw)
# Hosts we rejected for broken hardware GLX (see gl_host_problem), one "machine_id host_id  # why"
# line each. A rejected machine stays bad (it's the host kernel's config), and so does the rest of
# its provider's fleet in practice (one vast host_id gave three modeset=N machines in a row), so
# every later search excludes both the machines and their host_ids.
BAD_MACHINES = os.path.join(HERE, ".bad-machines")
_FILES = threading.Lock()     # .bad-machines / .host-tests.jsonl are appended from worker threads
def bad_ids(col):                   # col 0: machine_ids, col 1: host_ids
    try:
        rows = [l.split("#")[0].split() for l in open(BAD_MACHINES)]
    except FileNotFoundError:
        return []
    return sorted({r[col] for r in rows if len(r) > col and r[col].isdigit()})
def mark_bad(inst, why):
    mid, hid = (inst or {}).get("machine_id"), (inst or {}).get("host_id")
    with _FILES:
        if mid and str(mid) not in bad_ids(0):
            with open(BAD_MACHINES, "a") as f: f.write(f"{mid} {hid or '-'}  # {time.strftime('%Y-%m-%d')} {why}\n")
def candidate_offers(k, skip_hosts=()):
    """Up to k offer ids, each from a different provider (host_id), best-ranked first (scripts/offers.py,
    which prefers RTTs measured by earlier runs over vast's geolocation). skip_hosts: tried this run."""
    env = dict(os.environ)
    block = env.get("DESKTOPIA_BLOCKLIST", "")   # vast.sh appends DESKTOPIA_BLOCKLIST to its search query
    for field, ids in (("machine_id", bad_ids(0)), ("host_id", sorted(set(bad_ids(1)) | {str(h) for h in skip_hosts}))):
        if ids: block += f" {field} notin [{','.join(ids)}]"
    env["DESKTOPIA_BLOCKLIST"] = block.strip()
    r = subprocess.run(["bash", os.path.join(HERE, "vast.sh"), "candidates", str(k)], env=env,
                       capture_output=True, text=True, timeout=120)
    if r.stderr.strip(): print(r.stderr.strip(), flush=True)
    # offers.py's "candidate: id=N ... ~Rms" lines carry each offer's ranking RTT (measured or estimated)
    est = {m.group(1): float(m.group(2)) for m in re.finditer(r"candidate: id=(\d+) .*?~(\d+(?:\.\d+)?)ms", r.stderr)}
    return [(l.strip(), est.get(l.strip())) for l in r.stdout.splitlines() if l.strip().isdigit()]
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
# Hardware GLX for X11 apps (Slicer) needs Xwayland's glamor, which needs NVIDIA's GBM backend, which
# needs the HOST's nvidia-drm loaded with modeset=1. Hosts with modeset=N still stream fine (the
# compositor uses the EGL device platform), but glamor falls back to software and every GLX context
# fails -> Slicer's "QOpenGLWidget: Failed to create context" / "composeAndFlush: makeCurrent() failed"
# and a black window. Not fixable in the container, so such hosts are rejected like any other bad host.
# The software path (DESKTOPIA_SOURCE=xvfb) never uses the GPU for GL, so it skips both checks.
GL_CHECKED = os.environ.get("DESKTOPIA_SOURCE", "auto") != "xvfb"
def gl_host_problem(tgt):           # before provisioning: the cheap kernel-side precondition
    r = sshk(tgt, "cat /sys/module/nvidia_drm/parameters/modeset 2>/dev/null || echo missing")
    v = r.stdout.strip()
    return None if v == "Y" else f"nvidia-drm modeset={v or '?'} (no GBM -> no hardware GLX)"
def gl_stack_problem(tgt):          # after the stack is up: did Xwayland actually get glamor?
    r = sshk(tgt, "grep -m1 -E 'Failed to initialize glamor|glamor_egl_get_display' /tmp/xway.log 2>/dev/null")
    return f"Xwayland: {r.stdout.strip()}" if r.stdout.strip() else None

# ---- measured qualification. vast's geolocation is not the network path (a "Pennsylvania" offer
# measured 240 ms from Boston: the box was in the UK), so each candidate is MEASURED from here once its
# ssh is up, over the direct ssh-url IP:port (the box itself, not a vast proxy):
#   rtt   median TCP connect time from this machine (what every frame and every click pays)
#   down  Mbit/s this machine pulls from the box over ssh (the stream's direction)
#   fetch MB/s the box pulls from download.slicer.org (the 475 MB Slicer fetch: 1.4 MB/s = 6 min)
# Thresholds are env-overridable. TCP `down` overstates what the QUIC stream gets on a long path
# (aioquic's congestion control: 20 Mbit/s over ssh vs 1.8 Mbit/s of stream at 240 ms), so RTT is the
# limit that matters most and the one candidates are ranked by. ----
MAX_RTT_MS    = float(os.environ.get("DESKTOPIA_MAX_RTT_MS", "80"))
MIN_DOWN_MBPS = float(os.environ.get("DESKTOPIA_MIN_DOWN_MBPS", "30"))
MIN_FETCH_MBS = float(os.environ.get("DESKTOPIA_MIN_FETCH_MBS", "8"))
SLICER_URL    = "https://download.slicer.org/download?os=linux&stability=release"   # as entrypoint fetches
HOST_TESTS    = os.path.join(HERE, ".host-tests.jsonl")   # every measurement; offers.py ranks by it

def measure_rtt(tgt, n=5):
    _, host, port = tgt; ts = []
    for _ in range(n):
        t = time.monotonic()
        try:
            with socket.create_connection((host, int(port)), timeout=5): ts.append((time.monotonic() - t) * 1000)
        except OSError:
            pass
    return sorted(ts)[len(ts) // 2] if ts else None

def measure_down(tgt, nbytes=16 << 20, timeout=60):
    """Mbit/s pulled from the box, timed from the first byte (ssh's handshake is not the link)."""
    user, host, port = tgt
    p = subprocess.Popen(["ssh", *SOPTS, "-p", port, f"{user}@{host}",
                          f"echo GO; head -c {nbytes} /dev/urandom"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    killer = threading.Timer(timeout, p.kill); killer.start()
    try:
        if p.stdout.readline() != b"GO\n": return None
        t, got = time.monotonic(), 0
        while chunk := p.stdout.read(1 << 16): got += len(chunk)
        dt = time.monotonic() - t
        return got * 8 / dt / 1e6 if got >= nbytes and dt > 0 else None
    finally:
        killer.cancel(); p.kill(); p.wait()

def measure_fetch(tgt):
    r = sshk(tgt, f"curl -sL -o /dev/null -m 10 -w '%{{speed_download}}' '{SLICER_URL}'", timeout=30)
    try: return float(r.stdout.strip().splitlines()[-1]) / 1e6
    except (ValueError, IndexError): return None

def record_test(c):
    inst = c.get("inst") or {}
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "host_id": inst.get("host_id"),
           "machine_id": inst.get("machine_id"), "geo": inst.get("geolocation"), "dph": inst.get("dph_total"),
           **{k: c.get(k) for k in ("rtt_ms", "down_mbps", "fetch_mbs", "verdict")}}
    with _FILES, open(HOST_TESTS, "a") as f: f.write(json.dumps(row) + "\n")

def await_ssh(c, boot_s=240, ports_s=90):
    """Booted, ports mapped, ssh authorized -> c['inst'], c['tgt']; else c['why']. Returns ok."""
    iid = c["iid"]
    c["phase"] = "booting"
    for _ in range(boot_s // 2):
        inst = instance(iid)
        if inst and inst.get("actual_status") == "running": break
        time.sleep(2)
    else:
        c["why"] = "host never started"; return False
    c["phase"] = "ports"
    for _ in range(ports_s // 3):
        inst = instance(iid)
        if stream_endpoint(inst): break
        time.sleep(3)
    else:
        c["why"] = "no ports"; return False
    c["inst"] = inst
    # A perms failure here is vast's StrictModes/ownership quirk (same on every box; the onstart chown
    # loop heals it within seconds), hence the long wait.
    c["phase"] = "ssh"
    tgt = ssh_target(iid)
    for _ in range(40):
        if tgt and sshk(tgt, "echo ok").returncode == 0: c["tgt"] = tgt; return True
        time.sleep(3); tgt = ssh_target(iid)
    c["why"] = "ssh never authorized"; c["ssh_fail"] = True; return False

def qualify(c):
    """Worker thread for one candidate: boot, ssh, then measure. Sets c['ok'] / c['why']; always c['done']."""
    try:
        if not await_ssh(c): return
        if GL_CHECKED:
            c["phase"] = "gl"
            if why := gl_host_problem(c["tgt"]):     # rtt still goes in the history: it ranks the provider
                c["why"] = why; mark_bad(c["inst"], why); c["verdict"] = "bad-gl"
                c["rtt_ms"] = measure_rtt(c["tgt"]); record_test(c); return
        c["phase"] = "net"
        c["rtt_ms"] = measure_rtt(c["tgt"])
        c["down_mbps"] = measure_down(c["tgt"])
        c["fetch_mbs"] = measure_fetch(c["tgt"])
        fails = [f"{name} {'?' if v is None else f'{v:.0f}'} {u} (limit {lim:.0f})"
                 for name, v, u, lim, hi in (("rtt", c["rtt_ms"], "ms", MAX_RTT_MS, True),
                                             ("down", c["down_mbps"], "Mbit/s", MIN_DOWN_MBPS, False),
                                             ("fetch", c["fetch_mbs"], "MB/s", MIN_FETCH_MBS, False))
                 if v is None or (v > lim if hi else v < lim)]
        c["ok"] = not fails; c["why"] = ", ".join(fails) or None
        c["verdict"] = "ok" if c["ok"] else "slow"; record_test(c)
    except Exception as e:
        c["why"] = f"{type(e).__name__}: {e}"
    finally:
        c["done"] = True; c["phase"] = "passed" if c.get("ok") else "rejected"

def describe(c):
    m = " ".join(f"{v:.0f}{u}" for v, u in ((c.get("rtt_ms"), "ms"), (c.get("down_mbps"), "Mb/s"),
                                             (c.get("fetch_mbs"), "MB/s fetch")) if v is not None)
    geo = (c.get("inst") or {}).get("geolocation") or "?"
    return f"{c['iid']} [{geo}] {c['phase']} {m}" + (f" -- {c['why']}" if c.get("why") else "")

def stream_endpoint(inst):
    if not inst: return None        # a failed fetch (e.g. vast's 429) -- the caller's loop polls again
    p = (inst.get("ports") or {}).get("4433/udp")
    ip = inst.get("public_ipaddr")
    return f"{ip}:{p[0]['HostPort']}" if (p and ip) else None

def ws_endpoint(inst):              # the WS/TCP transport (wss with our self-signed cert on vast direct)
    if not inst: return ""
    p = (inst.get("ports") or {}).get("4434/tcp")
    ip = inst.get("public_ipaddr")
    return f"wss://{ip}:{p[0]['HostPort']}" if (p and ip) else ""

# ---- web server (serves client/ so the page + status.json are reachable) + open Chrome ----
def serve():
    os.chdir(CLIENT)
    h = http.server.SimpleHTTPRequestHandler
    socketserver.TCPServer.allow_reuse_address = True
    socketserver.TCPServer(("127.0.0.1", PORT_WEB), h).serve_forever()

def probe():
    """DESKTOPIA_PROBE=1 (`vast.sh probe`): one round of candidates, measured and recorded in
    .host-tests.jsonl, then ALL destroyed (winner too). Builds the measured history offers.py ranks by,
    for a few cents, without leaving a box running."""
    c = pick_host(1, set())
    if c: destroy(c["iid"])
    print("probe done; history in", HOST_TESTS, flush=True)

def main():
    if os.environ.get("DESKTOPIA_PROBE"): return probe()
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
    if resume:
        c = {"iid": int(resume)}; say(f"Resuming instance {resume}…")
        if not await_ssh(c):
            write_status(phase=f"Instance {resume}: {c['why']}"); print("RESUME FAIL:", c["why"], flush=True); return
        if GL_CHECKED and (why := gl_host_problem(c["tgt"])):   # never destroy a box we were asked to attach to
            print(f"WARNING: instance {resume} has no hardware GLX: {why}", flush=True)
        provision(c, resume=True)
        return

    tried = set()          # host_ids tested this run: a later round goes to different providers
    for rnd in range(1, ROUNDS + 1):
        c = pick_host(rnd, tried)
        if c and provision(c, resume=False): return
        if c: destroy(c["iid"])
    say(f"Gave up after {ROUNDS} rounds of {CANDIDATES} candidates — check the logs.")

CANDIDATES = int(os.environ.get("DESKTOPIA_CANDIDATES", "3"))   # boxes rented and tested in parallel
ROUNDS     = int(os.environ.get("DESKTOPIA_ROUNDS", "2"))
# Once one candidate passes, stop at once if no untested candidate could beat it (its ranking RTT is
# no better), else wait up to GRACE_S for those: the closest offers can be the slowest to boot (a
# 45 s cut once dropped two ~12 ms candidates still booting in favor of a 62 ms pass).
GRACE_S    = 240
QUALIFY_S  = 600    # whole round's deadline (boot + image pull + ssh + tests)

def pick_host(rnd, tried):
    """Rent CANDIDATES offers from different providers at once, qualify them in parallel, keep the best
    that passes (lowest measured RTT, then price) and destroy the rest. Returns the winner or None.
    A few minutes of the losers' rent (~$0.05 each) buys not finding out 10 minutes in."""
    say(f"Renting {CANDIDATES} candidate GPUs from different providers (round {rnd})…")
    offers = candidate_offers(CANDIDATES, tried)
    if not offers:
        say("No offers matched."); time.sleep(5); return None
    cands = []
    for offer, est_ms in offers:
        iid = create(offer)
        print(f"[round {rnd}] offer={offer} -> instance={iid}", flush=True)
        if iid: cands.append({"iid": iid, "offer": offer, "est_ms": est_ms, "phase": "created"})
    if not cands:
        say("Create failed."); time.sleep(5); return None
    for c in cands: threading.Thread(target=qualify, args=(c,), daemon=True).start()

    say(f"Testing {len(cands)} candidate hosts (GPU GL, latency, bandwidth)…")
    t0, first_ok = time.time(), None
    while True:
        oks = [c for c in cands if c.get("ok")]
        if oks and first_ok is None: first_ok = time.time()
        n_done = sum(bool(c.get("done")) for c in cands)
        write_status(headline=f"{n_done}/{len(cands)} candidates tested, {len(oks)} passed",
                     log_container="\n".join(describe(c) for c in cands), log_daemon="")
        best = min((c["rtt_ms"] for c in oks), default=None)
        could_beat = [c for c in cands if not c.get("done") and best is not None
                      and (c.get("est_ms") is None or c["est_ms"] < best)]
        if (n_done == len(cands) or (first_ok and (not could_beat or time.time() - first_ok > GRACE_S))
                or time.time() - t0 > QUALIFY_S):
            break
        time.sleep(3)

    winner = min(oks, key=lambda c: (c["rtt_ms"], (c["inst"] or {}).get("dph_total") or 0)) if oks else None
    for c in cands:
        print(("  KEEP   " if c is winner else "  reject ") + describe(c), flush=True)
        inst = c.get("inst") or instance(c["iid"], max_age=0) or {}
        if inst.get("host_id"): tried.add(inst["host_id"])
        if c is not winner: destroy(c["iid"])
    if all(c.get("ssh_fail") for c in cands):
        print("ssh failed on EVERY candidate: likely the key/agent, not the hosts (see vast.sh slicer-up)", flush=True)
    if not winner:
        say(f"No candidate passed (round {rnd}).")
    return winner

def provision(c, resume):
    """rsync the repo, start the stack, wait for the cert, then serve forever. False = this host failed
    (the caller destroys it); a resumed box is never failed, only warned about."""
    iid, tgt, inst = c["iid"], c["tgt"], c.get("inst") or instance(iid)
    endpoint, t0 = stream_endpoint(inst), time.time()
    say("Setting up the desktop…")
    subprocess.run(["rsync", "-a", "--exclude", ".git", "--exclude", "__pycache__",
                    "--exclude", ".venv", "-e", "ssh " + " ".join(SOPTS) + " -p " + tgt[2],
                    "./", f"{tgt[0]}@{tgt[1]}:/root/desktopia/"], cwd=HERE, timeout=180)
    # rewritten every launch (empty when nothing is set), so a previous run's settings never linger
    box_env = "".join(f"{k}={shlex.quote(os.environ[k])}\n" for k in BOX_ENV if os.environ.get(k))
    sshk(tgt, f"printf %s {shlex.quote(box_env)} > /root/desktopia/.desktopia.env")
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
        tail, _, cl = r.stdout.rpartition("__CERTLINE__")
        cert = cl.strip()
        inst = instance(iid) or inst
        msg = (inst.get("status_msg") or "").strip().splitlines()
        head = inst.get("actual_status", "?") + (" — " + msg[-1][:90] if msg else "")
        write_status(headline=head, log_container=tail.strip()[-7000:], log_daemon=get_log(iid, True)[-7000:])
        if cert: break
        time.sleep(2)
    if not cert:
        say("Stream never came up."); return False
    if GL_CHECKED and (why := gl_stack_problem(tgt)):
        print(f"{'WARNING' if resume else 'bad host'} {iid} (machine {inst.get('machine_id')}): {why}", flush=True)
        if not resume:
            say("Bad host (Xwayland has no hardware GL)."); mark_bad(inst, why); return False

    dt = int(time.time() - t0)
    refresh_logs(iid)
    write_status(phase="Connecting to the live stream…", ready=True, ip_port=endpoint, cert=cert,
                 ws=ws_endpoint(inst), transport=os.environ.get("DESKTOPIA_TRANSPORT", "webtransport"))
    print(f"=== READY === id={iid} stream={endpoint} cert={cert} ready=+{dt}s ({dt//60}m{dt%60}s)"
          + (f" rtt={c['rtt_ms']:.0f}ms down={c['down_mbps']:.0f}Mbit/s" if c.get("rtt_ms") and c.get("down_mbps") else ""),
          flush=True)
    # keep serving so the page can reach status.json / reload during the session
    try:
        while True: refresh_logs(iid); time.sleep(10)
    except KeyboardInterrupt:
        pass
    return True

if __name__ == "__main__":
    main()
