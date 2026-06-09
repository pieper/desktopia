# Desktopia 3D-offload (client-side GPU rendering)

Goal: run the **real Slicer on a real X workstation** on a cheap **GPU-less** cloud box, stream the
Qt desktop as video as today, but **render the 3D view(s) on the *client's* GPU** by shipping VTK
*scene state* (geometry/scene delivery, a.k.a. "local rendering") instead of pixels. Long-term the
3D viewport is hole-punched into the streamed desktop and composited under it; menus/popups stream
and sit on top. Server needs no GPU → pennies/hour; 3D interaction is local → instant.

This dir starts with **Phase 0: a spike** to de-risk the core question — *can we faithfully and
performantly re-render Slicer's 3D view in a browser from serialized scene state?* — before building
the compositor and sync loop.

Background + the full plan/level-comparison live in the chat thread; the one-line summary:
- **Level chosen:** VTK-object (scene) level, not OpenGL-command level (only the scene level gives
  local interaction + a GPU-less server). Client backend: **vtk.js / WebGL2 now**, vtk-wasm/WebGPU later.
- **Prior art:** trame `VtkLocalView`, VTK `render_window_serializer` / `vtkObjectManager`, and a
  working Slicer PoC (forum: "SlicerCAT as web service", @keri) that hit a "gets slow over time" issue.

## Why the split wins (cloud scenario): compute on the server, rendering on the client

Moving *rendering* to the client GPU frees the server to be what cloud is actually good at — a
**big-memory, many-core compute** box. The heavy work (segmentation, registration, filtering, large
volumes, AI inference) runs server-side with as much RAM/CPU — or a *compute* GPU — as you care to pay
for, while **display** rides the *user's* GPU. You stop renting an expensive *graphics* GPU in the cloud
just to draw pixels: you rent compute, and the browser draws.

And because the same `vtkRenderWindow` can be delivered either way (trame's `VtkRemoteLocalView` model),
the system can **gracefully trade off local vs remote rendering per view, by scene complexity**:
- **Light scenes** (models, ordinary volumes) → render **locally** on the client GPU: instant
  interaction, no server GPU, and the 3D viewport leaves the video stream entirely.
- **Heavy scenes** (huge meshes / massive volumes that would blow the client's GPU/RAM or the upload
  bandwidth) → fall back to **remote** server-side rendering + video for that view, where the big box
  does the rasterization.
- The choice is **per-view and can be automatic** (size/feature thresholds), so e.g. a giant 3D volume
  streams pixels while the slice views and models render locally — always the lowest latency the client
  can handle, with the server stepping in as the heavy-lifter only when it must.

Net: cloud scales the **computation**, the client scales the **rendering**, and the boundary between
them flexes with the scene instead of being fixed — the opposite of today's "rent a GPU to draw."

## Phase 0 spike — run it

Two pieces, no build step:

1. **Server** — `spike/slicer_scene_export.py`. Paste into Slicer's Python console, then:
   ```python
   startSceneExport()        # serves /scene and /array on :2027 (CORS on)
   ```
   It serializes 3D view 0's `vtkRenderWindow` with VTK's `render_window_serializer` on Slicer's main
   Qt thread (thread-safe). If the import fails, the script prints the one-file fix (drop VTK's
   `render_window_serializer.py` beside it).

2. **Client** — `spike/client/index.html`. Open in a browser on a machine **with a GPU**, set the
   server URL (e.g. `http://localhost:2027`, or the box's address through the Desktopia tunnel/ingress),
   pick a view index, hit **Load scene**. It re-renders the scene with vtk.js and lets you rotate/zoom
   **locally**. "auto-reload" re-syncs every second to stress the path.

Try it with: a loaded model (e.g. a segmentation closed surface), then a volume with volume rendering on.

## What to judge (the go/no-go)

- **Fidelity:** does the model/surface appear with correct color/opacity/LUT? Camera match? What's
  missing (orientation marker, scalar bar, 2D overlays, glyphs)?
- **Volume rendering:** the known weak spot in vtk.js/WebGL2 — does the volume render at all, and how
  close is it to Slicer's (shading, gradient opacity, transfer function)? This most influences the
  "ship WebGL2 now vs skate to WebGPU" call.
- **Interaction:** is local rotate/zoom smooth and instant (the whole point)?
- **Perf / the keri risk:** with auto-reload on, does **sync+render time creep up** over minutes
  (leak)? Watch the status line and the browser's memory.
- **Payload size:** the status line shows scene KB + array fetch time — gauges the upfront-transfer cost.

## If it looks good → next phases (see the thread for the full plan)

- **P1** scene *deltas* on MRML change (not full re-serialize) + camera pushed back to `vtkMRMLViewNode`.
- **P2** the hole-punch compositor (key-color viewport, viewport-rect tracking, popup z-order) into the
  Desktopia streamed desktop; video fallback while a window is dragged / for unsupported views.
- **P3** interaction policy (camera local; markups/segment-edit round-trip) + back-pressure.
- **P5** vtk-wasm + WebGPU for volume-rendering parity (same VTK both ends).

## Caveats already known

- **PHI:** scene delivery ships the actual meshes/volume data to the browser (unlike pixel streaming).
  Fine for education/non-PHI; for patient data, ship derived/downsampled geometry or fall back to video.
- This is **spike code** — first draft to run against a live Slicer and iterate; the vtk.js wiring and
  the `vtkmodules.web` availability are the two things most likely to need a tweak on first run.
