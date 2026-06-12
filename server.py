"""Desktopia streaming server: GStreamer screen capture -> H.264 -> QUIC datagrams + WebSocket.

Two capture sources, chosen by --source (default: auto-detect):
  - wayland: gst-wayland-display's `waylanddisplaysrc`, a headless GPU Smithay compositor that
             renders on a DRM render node (hardware GL, NVENC). Xwayland/Slicer are its clients.
  - xvfb:    `ximagesrc` capturing an existing X server (Xvfb on :2) -- pure software (Mesa
             llvmpipe), no GPU / no DRM render node. The CPU path for Colab and cheap CPU hosts.
Auto-detect picks wayland when a render node AND the compositor plugin are present, else xvfb.

Either way each frame is H.264-encoded (NVENC if available, else software x264), fragmented into
QUIC datagrams (WebTransport) and/or sent whole over WebSocket, and fanned out to every viewer.
A reliable client byte (type 0) means "send me a keyframe".

In wayland mode the compositor pipeline IS the display, so it must start before Xwayland; in xvfb
mode Xvfb is started first (by session-wayland.sh) and this server just captures it.
"""
import argparse, asyncio, concurrent.futures, email.utils, http, json, os, ssl, struct, subprocess
import gi
import websockets       # TCP/WSS transport for hosts that only allow HTTP ingress (e.g. NRP)
from websockets.http11 import Response                    # serve the client page on the same port as the WS
from websockets.datastructures import Headers             # so the ingress sees one HTTP-answering backend
gi.require_version("Gst", "1.0")
from gi.repository import Gst
from aioquic.asyncio import serve
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ProtocolNegotiated, StreamDataReceived
from aioquic.h3.connection import H3Connection
from aioquic.h3.events import HeadersReceived, WebTransportStreamDataReceived

Gst.init(None)

MTU = 1100                          # safe QUIC datagram payload (path MTU ~1200)
HDR = struct.Struct(">IBHH")        # frame_id(u32), flags(u8), n_chunks(u16), chunk_idx(u16)
FLAG_KEY = 0x01
# Defaults for capture size / rate / bitrate (overridable via CLI -- the CPU/Colab path drops these
# to e.g. 1280x720@15 so software encode keeps up). The keyframe interval is tied to fps (= ~1s).
DEF_W, DEF_H, DEF_FPS, DEF_BITRATE = 1920, 1080, 60, 12000

# Client->server input protocol (reliable stream), fixed length per message type:
#  0 keyframe-req[1]  1 move[1+x:u16+y:u16]  2 mousedown[1+btn]  3 mouseup[1+btn]
#  4 wheel[1+dir(0=up,1=down)]  5 keydown[1+keysym:u16]  6 keyup[1+keysym:u16]
MSG_LEN = {0: 1, 1: 5, 2: 2, 3: 2, 4: 2, 5: 3, 6: 3}

# Control channel (separate reliable WT stream, tagged with a 0xC7 magic first byte so the input
# stream above is untouched). Newline-delimited JSON both ways. EXPLICIT clipboard only -- nothing
# syncs automatically; the user pushes/pulls via the control panel. Clipboard lives in the X11
# CLIPBOARD selection on :2 (where Slicer/apps run), driven by xclip.
CTRL_MAGIC = 0xC7

# XTEST/Xlib is synchronous and round-trips to the X server, which can stall when the server is busy
# (e.g. Slicer starting up). NEVER run it on the asyncio event loop -- a stalled Xlib call would freeze
# video, the HTTP page, and accepting new viewers (observed: a viewer connecting wedged the whole server
# at inj.reset()). Route every injector call through ONE dedicated thread: off the loop, and serialized
# because python-xlib is not thread-safe.
_X_EXEC = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="xinject")

def clipboard_set(text):
    try:
        subprocess.run(["xclip", "-selection", "clipboard", "-i"], input=(text or "").encode("utf-8"),
                       env={**os.environ, "DISPLAY": ":2"}, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print("clip-set failed:", e, flush=True)

def clipboard_get():
    try:
        r = subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True,
                           env={**os.environ, "DISPLAY": ":2"}, timeout=5)
        return r.stdout.decode("utf-8", "replace")
    except Exception as e:
        print("clip-get failed:", e, flush=True); return ""


def dispatch_input(m, inj, b):                  # one fixed-length input message -> XTEST (shared QUIC+WS)
    if not m: return
    t = m[0]
    if   t == 0: b.force_keyframe()
    elif t == 1 and len(m) >= 5: inj.move((m[1] << 8) | m[2], (m[3] << 8) | m[4])
    elif t == 2 and len(m) >= 2: inj.button(m[1], True)
    elif t == 3 and len(m) >= 2: inj.button(m[1], False)
    elif t == 4 and len(m) >= 2: inj.wheel(m[1] == 1)
    elif t == 5 and len(m) >= 3: inj.key((m[1] << 8) | m[2], True)
    elif t == 6 and len(m) >= 3: inj.key((m[1] << 8) | m[2], False)

def handle_control(m, reply, b):                # NDJSON control message (shared QUIC+WS)
    t = m.get("t")
    if   t == "ping":     reply({"t": "pong", "id": m.get("id")})       # RTT
    elif t == "clip-set": clipboard_set(m.get("text", ""))             # explicit push
    elif t == "clip-get": reply({"t": "clip", "text": clipboard_get()})  # explicit pull


class Injector:
    """Replays browser input into the X app on :2 via XTEST (Slicer is an X client there)."""
    def __init__(self, disp=":2"):
        self.dispname = disp
        self.d = None

    def _ok(self):
        if self.d is None:
            try:
                from Xlib import display
                self.d = display.Display(self.dispname)
            except Exception as e:
                print("injector: cannot open", self.dispname, e, flush=True)
                return False
        return True

    def move(self, x, y):
        if not self._ok(): return
        from Xlib import X
        from Xlib.ext import xtest
        xtest.fake_input(self.d, X.MotionNotify, x=x, y=y); self.d.sync()

    def button(self, btn, press):
        if not self._ok() or not btn: return
        from Xlib import X
        from Xlib.ext import xtest
        xtest.fake_input(self.d, X.ButtonPress if press else X.ButtonRelease, btn); self.d.sync()

    def wheel(self, down):
        self.button(5 if down else 4, True); self.button(5 if down else 4, False)

    def key(self, keysym, press):
        if not self._ok() or not keysym: return
        from Xlib import X
        from Xlib.ext import xtest
        kc = self.d.keysym_to_keycode(keysym)
        if kc:
            xtest.fake_input(self.d, X.KeyPress if press else X.KeyRelease, kc); self.d.sync()

    def reset(self):
        """Release any stuck buttons/modifiers so a reconnect (page reload) recovers cleanly
        from a drag that ended off-canvas or a key whose release was missed."""
        for b in (1, 2, 3):
            self.button(b, False)
        for ks in (0xFFE1, 0xFFE2, 0xFFE3, 0xFFE4, 0xFFE5,   # Shift_L/R, Control_L/R, Caps_Lock
                   0xFFE7, 0xFFE8, 0xFFE9, 0xFFEA):           # Meta_L/R, Alt_L/R
            self.key(ks, False)


def _enc_props(name):
    """The property names an encoder element ACTUALLY exposes (varies a lot across nvcodec/x264
    versions). We use this to add low-latency knobs only when present -- an unknown property passed to
    Gst.parse_launch fails the WHOLE pipeline, and here that tears down the compositor + desktop."""
    try:
        el = Gst.ElementFactory.make(name, None)
        return {p.name for p in el.list_properties()} if el is not None else set()
    except Exception:
        return set()


def encoder_bin(fps, bitrate):
    """Prefer hardware NVENC; fall back to software x264. h264parse config-interval=-1 prepends
    SPS/PPS to EVERY keyframe, so a client that connects (or reconnects) mid-stream gets a
    self-contained IDR it can actually decode -- without it x264enc emits AUD+IDR with no parameter
    sets except at stream start, and every late joiner silently decodes nothing (consumes frames,
    0 output, no error). A fps-length keyframe interval (~1s) also lets the WebSocket path -- which,
    unlike the datagram path, can't detect a single dropped AU -- self-heal corruption within ~1s.
    Constrain to H.264 High (browser WebCodecs avc1.640028); byte-stream/AU = Annex-B framing.

    LOW LATENCY: NVENC's defaults keep B-frames AND a rate-control lookahead, which hold several
    frames in the encoder before output -- the single biggest avoidable latency on the GPU path. We
    force zero B-frames, no lookahead, and a low-delay CBR rate control, but ONLY via knobs the
    installed nvcodec build exposes (introspected above) so an old/renamed property can't crash the
    compositor pipeline. x264's tune=zerolatency already implies bframes=0 + no lookahead + sliced VBV."""
    if Gst.ElementFactory.find("nvh264enc"):
        p = _enc_props("nvh264enc")
        parts = [f"nvh264enc name=enc bitrate={bitrate} gop-size={fps}"]
        if "zerolatency" in p:    parts.append("zerolatency=true")   # nvcodec all-in-one low-delay switch
        if "bframes" in p:        parts.append("bframes=0")
        elif "b-frames" in p:     parts.append("b-frames=0")
        if "rc-lookahead" in p:   parts.append("rc-lookahead=0")
        if "rc-mode" in p:        parts.append("rc-mode=cbr")        # CBR low-delay; "cbr" nick is standard
        enc = " ".join(parts)
    else:
        # ultrafast (not veryfast): ~half the per-frame encode TIME -> lower latency AND CPU headroom
        # to run a higher capture rate; the small bitrate-efficiency loss is invisible on a static UI.
        enc = f"x264enc name=enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate} key-int-max={fps}"
    return (f"{enc} ! video/x-h264,profile=high "
            "! h264parse config-interval=-1 "
            "! video/x-h264,stream-format=byte-stream,alignment=au")


def render_node():
    """waylanddisplaysrc defaults to /dev/dri/renderD128, but the GPU's render node varies per
    host -- on vast it can be renderD129/130 when the GPU isn't card0. Pick the one present, or
    the compositor opens a nonexistent node and reports 'Supported DMA formats: []' and never
    creates its Wayland socket."""
    import glob
    nodes = sorted(glob.glob("/dev/dri/renderD*"))
    return nodes[0] if nodes else "/dev/dri/renderD128"


def resolve_source(requested):
    """Decide between the GPU Wayland compositor and the software Xvfb capture. Explicit wins;
    'auto' uses the GPU path only when BOTH a DRM render node and the compositor plugin exist
    (so a CUDA-only/no-GPU host like Colab cleanly falls to the software path)."""
    if requested in ("wayland", "xvfb"):
        return requested
    import glob
    have_node = bool(glob.glob("/dev/dri/renderD*"))
    have_comp = Gst.ElementFactory.find("waylanddisplaysrc") is not None
    src = "wayland" if (have_node and have_comp) else "xvfb"
    print(f"source auto-detect: render_node={have_node} waylanddisplaysrc={have_comp} -> {src}", flush=True)
    return src


def build_pipeline(source, w, h, fps, bitrate, display=":2"):
    enc = encoder_bin(fps, bitrate)
    sink = "appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true"
    if source == "xvfb":
        # Capture an already-running X server (Xvfb on :2) -- pure software, no DRM render node.
        # ximagesrc reads the screen at its native WxH (Xvfb is created at the requested size, so no
        # videoscale needed); use-damage only re-grabs changed regions (cheap for a mostly-static UI).
        return (f"ximagesrc display-name={display} use-damage=true show-pointer=true "
                f"! video/x-raw,framerate={fps}/1 ! videoconvert ! {enc} ! {sink}")
    # wayland: headless GPU Smithay compositor on a DRM render node (hardware GL; Xwayland clients).
    return (f"waylanddisplaysrc render-node={render_node()} "
            f"! video/x-raw,width={w},height={h},format=RGBx,framerate={fps}/1 "
            f"! videoconvert ! {enc} ! {sink}")


class Broadcaster:
    """Owns the GStreamer pipeline; pushes encoded AUs to all WebTransport sessions."""
    def __init__(self, loop, pipeline):
        self.loop = loop
        self.sessions = set()            # set[StreamProtocol]
        self.frame_id = 0
        print("pipeline:", pipeline, flush=True)
        self.pipe = Gst.parse_launch(pipeline)
        self.enc = self.pipe.get_by_name("enc")
        self.pipe.get_by_name("sink").connect("new-sample", self._on_sample)
        bus = self.pipe.get_bus(); bus.add_signal_watch()
        bus.connect("message::error", self._on_bus)
        bus.connect("message::eos", self._on_bus)

    def _on_bus(self, _bus, msg):
        # NOTE: a blind set_state(NULL)->PLAYING restart is NOT safe here -- waylanddisplaysrc IS the
        # Wayland compositor and Xwayland/Slicer are its clients, so restarting the pipeline tears the
        # whole desktop down. So log loudly; the browser auto-reconnects for transient stalls, and a
        # hard pipeline failure needs a session relaunch (session-wayland.sh). A true in-place
        # restart-watchdog would require splitting the compositor and encoder into separate pipelines.
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error(); print("GST PIPELINE ERROR:", err, dbg, flush=True)
        else:
            print("GST PIPELINE EOS", flush=True)

    def start(self):
        self.pipe.set_state(Gst.State.PLAYING)

    def force_keyframe(self):
        self.enc.send_event(Gst.Event.new_custom(
            Gst.EventType.CUSTOM_DOWNSTREAM, Gst.Structure.new_empty("GstForceKeyUnit")))

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(mi.data)
        is_key = not (buf.get_flags() & Gst.BufferFlags.DELTA_UNIT)
        buf.unmap(mi)
        self.loop.call_soon_threadsafe(self._fanout, data, is_key)   # GStreamer thread -> asyncio
        return Gst.FlowReturn.OK

    def _fanout(self, data, is_key):
        if not self.sessions:
            self.frame_id += 1
            return
        fid = self.frame_id & 0xFFFFFFFF
        self.frame_id += 1
        for sp in list(self.sessions):           # sessions are QUIC (StreamProtocol) or WS (WSSession)
            try:
                sp.send_video(fid, data, is_key)
            except Exception:
                pass


class StreamProtocol(QuicConnectionProtocol):
    broadcaster: Broadcaster = None
    injector: Injector = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._h3 = None
        self._session_id = None
        self._inbuf = b""
        self._ctrlbuf = b""
        self._stype = {}        # stream_id -> 'I'(nput) | 'C'(ontrol), classified by first byte
        self._ctrl_out = None   # server->client control stream id (NDJSON: pong, clip, status)

    def quic_event_received(self, event):
        if isinstance(event, ProtocolNegotiated):
            self._h3 = H3Connection(self._quic, enable_webtransport=True)
        elif self._h3 is not None:
            for e in self._h3.handle_event(event):   # input arrives as WebTransportStreamDataReceived
                self._on_h3(e)

    def _parse_input(self):
        buf, i, n = self._inbuf, 0, len(self._inbuf)
        while i < n:
            t = buf[i]
            ln = MSG_LEN.get(t)
            if ln is None:        # desync on unknown type — drop the rest
                i = n; break
            if i + ln > n:
                break
            m = buf[i:i + ln]; i += ln
            self._dispatch(m)
        self._inbuf = buf[i:]

    def _dispatch(self, m):
        self.broadcaster.loop.run_in_executor(_X_EXEC, dispatch_input, m, self.injector, self.broadcaster)

    def _parse_control(self):
        while b"\n" in self._ctrlbuf:
            line, self._ctrlbuf = self._ctrlbuf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                self._on_control(json.loads(line))
            except Exception as ex:
                print("ctrl parse:", ex, flush=True)

    def _on_control(self, m):
        handle_control(m, self._ctrl_send, self.broadcaster)

    def _ctrl_send(self, obj):
        if self._ctrl_out is None:
            return
        try:
            self._quic.send_stream_data(self._ctrl_out, (json.dumps(obj) + "\n").encode())
            self.transmit()
        except Exception:
            pass

    def _on_h3(self, e):
        if isinstance(e, HeadersReceived):
            hdrs = dict(e.headers)
            if hdrs.get(b":method") == b"CONNECT" and hdrs.get(b":protocol") == b"webtransport":
                self._session_id = e.stream_id
                self._h3.send_headers(e.stream_id, [(b":status", b"200")])
                self.broadcaster.sessions.add(self)
                self.broadcaster.loop.run_in_executor(_X_EXEC, self.injector.reset)  # off-loop; clear stuck keys
                try:                                       # server->client control stream (NDJSON)
                    self._ctrl_out = self._h3.create_webtransport_stream(
                        self._session_id, is_unidirectional=True)
                except Exception as ex:
                    print("ctrl stream create failed:", ex, flush=True)
                self.broadcaster.force_keyframe()         # new viewer needs an entry point
                self.transmit()
                print("viewer connected; sessions:", len(self.broadcaster.sessions), flush=True)
        elif isinstance(e, WebTransportStreamDataReceived):
            sid, data = e.stream_id, e.data
            typ = self._stype.get(sid)
            if typ is None:                                # classify the stream on its first byte
                if not data:
                    return
                typ = "C" if data[0] == CTRL_MAGIC else "I"
                self._stype[sid] = typ
                if typ == "C":
                    data = data[1:]
            if typ == "I":
                self._inbuf += data; self._parse_input()
            elif data:
                self._ctrlbuf += data; self._parse_control()

    def send_video(self, fid, data, is_key):     # fragment an AU into QUIC datagrams
        body = MTU - HDR.size
        chunks = [data[i:i + body] for i in range(0, len(data), body)] or [b""]
        n = len(chunks); flags = FLAG_KEY if is_key else 0
        for idx, c in enumerate(chunks):
            self.send_video_datagram(HDR.pack(fid, flags, n, idx) + c)

    def send_video_datagram(self, payload):
        try:
            self._h3.send_datagram(self._session_id, payload)
            self.transmit()
        except Exception:
            pass

    def connection_lost(self, exc):
        if self.broadcaster is not None:
            self.broadcaster.sessions.discard(self)
        super().connection_lost(exc)


class WSSession:
    """A browser connected over WebSocket (TCP/WSS) instead of QUIC -- the path for hosts that only
    expose HTTP ingress (e.g. NRP). Same role as StreamProtocol in broadcaster.sessions. Video out =
    binary frames (1-byte key flag + whole AU; TCP keeps boundaries, so no fragmentation). Control out
    = text (NDJSON). In: binary = one input message, text = NDJSON control."""
    def __init__(self, ws, b, inj):
        self.ws, self.b, self.inj = ws, b, inj
        # TWO queues drained by one writer: control (clipboard/pong) is kept OFF the video queue and
        # sent with strict priority, so a backlog of video AUs can never evict a clipboard reply. Video
        # is drop-oldest (a slow client should lag, not stall). The writer waits on an Event, NOT on the
        # video queue: use-damage pauses video on a static screen, and blocking on video would then stall
        # control replies whenever nothing is rendering.
        self.vq = asyncio.Queue(maxsize=2)       # whole H.264 AUs; drop-oldest under backpressure
        # (shallow on purpose: a deeper queue just shows the client a STALE backlog -- for an
        #  interactive stream we want it to skip to the newest frame, not play catch-up)
        self.ctrlq = asyncio.Queue(maxsize=64)   # NDJSON control (tiny, infrequent); effectively lossless
        self._wake = asyncio.Event()
        self.task = asyncio.ensure_future(self._writer())

    def send_video(self, fid, data, is_key):
        self._put(self.vq, (b"\x01" if is_key else b"\x00") + data)

    def _reply(self, obj):
        self._put(self.ctrlq, json.dumps(obj))   # str -> WS text frame

    def _put(self, q, item):
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:                # drop-oldest (video lag, or a wedged client)
            try: q.get_nowait()
            except Exception: pass
            try: q.put_nowait(item)
            except Exception: pass
        self._wake.set()

    async def _writer(self):
        try:
            while True:
                self._wake.clear()               # clear BEFORE draining, so a put during the drain re-wakes us
                sent = False
                while not self.ctrlq.empty():    # control first (priority)
                    await self.ws.send(self.ctrlq.get_nowait()); sent = True
                while not self.vq.empty():
                    await self.ws.send(self.vq.get_nowait()); sent = True
                if not sent:
                    await self._wake.wait()
        except Exception:
            pass


_CTYPES = {"html": "text/html; charset=utf-8", "js": "text/javascript", "mjs": "text/javascript",
           "json": "application/json", "css": "text/css", "svg": "image/svg+xml",
           "png": "image/png", "ico": "image/x-icon", "wasm": "application/wasm"}

def _http_response(status, body, ctype):
    """Build a Response the way ServerProtocol.reject() does -- the headers websockets needs to actually
    flush it. CRUCIAL: 'Connection: close' (a returned Response rejects the WS handshake, after which the
    server aborts the transport; without close the keep-alive response is discarded and the client gets
    nothing) plus Date/Content-Length/Content-Type."""
    return Response(status, http.HTTPStatus(status).phrase, Headers([
        ("Date", email.utils.formatdate(usegmt=True)),
        ("Connection", "close"),
        ("Content-Length", str(len(body))),
        ("Content-Type", ctype),
    ]), body)

def make_process_request(serve_dir):
    """Serve the client page over plain HTTP on the SAME port as the WebSocket. A k8s ingress (NRP's
    HAProxy) health-probes its backend with a plain GET; a WS-only server never answers, so the ingress
    marks the backend down and parks every /ws upgrade (symptom: curl to /ws hangs, then 504). With this,
    the one port answers both HTTP GETs (page + health) and WS upgrades, so a single ingress path '/' works."""
    root = os.path.realpath(serve_dir)
    async def process_request(connection, request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None                                   # let ws_handler take it
        rel = request.path.split("?", 1)[0].lstrip("/") or "index.html"
        fp = os.path.realpath(os.path.join(root, rel))
        if not (fp == root or fp.startswith(root + os.sep)) or not os.path.isfile(fp):
            return _http_response(404, b"not found\n", "text/plain; charset=utf-8")
        with open(fp, "rb") as f:
            body = f.read()
        ext = fp.rsplit(".", 1)[-1].lower() if "." in os.path.basename(fp) else ""
        return _http_response(200, body, _CTYPES.get(ext, "application/octet-stream"))
    return process_request


async def ws_handler(ws, *_):                    # *_ tolerates the (ws) or (ws, path) handler signatures
    b, inj = StreamProtocol.broadcaster, StreamProtocol.injector
    loop = asyncio.get_running_loop()
    sess = WSSession(ws, b, inj)
    b.sessions.add(sess)
    print("ws viewer connected; sessions:", len(b.sessions), flush=True)
    loop.run_in_executor(_X_EXEC, inj.reset)     # off-loop: a stalled XTEST must not freeze the server
    b.force_keyframe()
    try:
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                await loop.run_in_executor(_X_EXEC, dispatch_input, bytes(msg), inj, b)
            else:
                try: handle_control(json.loads(msg), sess._reply, b)
                except Exception: pass
    except Exception:
        pass
    finally:
        b.sessions.discard(sess); sess.task.cancel()
        print("ws viewer left; sessions:", len(b.sessions), flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cert"); ap.add_argument("--key")
    ap.add_argument("--source", choices=["auto", "wayland", "xvfb"], default="auto",
                    help="capture source: GPU compositor (wayland), software Xvfb (xvfb), or auto-detect")
    ap.add_argument("--width", type=int, default=DEF_W)
    ap.add_argument("--height", type=int, default=DEF_H)
    ap.add_argument("--fps", type=int, default=DEF_FPS)
    ap.add_argument("--bitrate", type=int, default=DEF_BITRATE, help="H.264 bitrate in kbit/s")
    ap.add_argument("--port", type=int, default=4433)
    ap.add_argument("--ws-port", type=int, default=4434)
    ap.add_argument("--ws-plain", action="store_true",   # plain ws:// when an ingress provides https/wss (NRP)
                    help="serve WebSocket without TLS (a reverse proxy / k8s ingress terminates TLS)")
    ap.add_argument("--serve-dir",                        # one port = page + WS (one ingress path '/')
                    help="also serve this dir's files over HTTP on the WS port, so a single ingress "
                         "path serves both the page and the WebSocket (backend answers HTTP probes)")
    args = ap.parse_args()

    # idle_timeout reaps a frozen/uncleanly-closed viewer (no QUIC ACKs) in ~25s instead of the 60s
    # default, so the broadcaster stops fanning datagrams at dead sessions and they don't pile up
    # (a healthy viewer ACKs the constant datagram flow, so it never trips this).
    cfg = QuicConfiguration(alpn_protocols=["h3"], is_client=False, max_datagram_frame_size=1500,
                            idle_timeout=25.0)
    cfg.load_cert_chain(args.cert, args.key)

    source = resolve_source(args.source)
    pipeline = build_pipeline(source, args.width, args.height, args.fps, args.bitrate)

    loop = asyncio.get_running_loop()
    b = Broadcaster(loop, pipeline)
    StreamProtocol.broadcaster = b
    StreamProtocol.injector = Injector(":2")   # XTEST into the X server (:2) hosting Slicer
    b.start()                                  # wayland: starts the compositor NOW (so the WL socket appears)

    await serve("0.0.0.0", args.port, configuration=cfg, create_protocol=StreamProtocol)
    print(f"WebTransport streamer on udp/{args.port}", flush=True)

    # WebSocket (TCP) transport in parallel -- same AUs, for HTTP-ingress-only hosts (NRP). wss with our
    # cert by default (direct, e.g. vast); --ws-plain when an ingress terminates TLS upstream.
    ws_ssl = None
    if not args.ws_plain:
        ws_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ws_ssl.load_cert_chain(args.cert, args.key)
    ws_kwargs = dict(max_size=None, compression=None, ping_interval=20, ping_timeout=20)
    if args.serve_dir:
        ws_kwargs["process_request"] = make_process_request(args.serve_dir)
    await websockets.serve(ws_handler, "0.0.0.0", args.ws_port, ssl=ws_ssl, **ws_kwargs)
    print(f"WebSocket streamer on tcp/{args.ws_port} ({'plain' if args.ws_plain else 'wss'})"
          + (f" + page from {args.serve_dir}" if args.serve_dir else ""), flush=True)
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
