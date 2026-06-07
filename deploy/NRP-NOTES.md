# NRP/Nautilus experiment — PAUSED 2026-06-06

We tried hosting Desktopia on **NRP (National Research Platform) at MGHPCC** (namespace `slicer-dev`,
NVIDIA A10, HTTPS-only HAProxy ingress, no public UDP). It got **most of the way** but the GPU/DRM
exposure across MGHPCC nodes was too inconsistent to be worth it right now. **The vast.ai version works
well and is dirt cheap**, so we stopped here. These notes capture exactly where we were so it's a short
hop to resume.

## What we PROVED works end-to-end through the NRP ingress
- **Page serves via ingress**: `GET https://desktopia-slicer-dev.nrp-nautilus.io/` → **HTTP 200** (~0.34s).
- **WebSocket upgrade completes via ingress**: `GET /ws` (HTTP/1.1, `Upgrade: websocket`) → **HTTP/1.1 101
  Switching Protocols** with a valid `sec-websocket-accept`. (Browsers do WS over HTTP/1.1; a curl test
  over HTTP/2 returns 404 because h2 strips `Connection/Upgrade` — not a bug.)
- **Compositor + NVENC + Slicer** render on the A10 (`nvh264enc`, non-empty DMA formats, "Switch to
  module: Welcome") — *on nodes where the render node was correct* (see blocker #1).

## The three fixes that got us there (all in the code/manifest now, kept)
1. **One port serves BOTH the page (HTTP) and the WebSocket** — `server.py --serve-dir client` +
   `make_process_request()`. NRP's HAProxy **health-probes the backend with a plain HTTP GET**; a
   WS-only server never answers, so HAProxy marks the backend down and **parks every `/ws` upgrade**
   (symptom: `curl /ws` hangs, then 504). Answering plain GETs *and* WS upgrades on one port → a single
   ingress path `/` → one healthy backend. (Manifest: single containerPort/Service port `4434`, one
   ingress path `/ → 4434`, env `DESKTOPIA_SERVE_PAGE=1`.)
2. **websockets 16.0 `process_request` Response must be built like `reject()`** — i.e. include
   **`Connection: close`** (plus `Date`, `Content-Length`, `Content-Type`). A returned `Response`
   *rejects* the WS handshake and the server then **aborts the transport**; without `Connection: close`
   the keep-alive response is discarded and the client gets **nothing**. (Bug we hit first: hand-built
   `Response(200,"OK",headers,body)` with only Content-Type/Length → curl got empty.) See
   `_http_response()`.
3. **All XTEST/Xlib must run OFF the asyncio event loop** — a single-thread executor `_X_EXEC`. A
   synchronous `inj.reset()` (Xlib round-trips to a busy X server) **on the loop froze the entire server
   the instant a viewer connected** (page + video + accept all wedged; no "ws viewer connected" logged).
   Now `inj.reset()` and `dispatch_input()` go through `_X_EXEC`. **This fix also benefits vast.**

## Why we stopped — remaining blockers (node-dependent GPU/DRM flakiness)
1. **Render-node selection is fragile.** `render_node()` picks the first `/dev/dri/renderD*` (usually
   `renderD128`). On some MGHPCC nodes `renderD128` is **not** the A10 → compositor reports
   `Supported DMA formats: []` → never creates the Wayland socket → `session-wayland` times out → its
   `cleanup` trap kills `server.py` → whole stack dies. Worked on nodes where the GPU was
   `renderD129`/`renderD133`. **TODO to resume:** pick the NVIDIA-backed node robustly, e.g. the
   `/dev/dri/renderD*` whose `/sys/class/drm/<n>/device/driver` resolves to `nvidia`, and/or retry.
2. **Worse: inconsistent DRM exposure.** On `gpu-08.nrp.mghpcc.org`, `nvidia-smi -L` saw the A10 but
   **`/dev/dri` was missing entirely** — no DRM render nodes at all, so headless EGL/Wayland compositing
   is impossible there. GPU/DRM exposure varies per node; you can't rely on it without node pinning or an
   admin fix.
3. **Operational friction:** bare Pod (not Deployment) with `restartPolicy` + `emptyDir` at
   `/opt/desktopia`; the debug-stable command (`... ; echo STACK-EXITED ; sleep 86400`) keeps the
   container up after a crash so you can exec in. 6h bare-pod cap. Pod recreate re-runs the runtime
   install (~45–60s deps+compositor; Slicer downloads in background). After recreate, HAProxy needs a few
   seconds to register the new pod endpoint (transient **503**).

## State of the files (ready to resume)
- `deploy/nrp-desktopia.yaml` — single-port (4434) generic `ubuntu:24.04` pod, one ingress path `/`,
  `DESKTOPIA_SERVE_PAGE=1`, `DESKTOPIA_WS_PLAIN=1`, `NVIDIA_DRIVER_CAPABILITIES=all`, A10 nodeSelector.
- `server.py` — combined page+WS server (`--serve-dir`), v16 `process_request`/`_http_response`,
  `_X_EXEC` off-loop injector.
- `session-wayland.sh` / `entrypoint-wayland.sh` — `DESKTOPIA_SERVE_PAGE` path; no separate http.server.
- `deploy/Dockerfile.nrp` + `deploy/build-image.sh` — **deferred** baked image (no runtime downloads,
  runs as non-root). Build via CI → GHCR, then swap the manifest `image:` and drop the wait-for-code
  `command`. This also sidesteps most of the operational friction.

## To resume later
1. Fix `render_node()` to select the nvidia-backed render node (+ retry / fail loud if `/dev/dri` empty).
2. Optionally pin to a node known to expose `/dev/dri` (e.g. one of gpu-01..18 that worked), or ask NRP
   admins (Matrix) why DRM nodes are missing on some MGHPCC GPU nodes.
3. Redeploy: `kubectl apply -f deploy/nrp-desktopia.yaml` → wait Running → push code (tar|kubectl exec)
   → open https://desktopia-slicer-dev.nrp-nautilus.io/. The ingress/WS path is already proven.

## Teardown (done 2026-06-06)
`kubectl delete -f deploy/nrp-desktopia.yaml` — pod + service + ingress removed; namespace clean.
