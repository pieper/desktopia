# Desktopia

A vast.ai GPU container that renders a desktop (3D Slicer / `glxgears`), hardware-encodes
it with NVENC, and streams it to a custom web page over **QUIC / WebTransport** — a modern,
WebRTC-free take on noVNC.

> **New here?** [SETUP.md](SETUP.md) replicates everything from scratch (repo, CI→GHCR,
> vast.ai key, dev loop). [SECURITY.md](SECURITY.md) covers the key scope / spend cap / 2FA model.

## Data path

```
GPU (Xorg, hardware GLX) → app renders → ximagesrc capture
  → nvh264enc (NVENC, intra-refresh, bframes=0, CBR, low-latency)
  → appsink → Python → fragment into QUIC datagrams
  → WebTransport (HTTP/3) → browser
  → reassemble → WebCodecs VideoDecoder → canvas
```

## Why this shape

- **WebTransport/QUIC, not WebRTC.** Datagrams (unreliable, UDP-like) carry video; a
  reliable WT stream is the keyframe-request back-channel. vast.ai's port mapping exposes a
  direct public `IP:PORT`, so there's **no TURN/coturn/signaling** to run.
- **Loss resilience is the "custom decoder."** NVENC **intra-refresh** (I-blocks spread
  across frames, so a dropped datagram doesn't black out the stream) plus a client-side
  WebCodecs `VideoDecoder` driven manually: reassemble per frame, detect loss when a *later*
  frame completes first, abandon the broken frame, and request a keyframe only on a delta gap.
- **GStreamer for encode, not aiortc.** aiortc is pure-Python and can't cleanly inject
  pre-encoded NALs or hardware-handle RTP/PLI.
- **Self-signed ECDSA P-256 cert, ≤14-day validity**, pinned client-side via
  `serverCertificateHashes` — skips needing a domain/CA on ephemeral instances.
- **GLVND, not Mesa.** Install `libglvnd0`/`libgl1`/`libglx0`/`libegl1` and let the NVIDIA
  Container Toolkit inject the driver libs. **Never** `apt install nvidia-driver-*`.

## Files

| File | Role |
|---|---|
| `Dockerfile` | Ubuntu 24.04 + GLVND + Xorg + GStreamer/NVENC + aioquic |
| `entrypoint.sh` | Derives Xorg BusID from `nvidia-smi`, writes `xorg.conf`, starts X, mints the cert, launches the server |
| `server.py` | GStreamer appsink → QUIC-datagram fan-out to WebTransport sessions |
| `client/index.html` | WebTransport + WebCodecs client with the loss handler |

## Dev / test loop (no local Docker)

The Mac is arm64; vast.ai hosts are amd64 — so we **never build locally**. Two phases:

**Phase 1 — interactive debugging on vast.ai (no Docker).** Rent a stock CUDA instance, sync
this repo up, and run the scripts by hand. Seconds per iteration; this is where the X11 /
GLVND / GStreamer / aioquic sharp edges get resolved.

```bash
pip install --user vastai && vastai set api-key <YOUR_KEY>
make search                 # cheapest single RTX 4090 offers
make up OFFER=<OFFER_ID>     # launch CUDA base image with the UDP port + graphics caps
make ls                     # instance id + status
make ssh                    # shell in; run the apt block, then `bash entrypoint.sh`
make sync                   # rsync the working tree to /root/desktopia
make port                   # public IP:PORT mapped to 4433/udp -> paste into client/index.html
make down                   # destroy when done (stops billing)
```

**Phase 2 — bake into an image via CI.** Once Phase-1 commands work, they're already the
Dockerfile. Pushing to `main` triggers `.github/workflows/build.yml`, which builds on free
amd64 GitHub runners and pushes `ghcr.io/pieper/desktopia:latest` (no local Docker). Then:

```bash
make up-ghcr OFFER=<OFFER_ID>   # launch the built image instead of the CUDA base
```

vast.ai launch options (already applied by `vast.sh`):

```
-e NVIDIA_DRIVER_CAPABILITIES=all     # MUST include graphics,display,video,compute
-e NVIDIA_VISIBLE_DEVICES=all
-p 4433:4433/udp                      # QUIC is UDP — the /udp is mandatory
```

Rent a **single** Ada GPU (RTX 4090) — best NVENC, and `nvav1enc` is available if you switch
to AV1. From the boot logs, copy the `CERT_SHA256_BASE64=` line and `make port` into
`client/index.html`, then open it in Chromium (WebTransport + WebCodecs are Chromium-only
today). Serve the page from `localhost` or HTTPS.

## Sharp edges — verify before trusting it

1. **Xorg-in-container is the #1 go/no-go.** It needs a VT and device access; some vast.ai
   hosts won't allow it without privilege. Fallbacks: (a) headless **Wayland** (`cage`/wlroots)
   + `pipewiresrc`/dmabuf capture — the future-proof, zero-copy-on-UMA path; (b) **EGL
   offscreen** if you only need render→encode and not an interactive desktop (then the app
   must render into a surface you capture via GL/CUDA interop — `ximagesrc` has nothing to grab).
2. **`nvh264enc` property names drift** across nvcodec versions (`nvh264enc` vs
   `nvcudah264enc`/`nvautogpuh264enc`; `tune`/`rc-mode`/`gop-size` naming). Run
   `gst-inspect-1.0 nvh264enc` on the actual image and adjust `PIPELINE` in `server.py`.
3. **aioquic's WebTransport datagram/session API is version-sensitive.** `send_datagram(session_id, …)`
   and the H3 WT events may need tweaking against the installed version's `webtransport`
   example. Test a trivial datagram echo first; the fragmentation/fan-out logic is stable.
4. **The capture is a GPU→CPU→GPU readback** (`ximagesrc`) — fine at 1080p/1440p60, becomes
   the bottleneck at 4K60. The dmabuf/PipeWire fallback removes it on coherent hardware.
5. **`max_datagram_frame_size`** must be negotiated (set in `server.py`) or datagrams
   silently won't send; keep chunk size ≤ ~1100 B regardless.

## Roadmap

- [ ] PoC 1 — prove Xorg + hardware GLX in a vast.ai container (`glxinfo` shows NVIDIA, not llvmpipe)
- [ ] PoC 2 — datagram echo over WebTransport (validate aioquic API surface)
- [ ] PoC 3 — full pipeline with `glxgears`, then swap in 3D Slicer
- [ ] dmabuf/PipeWire capture variant (kills the readback; vendor-neutral via VA-API)
- [ ] MCP / slicer-skill agent tool surface wired into the session
