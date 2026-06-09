# Run Desktopia locally (Docker / colima) — CPU/software path

Runs the **real Slicer on a real X workstation** (Xvfb + Mesa llvmpipe, no GPU) inside a Linux
container on your Mac via colima, streamed over plain WebSocket, and exposed to your browser at
`http://localhost:4434`. For developing/iterating the client, the compositor, and the 3D-offload
spike against a real Slicer without touching a cloud box.

## Quick start
```sh
./deploy/run-local.sh            # build deps image (first time) + run; prints the URL
# open http://localhost:4434/
./deploy/run-local.sh logs       # container logs        (startup, Slicer download)
./deploy/run-local.sh slog       # server.py logs        (pipeline, ws viewer connected)
./deploy/run-local.sh shell      # a shell in the container
# edit code on the host, then:
./deploy/run-local.sh restart    # re-stage code + restart the session
./deploy/run-local.sh stop       # remove container (keeps the Slicer volume)
```

## Why it iterates fast (the persistent-volume design)
- **Image = heavy apt deps only** (`deploy/Dockerfile.cpu` → `build-image.sh` with
  `DESKTOPIA_SKIP_SLICER=1 DESKTOPIA_SKIP_COMPOSITOR=1`). Built once, cached.
- **Slicer = a persistent volume at `/opt`** (`/tmp/desktopia-opt` in the VM). The entrypoint's
  `if ! ls /opt/Slicer-*` guard downloads it **once**; every later run reuses it. (A `colima stop/start`
  reboots the VM and clears `/tmp` → one re-download; point `DESKTOPIA_SLICER_DIR` at a `$HOME` path or
  a `docker volume` to survive that too.)
- **Code = the repo bind-mounted at `/opt/desktopia`.** Edit on the host, `restart` to apply — no rebuild.

So the slow parts (apt, the ~GB Slicer download) happen once; the edit→run loop is seconds.

## Notes
- No GPU in colima → `server.py` auto-detects the **xvfb/llvmpipe** software path; 3D is CPU-rendered
  (fine for wiring/UX; the point of the [3D-offload](../offload/README.md) work is to move that to the
  client's GPU). The offload spike (`offload/spike/slicer_scene_export.py`) can run in this very Slicer.
- Plain `ws://` (no TLS) is correct for localhost — the client's `defaultWsURL()` picks `ws:` over `http:`.
- `--shm-size=2g` is set because Slicer's bundled QtWebEngine (Chromium) needs more than Docker's default 64 MB `/dev/shm`.

## Why this matters: a scalable sandbox with a well-defined security boundary
Running Slicer **server-side in a disposable container**, reachable only as pixels + a narrow
input/control channel in the browser, turns "try this extension/script" into a **sandboxed** action
with a tight, explicit boundary:

- **The untrusted code never touches the user's machine.** It executes in the container, not on the
  viewer's laptop — no access to local files, devices, keychain, or network beyond what the container
  is granted. The browser only ever sends input events and receives a video stream (and, for the 3D
  offload, scene geometry) over one port.
- **The blast radius is the container.** Data and hardware access are whatever we explicitly mount/grant
  (by default: none of the user's data, no GPU, no host network). A misbehaving or malicious extension
  can be contained, rate-limited, and thrown away — `stop` and it's gone; the next session is clean.
- **It scales horizontally.** One disposable container per user/session (Cloud Run, a cheap CPU VM,
  k8s), each isolated, scale-to-zero when idle.

The payoff: you can **open extension development and testing to a much wider audience** — let people
run and try arbitrary Slicer extensions/scripts from a link — **without** the usual gatekeeping
(code signing, notarization, app-store review, manual install trust). The security model shifts from
"trust this binary on your computer" to "this runs in a bounded sandbox you can inspect and discard,"
which is a far easier bar for a reviewer or a newcomer to accept. It's the same reason web sandboxes
unlocked broad sharing — applied to a full native medical-imaging workstation.
