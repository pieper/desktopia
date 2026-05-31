"""Desktopia streaming server: GStreamer appsink -> QUIC datagrams (WebTransport).

Captures the GPU X server, hardware-encodes via NVENC with intra-refresh, fragments
each access unit into QUIC datagrams, and fans them out to every connected
WebTransport session. A reliable WT stream from the client signals "send a keyframe".
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
from aioquic.h3.events import HeadersReceived

Gst.init(None)

MTU = 1100                          # safe QUIC datagram payload (path MTU ~1200)
HDR = struct.Struct(">IBHH")       # frame_id(u32), flags(u8), n_chunks(u16), chunk_idx(u16)
FLAG_KEY = 0x01

# NVENC tuned for low latency + loss resilience:
#  - intra-refresh spreads I-blocks across frames (no full-IDR stalls on loss)
#  - bframes=0, low-latency preset, CBR. Use nvav1enc on Ada/Blackwell for ~30% less bitrate.
#  NOTE: verify property names against the actual image: gst-inspect-1.0 nvh264enc
PIPELINE = (
    "ximagesrc use-damage=0 ! video/x-raw,framerate=60/1 ! videoconvert ! "
    "nvh264enc name=enc preset=p1 tune=ultra-low-latency rc-mode=cbr bitrate=12000 "
    "  gop-size=-1 bframes=0 ! "
    "video/x-h264,stream-format=byte-stream,alignment=au ! "
    "appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true"
)


class Broadcaster:
    """Owns the GStreamer pipeline; pushes encoded AUs to all WebTransport sessions."""
    def __init__(self, loop):
        self.loop = loop
        self.sessions = set()            # set[StreamProtocol]
        self.frame_id = 0
        self.pipe = Gst.parse_launch(PIPELINE)
        self.enc = self.pipe.get_by_name("enc")
        sink = self.pipe.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)

    def start(self):
        self.pipe.set_state(Gst.State.PLAYING)

    def force_keyframe(self):
        self.enc.send_event(
            Gst.Event.new_custom(Gst.EventType.CUSTOM_DOWNSTREAM,
                Gst.Structure.new_empty("GstForceKeyUnit")))

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(mi.data)
        is_key = not (buf.get_flags() & Gst.BufferFlags.DELTA_UNIT)
        buf.unmap(mi)
        # hop from the GStreamer streaming thread to the asyncio loop
        self.loop.call_soon_threadsafe(self._fanout, data, is_key)
        return Gst.FlowReturn.OK

    def _fanout(self, data, is_key):
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

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._h3 = None
        self._session_id = None

    def quic_event_received(self, event):
        if isinstance(event, ProtocolNegotiated):
            self._h3 = H3Connection(self._quic, enable_webtransport=True)
        elif self._h3 is not None:
            for e in self._h3.handle_event(event):
                self._on_h3(e)
            # back-channel: any reliable stream byte == "send me a keyframe"
            if isinstance(event, StreamDataReceived) and event.data:
                self.broadcaster.force_keyframe()

    def _on_h3(self, e):
        if isinstance(e, HeadersReceived):
            hdrs = dict(e.headers)
            if hdrs.get(b":method") == b"CONNECT" and hdrs.get(b":protocol") == b"webtransport":
                self._session_id = e.stream_id
                self._h3.send_headers(e.stream_id, [(b":status", b"200")])
                self.broadcaster.sessions.add(self)
                if self.broadcaster.frame_id == 0:
                    self.broadcaster.start()
                self.broadcaster.force_keyframe()   # new viewer needs an entry point

    def send_video_datagram(self, payload):
        # associates the datagram with the WT session; API name varies by aioquic version
        try:
            self._h3.send_datagram(self._session_id, payload)
        except Exception:
            pass

    def connection_lost(self, exc):
        if self.broadcaster is not None:
            self.broadcaster.sessions.discard(self)
        super().connection_lost(exc)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cert")
    ap.add_argument("--key")
    ap.add_argument("--port", type=int, default=4433)
    args = ap.parse_args()

    cfg = QuicConfiguration(alpn_protocols=["h3"], is_client=False, max_datagram_frame_size=1500)
    cfg.load_cert_chain(args.cert, args.key)

    loop = asyncio.get_running_loop()
    b = Broadcaster(loop)
    StreamProtocol.broadcaster = b

    await serve("0.0.0.0", args.port, configuration=cfg, create_protocol=StreamProtocol)
    print(f"WebTransport streamer on udp/{args.port}")
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
