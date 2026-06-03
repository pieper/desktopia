# Desktopia

Desktopia turns a cloud GPU machine into a full Linux desktop that
you operate from an ordinary web browser tab. A desktop application — by default
[3D Slicer](https://www.slicer.org/) — renders with real hardware GPU acceleration on the
server; its screen is encoded as [H.264](https://en.wikipedia.org/wiki/Advanced_Video_Coding)
video and streamed to a single web page, and your keyboard and mouse travel back the other way
over the same connection. The result is a low-latency "desktop in a browser tab," in the spirit
of [noVNC](https://novnc.com/) but built on modern web-streaming technology instead of the
decades-old [VNC (Virtual Network Computing)](https://en.wikipedia.org/wiki/Virtual_Network_Computing)
protocol.

It is meant to run on a rented cloud GPU — it was developed against
[vast.ai](https://vast.ai/), a marketplace for renting GPU machines by the hour — but nothing
in the design is tied to a particular host.

## How it works

There are two data paths: video going out to the browser, and input coming back to the desktop.

### Video: server → browser

```
3D Slicer and other apps
  │  draw with hardware OpenGL
  ▼
Xwayland  ──hosts the X11 apps──►  headless Wayland compositor   (on the GPU, no monitor)
                                          │
                                          ▼
            GStreamer:  capture the compositor's screen  →  H.264 encode (NVENC, or software)
                                          │
                                          ▼
                          split each video frame into QUIC packets
                                          │
                   WebTransport over QUIC ═══ public internet ═══► browser
                                          │
                                          ▼
                 reassemble each frame  →  WebCodecs decoder  →  <canvas> on the page
```

- The desktop runs on a **headless [Wayland](https://en.wikipedia.org/wiki/Wayland_(protocol))
  compositor** — a display server that renders entirely on the GPU with no physical monitor
  attached (built on [Smithay](https://github.com/Smithay/smithay) via
  [gst-wayland-display](https://github.com/games-on-whales/gst-wayland-display)). It reaches the
  GPU through a [DRM render node](https://en.wikipedia.org/wiki/Direct_Rendering_Manager#Render_nodes),
  which needs no special display privileges and **no
  [VirtualGL](https://www.virtualgl.org/)** (a fragile shim that older remote-3D setups rely on).
- Traditional X11 applications such as 3D Slicer and Chrome run under
  **[Xwayland](https://wayland.freedesktop.org/xserver.html)** as clients of that compositor,
  which gives them hardware **[OpenGL](https://en.wikipedia.org/wiki/GLX)** acceleration.
- **[GStreamer](https://gstreamer.freedesktop.org/)** (a media-pipeline framework) captures the
  compositor's output and encodes it to **H.264** using NVIDIA's hardware encoder,
  [**NVENC**](https://en.wikipedia.org/wiki/Nvidia_NVENC), when the host allows it, or the
  **x264** software encoder otherwise. ([AV1](https://en.wikipedia.org/wiki/AV1), a newer codec,
  is available on recent GPUs.)
- Each encoded frame is split into **[QUIC](https://en.wikipedia.org/wiki/QUIC)** packets and
  pushed to the browser with the
  **[WebTransport](https://developer.mozilla.org/en-US/docs/Web/API/WebTransport)** API (which
  runs over QUIC and [HTTP/3](https://en.wikipedia.org/wiki/HTTP/3)). Unlike an ordinary TCP
  connection, one lost packet does not stall everything queued behind it
  ([head-of-line blocking](https://en.wikipedia.org/wiki/Head-of-line_blocking)).
- The browser reassembles each frame and decodes it with the
  **[WebCodecs](https://developer.mozilla.org/en-US/docs/Web/API/WebCodecs_API)** API straight
  into an HTML `<canvas>`, so the page itself controls how much it buffers and how much latency
  it accepts.
- **Surviving packet loss.** The encoder uses *intra-refresh*: instead of periodically sending
  one large, expensive keyframe, it refreshes a slice of the picture every frame. A dropped
  packet then causes a small, brief smear that heals within a few frames rather than a full
  freeze. The page notices when a frame is missing and asks for a fresh keyframe only when it
  truly needs one.

### Input: browser → server

Mouse and keyboard events from the canvas are sent back over a reliable WebTransport stream and
replayed into the desktop as synthetic events (through the X11 *XTEST* extension), so the remote
applications receive them as ordinary input.

### Connection security

The server presents a short-lived, self-signed
[TLS (Transport Layer Security)](https://en.wikipedia.org/wiki/Transport_Layer_Security)
certificate, and the browser trusts it by matching its hash (the WebTransport
`serverCertificateHashes` option). This avoids needing a domain name or a certificate authority
for a machine that may only exist for an hour.

## Compared to noVNC and Selkies

Cloud "desktop in a browser" offerings usually ship one of two stacks:
[**noVNC**](https://novnc.com/) (an HTML5 client for the old VNC protocol) or
[**Selkies**](https://github.com/selkies-project/selkies) (a
[**WebRTC**](https://en.wikipedia.org/wiki/WebRTC)-based desktop streamer). Desktopia is a third
point in the design space, optimized for **low interactive latency on GPU/3D workloads** and for
being **small enough to modify**.

| | noVNC (+ VNC server) | Selkies (WebRTC) | **Desktopia** |
|---|---|---|---|
| **Transport** | Web page ↔ server over **TCP**; one lost packet stalls everything behind it | **WebRTC** over UDP; needs connection negotiation and usually a relay server ([TURN](https://en.wikipedia.org/wiki/Traversal_Using_Relays_around_NAT)) to cross firewalls | **WebTransport over QUIC** (UDP); one public port, no negotiation or relay |
| **Video** | Framebuffer tile diffs (the VNC protocol), **CPU only** — no hardware video codec | Hardware **NVENC** (H.264/VP8/VP9) | Hardware **H.264 via NVENC** (software x264 fallback) |
| **Browser decode** | JavaScript paints the framebuffer | Browser's built-in WebRTC player, with a smoothing buffer **you can't tune** | **WebCodecs decoder driven by hand** — the page controls buffering and latency |
| **Packet loss** | TCP re-sends the data → visible stall | Retransmit requests + smoothing buffer → added latency | **Intra-refresh + custom handler**: skip the damaged frame, request a keyframe only when needed |
| **Infrastructure** | A VNC server (plus **VirtualGL** for 3D) | A signaling service and often a **TURN relay** | One **UDP port** and a self-signed certificate |
| **3D / GPU desktop** | Needs VirtualGL (fragile) | GPU desktop with NVENC | **Headless Wayland + Xwayland** → hardware OpenGL, **no VirtualGL** |
| **Made of** | Fixed VNC protocol | Fixed stack | A small Python server + one HTML page you can modify |

The core idea is **latency and control**: a direct QUIC connection plus a hand-driven WebCodecs
decoder removes WebRTC's negotiation step and its opaque smoothing buffer, and drops the
relay/signaling servers entirely — which matters for a short-lived rented machine reached at a
bare public address. On this path, an interactive CT volume render has run at
**80–90 frames per second, full-frame, end-to-end in the browser**.

**Honest trade-offs.** Desktopia is experimental and minimal where the others are mature: it
**falls back to CPU encoding** on hosts that block NVENC, has **no audio or clipboard yet**, and
requires a **Chromium-based browser** (the WebTransport and WebCodecs APIs are not in Safari and
only partly in Firefox). noVNC wins on universal browser support; Selkies wins on polish (audio,
adaptive bitrate, clipboard, years of testing). Reach for Desktopia when you want the lowest
interactive latency for a GPU/3D workload and a pipeline small enough to change.

## Components

| File | Role |
|---|---|
| `server.py` | Runs the headless Wayland compositor and GStreamer H.264 encoder, fans each encoded frame out to every connected browser as QUIC packets, and injects incoming keyboard/mouse input into the desktop |
| `session-wayland.sh` | Brings up the compositor and streaming server, then Xwayland, the [Openbox](http://openbox.org/) window manager, and the apps (terminal, Chrome, 3D Slicer) |
| `entrypoint-wayland.sh` | Container start-up: installs dependencies, creates the unprivileged desktop user, mints the certificate, then launches the session |
| `provision-wayland.sh` | Installs the system packages the desktop and streaming pipeline depend on |
| `client/index.html` | The web page: connects over WebTransport, decodes with WebCodecs, draws to a canvas, and forwards keyboard and mouse input |

## Running it

You need the [vast.ai command-line tool](https://vast.ai/) and a Chromium-based browser
(Chrome, Edge, Brave, …).

```bash
pip install vastai && vastai set api-key <YOUR_KEY>

make best          # pick a suitable single-GPU offer (an RTX 4090 is ideal)
make up-best       # rent it
make wl-setup      # build the Wayland compositor on the bare instance (one time)
make stream        # start the compositor, encoder, QUIC server, desktop, and 3D Slicer
make port          # print the public address (IP:PORT) the stream is reachable at
```

`make stream` also prints a `CERT_SHA256_BASE64=` line (the certificate hash). Paste that hash
and the `IP:PORT` from `make port` into `client/index.html`, then open that page in a
Chromium-based browser (served from `localhost` or over HTTPS) to connect to the desktop.

Alternatively, the prebuilt container image (built by CI, with the compositor and 3D Slicer
already baked in) boots straight into the stream — rent an offer with `make up-ghcr OFFER=<id>`
and skip `make wl-setup`.

The GPU machine must be launched with these options (the tooling applies them automatically):

```
-p 4433:4433/udp                      # QUIC is UDP — the /udp suffix is required
-e NVIDIA_DRIVER_CAPABILITIES=all     # must include graphics, display, video, compute
-e NVIDIA_VISIBLE_DEVICES=all
```

When you are finished, `make down` destroys the machine and stops billing.

## Requirements and limitations

- **GPU:** a single NVIDIA GPU; an Ada-generation card (e.g. RTX 4090) gives the best hardware
  encoding and also supports AV1.
- **Browser:** Chromium-based only — the WebTransport and WebCodecs APIs are not available in
  Safari and only partially in Firefox.
- **Encoding:** some hosts block NVENC, in which case the pipeline falls back to slower CPU
  (x264) encoding.
- **Not yet implemented:** audio and clipboard sharing.

## See also

- [BACKENDS.md](BACKENDS.md) — running the image on other GPU backends (AWS, GCP, Azure, GPU‑rental clouds, local) and how to test each.
- [SECURITY.md](SECURITY.md) — certificate/key handling and the access model.
- [SETUP.md](SETUP.md) — reproducing the container image from scratch.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
