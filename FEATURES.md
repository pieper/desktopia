# Desktopia — remote-desktop feature map & roadmap

A survey of features from other remote-desktop systems and how they map onto our stack
(QUIC/WebTransport + WebCodecs client, headless-Wayland + GStreamer server — **we control both ends**),
plus the priorities we've decided to pursue.

**Who has it:** VNC = RealVNC/TigerVNC/**noVNC** (RFB) · RDP = MS Remote Desktop · TV = TeamViewer/AnyDesk/Splashtop ·
GS = game-streaming (Parsec/Moonlight/Sunshine) · Guac = Apache Guacamole (web).
**Desktopia:** ✅ have · ◑ partial · ❌ missing.

## Decided priorities (2026-06-05)
**Building now:**
- **Explicit clipboard** — never auto-sync (so passwords etc. don't leak). User explicitly pushes to / pulls from
  the remote clipboard, with a **history** (cap ~10–20 items / 10 MB).
- **Top status+menu bar** (thin, translucent) always visible: throughput, FPS, and a **latency** metric
  (keystroke→char / mouse-move→update; start with round-trip, refine toward input-to-photon).
- **Control-panel popup** — discoverable via a **button** in the top bar (unlike noVNC's hidden tab),
  translucent "liquid glass" (Apple-style `backdrop-filter` blur). Contains: clipboard push/pull + history,
  **virtual keyboard** (international if easy) and **Ctrl-Alt-Del**, and the shared-folders UI (below).
- **FUSE ↔ File System Access directory sharing** — server-side FUSE mount bridged to the browser's
  File System Access API over a control stream → **lazy upload/download** of selected local directories.
  Add/remove shared dirs from the popup; show status there.
- **Drag-and-drop upload/download** to arbitrary locations (not just the FUSE folder).
- Drag-drop *from the local desktop onto the remote app* — nice-to-have, do if easy.

**Deferred / not now:** multi-machine fleets, audio, pointer-lock. Most other rows look easy and can be added on demand.

**Strategic note:** the bigger interest is the **on-demand / elastic computing** side (spin-up, warm pools,
scene migration, scale-to-the-data) more than classic 30-year-old remote-desktop parity.

## Feature map

### Display & session core
| Feature | Why users like it | Who | Desktopia | On our stack |
|---|---|---|---|---|
| Live video stream | the product | all | ✅ H.264/QUIC/WebCodecs | done |
| Dynamic resolution / fit-to-window | no black bars, sharp at any size | RDP, GS, TV | ❌ fixed 1920×1080 | client posts viewport → compositor `wlr-randr` resize + re-config encoder |
| HiDPI / scaling | crisp on retina | RDP, TV | ◑ CSS scales | render at device-pixel res via resolution channel |
| Multi-monitor | more real estate | RDP, TV, GS | ❌ | multiple compositor outputs → multiple canvases |
| Quality/bitrate control | trade sharpness vs lag | all | ❌ fixed 8 Mbps | live `x264enc bitrate` + slider over control stream |
| Lossless / 4:4:4 "text mode" | crisp text / medical detail | RDP, Parsec | ❌ (4:2:0) | 4:4:4 profile / periodic lossless tiles (matters for Slicer slices) |
| Adaptive bitrate | auto-smooth on congestion | RDP, GS, TV | ❌ | drive bitrate/fps from QUIC RTT/loss |

### Input & navigation
| Feature | Why | Who | Desktopia | On our stack |
|---|---|---|---|---|
| Mouse + keyboard | core | all | ✅ XTEST | done |
| Visible cursor | know where you point | all | ◑ unclear | send cursor shape+pos on control stream, draw client-side |
| Send Ctrl-Alt-Del / special combos | escape captured keys | VNC, RDP | ◑ keysyms only | panel buttons + "pass all keys" toggle |
| Pointer lock (relative mouse) | 3D rotate / FPS | GS | ❌ (deferred) | Pointer Lock API → relative deltas |
| Gamepad / 3D mouse | specialized | GS | ❌ | Gamepad API → uinput |
| Touch / pinch-zoom / virtual trackpad | tablets/phones | TV, noVNC | ◑ pointer events | touch→mouse, gestures, OSK |
| Fullscreen | immersion | all | ❌ | Fullscreen API |

### Clipboard & files
| Feature | Why | Who | Desktopia | On our stack |
|---|---|---|---|---|
| **Text clipboard (explicit, bi-dir)** | copy/paste both ways, safely | all | ❌ → building | `navigator.clipboard` ↔ control stream ↔ `xclip` on :2; explicit push/pull only |
| Clipboard history | re-use past copies | TV (file box) | ❌ → building | client-side ring buffer, capped |
| Image clipboard | paste screenshots | RDP, TV | ❌ | clipboard API image ↔ PNG over reliable stream |
| **File upload (local→remote)** | get your data onto the box | RDP, TV, Guac | ❌ → building | drag/picker → reliable stream → write path |
| File download (remote→local) | pull results back | RDP, TV, Guac | ❌ → building | server → stream → `Blob` save |
| **FUSE ↔ FS-Access shared dirs (lazy)** | live shared folders, no full copy | (novel) | ❌ → building | server FUSE ⇄ browser File System Access over control stream |
| Drag file into remote app | frictionless | TV, RDP | ◑ box-local (pcmanfm→Slicer) | upload + auto-drop |

### Sessions, collaboration & monitoring
| Feature | Why | Who | Desktopia | On our stack |
|---|---|---|---|---|
| Auto-reconnect / persistence | survive blips | all | ✅ | done |
| Multi-user view | pair/support/teach | VNC, TV, Guac | ◑ fan-out, no arbitration | input lock + per-user cursors |
| View-only / share link | safe demos | VNC, TV, Guac | ❌ | token disabling input |
| Session chat | talk while sharing | TV, Guac | ❌ | control-stream messages |
| Session recording | audit/training | RDP, TV, Guac | ❌ | tee H.264 AUs → mp4 (cheap, already encoded) |
| Multi-machine dashboard / fleet | watch many boxes | TV, Splashtop | ❌ (deferred) | `launch.py` data → thumbnail grid |
| Status HUD (FPS/RTT/bitrate/loss) | diagnose lag | GS overlay | ◑ had debug counters | building (top bar) |
| Remote reboot / restart-app | recover w/o SSH | RDP, TV | ❌ | control-stream commands |

### Access, security & niceties
| Feature | Why | Who | Desktopia | On our stack |
|---|---|---|---|---|
| Pinned cert / encrypted transport | trust | all | ✅ QUIC/TLS cert pin | done |
| Auth / access token | keep strangers out | all | ❌ | token in WT CONNECT / status gate |
| Approve-to-join | support workflow | TV | ❌ | server prompt before granting input |
| Idle timeout / auto-suspend | save GPU $ | TV, cloud | ◑ vast idle | hook to stop/warm-pool |
| Audio out / mic | real desktop | RDP, TV, GS | ❌ (deferred) | PipeWire→Opus→WebAudio; mic→virtual device |
| Key-layout / localization | non-US keyboards | RDP, VNC | ❌ | xkb layout via control stream |

## Build phases
1. **Control channel + UI shell** — bidirectional control stream; top status bar (FPS, throughput, RTT) +
   discoverable button; glass control-panel popup; explicit clipboard (push/pull + history); virtual keyboard + Ctrl-Alt-Del.
2. **File transfer** — drag-drop / picker upload to a chosen path; download; surfaced in the panel.
3. **FUSE ↔ FS-Access shared directories** — the lazy live-share; add/remove dirs in the panel.
