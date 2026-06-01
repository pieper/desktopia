"""Desktopia streaming server: GStreamer (Wayland compositor) -> QUIC datagrams (WebTransport).

Runs gst-wayland-display's `waylanddisplaysrc` (a headless GPU Smithay compositor), encodes
each frame to H.264 (NVENC if the host allows it, else software x264), fragments each access
unit into QUIC datagrams, and fans them out to every connected WebTransport session. A reliable
WT stream byte from the client means "send me a keyframe".

The compositor pipeline starts immediately (so the Wayland socket exists and Xwayland/Slicer can
render into it) regardless of viewers; datagrams only go out once a browser connects.
"""
import argparse, asyncio, struct
import gi
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
W, H, FPS = 1920, 1080, 30

# Client->server input protocol (reliable stream), fixed length per message type:
#  0 keyframe-req[1]  1 move[1+x:u16+y:u16]  2 mousedown[1+btn]  3 mouseup[1+btn]
#  4 wheel[1+dir(0=up,1=down)]  5 keydown[1+keysym:u16]  6 keyup[1+keysym:u16]
MSG_LEN = {0: 1, 1: 5, 2: 2, 3: 2, 4: 2, 5: 3, 6: 3}


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


def encoder_bin():
    """Prefer hardware NVENC; fall back to software x264. Constrain to H.264 High so the
    browser's WebCodecs config (avc1.640028) matches. byte-stream/AU for Annex-B framing."""
    caps = "video/x-h264,profile=high,stream-format=byte-stream,alignment=au"
    if Gst.ElementFactory.find("nvh264enc"):
        return f"nvh264enc name=enc bitrate=8000 ! {caps}"
    return ("x264enc name=enc tune=zerolatency speed-preset=veryfast bitrate=8000 "
            f"key-int-max={FPS} ! {caps}")


PIPELINE = (
    f"waylanddisplaysrc ! video/x-raw,width={W},height={H},format=RGBx,framerate={FPS}/1 "
    f"! videoconvert ! {encoder_bin()} "
    "! appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true"
)


class Broadcaster:
    """Owns the GStreamer pipeline; pushes encoded AUs to all WebTransport sessions."""
    def __init__(self, loop):
        self.loop = loop
        self.sessions = set()            # set[StreamProtocol]
        self.frame_id = 0
        print("pipeline:", PIPELINE, flush=True)
        self.pipe = Gst.parse_launch(PIPELINE)
        self.enc = self.pipe.get_by_name("enc")
        self.pipe.get_by_name("sink").connect("new-sample", self._on_sample)
        bus = self.pipe.get_bus(); bus.add_signal_watch()
        bus.connect("message::error", lambda _b, m: print("GST ERROR:", m.parse_error(), flush=True))

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
        body = MTU - HDR.size
        chunks = [data[i:i + body] for i in range(0, len(data), body)] or [b""]
        n = len(chunks)
        flags = FLAG_KEY if is_key else 0
        for sp in list(self.sessions):
            for idx, c in enumerate(chunks):
                sp.send_video_datagram(HDR.pack(fid, flags, n, idx) + c)


class StreamProtocol(QuicConnectionProtocol):
    broadcaster: Broadcaster = None
    injector: Injector = None

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._h3 = None
        self._session_id = None
        self._inbuf = b""

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
        t = m[0]; inj = self.injector
        if t == 0:   self.broadcaster.force_keyframe()
        elif t == 1: inj.move((m[1] << 8) | m[2], (m[3] << 8) | m[4])
        elif t == 2: inj.button(m[1], True)
        elif t == 3: inj.button(m[1], False)
        elif t == 4: inj.wheel(m[1] == 1)
        elif t == 5: inj.key((m[1] << 8) | m[2], True)
        elif t == 6: inj.key((m[1] << 8) | m[2], False)

    def _on_h3(self, e):
        if isinstance(e, HeadersReceived):
            hdrs = dict(e.headers)
            if hdrs.get(b":method") == b"CONNECT" and hdrs.get(b":protocol") == b"webtransport":
                self._session_id = e.stream_id
                self._h3.send_headers(e.stream_id, [(b":status", b"200")])
                self.broadcaster.sessions.add(self)
                self.broadcaster.force_keyframe()         # new viewer needs an entry point
                self.transmit()
                print("viewer connected; sessions:", len(self.broadcaster.sessions), flush=True)
        elif isinstance(e, WebTransportStreamDataReceived):   # input + keyframe channel
            self._inbuf += e.data
            self._parse_input()

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


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cert"); ap.add_argument("--key")
    ap.add_argument("--port", type=int, default=4433)
    args = ap.parse_args()

    cfg = QuicConfiguration(alpn_protocols=["h3"], is_client=False, max_datagram_frame_size=1500)
    cfg.load_cert_chain(args.cert, args.key)

    loop = asyncio.get_running_loop()
    b = Broadcaster(loop)
    StreamProtocol.broadcaster = b
    StreamProtocol.injector = Injector(":2")   # XTEST into the Xwayland hosting Slicer
    b.start()                                  # start the compositor NOW (so the WL socket appears)

    await serve("0.0.0.0", args.port, configuration=cfg, create_protocol=StreamProtocol)
    print(f"WebTransport streamer on udp/{args.port}", flush=True)
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
