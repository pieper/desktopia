# Desktopia offload — missing-features TODO

Status (2026-06-12): a workable offload Slicer for everything **except the Segment Editor**. Render + interact
for volumes, models, segmentations, markups, ROI, transforms (applied), 2D slices (bg W/L + seg overlay +
markups), camera — all client-GPU, MRML-driven, with event-driven atomic geometry and a clinical aspect guard.
This list is the gap to full parity. Principle: **follow MRML via Slicer's own DM logic, never pixel-hack**
(see the memory note); **validate with the dual-Slicer compare** (container native vs client offload, `:2027/mcp`).

## Bugs (near-term)
- [x] **Maximize/layout: 3D annotations bleed into a maximized slice.** Double-clicking a slice to maximize it
      removes the 3D view from the layout, but the client still draws the 3D overlay + decorations (markup
      labels, R/A/S/L/P axis labels, ruler, 3D markup line) on top of the slice. Mirror of the slice-on-3D fix:
      `viewport_rect()` must return null/empty when the 3D view is **not visible in the current layout**
      (`threeDWidget(0).visible`), and the client must hide `host`/`out` + skip `drawDecorations2D`/
      `drawMarkupLabels3D` when there is no 3D viewport.
- [x] Confirm the line **measurement value** actually appends in the client label (read empty in one introspection).
- [x] Markup hover color should be **ActiveColor** (green) — currently a generic yellow highlight.

## DM area 1 — Transforms (not started)
> **vtk.js CONSTRAINT (checked 2026-06-12):** vtk.js is LINEAR-ONLY — `vtkTransform`/`vtkHomogeneousTransform`/
> `vtkAbstractTransform`/`vtkLandmarkTransform` and nothing else. NO GridTransform/ThinPlateSpline/BSpline/
> GeneralTransform/WarpTransform, NO TransformPolyDataFilter. So **do NOT implement client-side non-linear
> transform APPLICATION** — it comes free with WebGPU/vtk-wasm (full VTK). Split the work accordingly:
- [ ] **Deformation-field viz (glyph/grid)** — DOABLE NOW: the SERVER (full VTK) samples the displacement
      field within the `RegionNode` and ships `(point, displaced-point)` pairs; the client just draws vectors /
      a deformed lattice. Never touches a vtk.js transform. Needs a *warp* transform in the demo (current demo
      is linear → uniform field → nothing to show). `VisualizationMode` 0=glyph/1=grid/2=contour, `GlyphSpacingMm`,
      `GridSpacingMm` on `vtkMRMLTransformDisplayNode`.
- [x] **Interactive transform handles (LINEAR)** (translation done; rotation TODO): translate/rotate/scale widget → matrix write-back, reusing the
      existing handle/lease machinery (`EditorVisibility` + the handle-component flags on the display node).
- [ ] **Server pre-warp of geometry under a non-linear transform**: `worldMatrixCol` only composes linear
      `matrixToParent`, so a model under a warp currently renders UN-warped. Fix: at serialize time, apply the
      node's general transform to the polydata (harden/transform points) before `_pd_blobs`. (Linear stays a matrix.)
- [ ] **DEFERRED to WebGPU/vtk-wasm**: client-side application/editing of non-linear transforms.

## DM area 2 — Markup parity (remainders)
- [ ] **ROI properties label** ("CropROI") — the `vtkMRMLMarkupsROINode` branch needs label serialization too.
- [ ] **More markup types**: plane (`MarkupsPlaneNode`), angle (arc + degrees), closed-curve fill, ROI label.
- [ ] **Line thickness** from MRML `LineThickness` (currently fixed 2 px).
- [ ] **Screen-constant glyph size** on local zoom (recompute glyphRadius on camera change, not just on sync).
- [ ] **Markup placement / edit**: add + delete control points (currently only drag existing ones).
- [ ] **2D-slice markup interaction**: drag a control point *in a slice view* (project cursor→RAS on the slice
      plane, keep the out-of-plane offset, write `{mcp}` via the existing lease). Currently slice drags go to the
      server; only the 3D handles are client-local.
- [ ] 2D markup point **labels** in slices.

## DM area 3 — 2D completeness
- [ ] **Foreground volume + color LUT** compositing in slices (currently only background grayscale W/L).
- [ ] **Window/level + scroll/pan/zoom local** in slices with gated write-back (currently server round-trip).
- [ ] **Model slice intersections** (closed-surface → contour line in slice; `vtkMRMLModelSliceDisplayableManager`).
- [ ] **Segmentation as contours** in slice (currently filled labelmap overlay only).
- [ ] **Data probe**: RAS/IJK + voxel value readout at the cursor, computed locally (no round-trip).
- [ ] **Thick slab**: slab thickness + mode (max/mean/sum) as multi-tap sampling in the reslice shader.

## DM area 4 — Slice reformat + crosshair
- [ ] **Slice intersection lines**: the colored lines showing the other slice planes in each view.
- [ ] **Interactive reformat / rotate handles** (write-back to slice nodes).
- [ ] **Crosshair** (2D + 3D), with optional jump-to-slice.

## Interaction / perf polish
- [ ] Lightweight per-move update for markup/ROI drag (avoid the full `syncDMs()` re-apply each move).
- [ ] Per-node WS deltas instead of full `/mrml` re-pull on every change (scale/perf).
- [ ] Multiple 3D views (currently view 0 only) + linked slice views.

## Architecture (longer-term, see DISTRIBUTED-MRML-ARCHITECTURE.md / MRML-COUCH-DESIGN.md)
- [ ] First-class Slicer **RenderMode / render-pathway** flag on `vtkMRMLAbstractViewNode` (replace the
      keyhole `SetDraw(0)`+magenta shim with a supported API). Upstream PR.
- [ ] Event-driven (not poll) for ALL geometry — done for resize/move/layout; audit the rest.
- [ ] Collaborative / multi-place sync substrate (the CouchDB/PouchDB-vs-protocol design decisions).

## IDC + cloud track (separate from DM parity)
- [ ] IDC launch: URL params (StudyInstanceUID/SeriesInstanceUID, OHIF-style) → idc-index download from AWS in
      parallel with Slicer boot → feed into the DICOM DB as series complete → load.
- [ ] Multi-user fast-start Cloud Run (instance-per-session, scale-to-zero, <$100/mo; high concurrency + session
      affinity per user — NOT concurrency=1). No public URL; gcloud proxy for local test.
- [ ] **Faster Slicer startup (IN SCOPE — the ~60s boot is NOT irreducible).** Measured on Cloud Run: container
      start → page ~5s, but → Slicer+offload-server ~64s (Slicer boot ~60s is the cold-start wall, on top of the
      1.2GB image pull → ~80-100s cold to a usable offload). Reducing this is a project goal, not a given. Levers:
      trim the Slicer build / disable unused modules, lazy module load, headless (no Qt desktop) libSlicer,
      prebuilt/warmed or checkpoint-restore (CRIU) startup, a smaller image. Complements the warm-spare and the
      lightweight-web-viewer levers (the viewer has ZERO Slicer cold-start).

## Explicitly out of scope (for now)
- Segment Editor **preview effects** (brush cursor, level-tracing, draw-polyline): imperative Qt/VTK with no
  MRML node → no displayable manager → can't offload cleanly. The *result* (the labelmap) offloads fine.
