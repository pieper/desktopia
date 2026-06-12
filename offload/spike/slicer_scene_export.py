"""
Desktopia 3D-offload — Phase 0 spike, SERVER side (paste into Slicer's Python console).

Serializes Slicer's 3D view vtkRenderWindow to the vtk.js "synchronizable" scene format and
serves it over HTTP, so a browser client (client/index.html) can re-render the scene on the
CLIENT's GPU and interact locally. This is the "geometry/scene delivery" (a.k.a. VTK-object level)
path: the server ships scene state + data-array blobs, NOT pixels — so the server needs no GPU.

Why it rides Slicer's WebServer module: requests are dispatched on Slicer's MAIN Qt thread, so
touching live VTK objects during serialization is safe (no cross-thread VTK access).

Endpoints (CORS enabled):
  GET /scene          -> JSON scene state for 3D view 0 (re-serialized fresh each call)
  GET /scene?view=N   -> 3D view N
  GET /array?hash=H   -> raw bytes of the data array with md5 H (resolve a hash the client asks for)

Run:
  1. Tools > Extension... not needed. Just paste this whole file into the Python console.
  2. startSceneExport()        # default port 2027
  3. Open offload/spike/client/index.html in a browser (GPU machine), point it at
     http://localhost:2027  (or http://<host>:2027 through the Desktopia tunnel/ingress).
  Stop with:  sceneLogic.stop()

KNOWN UNCERTAINTIES (this is a spike — validate against a live Slicer):
  * render_window_serializer ships in VTK's Web/Python (vtkmodules.web). If Slicer's VTK build
    didn't include it, the import below fails — see _import_serializer() for the one-file fallback.
  * The 3D view render window may carry overlay renderers (orientation marker, etc.); those may
    not round-trip. Note what's missing.
  * Volume rendering fidelity in vtk.js (WebGL2) is the known weak spot — that's a key thing to judge.
"""

import base64
import hashlib
import json
import math
import os
import struct
import traceback
import urllib

import qt
import slicer
import WebServer
import WebServerLib


# ─── render_window_serializer (the pure-Python VTK->vtk.js serializer) ─────────

def _import_serializer():
    """VTK's render_window_serializer (+ its vtkmodules.web helpers) is pure Python but Slicer doesn't
    build the web module. Prefer an installed copy; otherwise load the vendored vtk_web/ package beside
    this script and register it AS `vtkmodules.web`, so the serializer's own `from vtkmodules.web import
    base64Encode, ...` resolves to the vendored helpers."""
    try:
        from vtkmodules.web import render_window_serializer as rws
        return rws
    except Exception:
        pass
    import os, sys, importlib.util, vtkmodules
    here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
    pkg_dir = os.path.join(here, "vtk_web")
    init = os.path.join(pkg_dir, "__init__.py")
    if not os.path.exists(init):
        raise ImportError(
            f"vendored vtk_web/ not found at {pkg_dir}. Fetch VTK's Web/Python/vtkmodules/web/"
            "(__init__.py + render_window_serializer.py) into that dir.")
    spec = importlib.util.spec_from_file_location(
        "vtkmodules.web", init, submodule_search_locations=[pkg_dir])
    web = importlib.util.module_from_spec(spec)
    sys.modules["vtkmodules.web"] = web
    setattr(vtkmodules, "web", web)
    spec.loader.exec_module(web)                       # defines base64Encode/hashDataArray/getReferenceId/…
    from vtkmodules.web import render_window_serializer as rws
    return rws


rws = _import_serializer()
rws.initializeSerializers()

# Slicer's volume rendering uses GPU mapper classes the stock serializer doesn't register (selection is
# by EXACT class name). Map them to the generic volume-mapper serializer + the vtk.js "vtkVolumeMapper"
# class so the volume (image data + transfer functions) actually transfers and renders client-side.
for _m in ("vtkGPUVolumeRayCastMapper", "vtkOpenGLGPUVolumeRayCastMapper",
           "vtkSmartVolumeMapper", "vtkMultiVolumeMapper", "vtkFixedPointVolumeRayCastMapper"):
    try:
        rws.registerInstanceSerializer(_m, rws.genericVolumeMapperSerializer)
        rws.registerJSClass(_m, "vtkVolumeMapper")
    except Exception as _ex:
        print("offload: could not register volume mapper", _m, _ex, flush=True)

# The keyhole cover (below) is a vtkActor2D added to the LIVE renderer to blank the server's 3D content in
# the video. Register a no-op serializer so it is silently skipped (never shipped to the client) instead of
# logging "!!!No serializer for vtkActor2D" on every /scene fetch.
rws.registerInstanceSerializer("vtkActor2D", lambda *a: None)


# Visibility is a SYNCED property: serialize each actor/volume with its real visibility so the client
# shows/hides exactly what Slicer does. (An earlier force-visible wrapper for a visibility-based keyhole
# was removed -- hiding content for the keyhole must NOT change the visibility the client sees. The
# server-render-suppression keyhole will be done by a mechanism decoupled from object visibility.)

# Keep the most recent serialization context alive so /array can resolve the hashes the client
# requests AFTER it has fetched /scene. (One client at a time for the spike.)
_context = None


def _three_d_render_window(view_index=0):
    widget = slicer.app.layoutManager().threeDWidget(view_index)
    if widget is None:
        raise RuntimeError(f"No 3D widget at index {view_index}")
    return widget.threeDView().renderWindow()


def _iter_collection(coll):
    coll.InitTraversal()
    while True:
        item = coll.GetNextItemAsObject()
        if item is None:
            return
        yield item


def _imagedata_state(node):
    if not isinstance(node, dict):
        return None
    if "ImageData" in (node.get("type") or "") or "StructuredPoints" in (node.get("type") or ""):
        return node
    for dep in (node.get("dependencies") or []):
        found = _imagedata_state(dep)
        if found:
            return found
    return None


def _fix_volume_geometry(rw, state):
    """Slicer keeps a volume's geometry in the vtkVolume's UserMatrix (IJK->RAS) while the image data is
    unit-spacing IJK; render_window_serializer ships neither correctly, so vtk.js draws a cube. Fix it in
    two parts: put the anisotropic SPACING into the image data (vtk.js's volume ray-caster samples that
    with the right aspect), and give the volume a RIGID (rotation+translation, NO scale) user matrix for
    RAS placement -- a non-uniform-scale user matrix is what distorted the render."""
    vol_by_id = {}
    for ren in _iter_collection(rw.GetRenderers()):
        for v in _iter_collection(ren.GetVolumes()):
            m = v.GetUserMatrix()
            if m is not None:
                vol_by_id[rws.getReferenceId(v)] = m
    if not vol_by_id:
        return

    def walk(node):
        if not isinstance(node, dict):
            return
        m = vol_by_id.get(node.get("id"))
        if m is not None:
            spacing = [math.sqrt(sum(m.GetElement(r, c) ** 2 for r in range(3))) or 1.0 for c in range(3)]
            img = _imagedata_state(node)
            if img is not None:                                  # aspect: spacing into the image data
                p = img.setdefault("properties", {})
                p["spacing"] = spacing
                p["origin"] = [0.0, 0.0, 0.0]
                p["direction"] = [1, 0, 0, 0, 1, 0, 0, 0, 1]
            mat = []                                             # rigid (scale-removed) user matrix, column-major
            for c in range(4):
                for r in range(4):
                    if r == 3:
                        mat.append(1.0 if c == 3 else 0.0)
                    elif c == 3:
                        mat.append(m.GetElement(r, 3))           # translation
                    else:
                        mat.append(m.GetElement(r, c) / spacing[c])  # rotation, scale factored out
            node.setdefault("calls", []).append(["setUserMatrix", [mat]])
        for dep in (node.get("dependencies") or []):
            walk(dep)

    walk(state)


def export_scene_state(view_index=0, full=False):
    """Serialize 3D view `view_index`'s render window to the vtk.js synchronizable state dict.

    CRITICAL: serialize on the SAME (main) thread, and FRESH. The change fingerprint comes from MRML node
    MTimes, which update the instant the user toggles something -- but the VTK actors the serializer reads
    are only refreshed by Slicer's displayable managers on the view's render. A raw renderWindow.Render()
    does NOT run that update, so the actors can lag the version: we'd serialize the OLD state under the NEW
    version and the client would lock it in (== "even waiting doesn't fix it"). qMRMLThreeDView.forceRender()
    runs the displayable-manager update synchronously, so the serialized actors always match the version."""
    global _context
    widget = slicer.app.layoutManager().threeDWidget(view_index)
    if widget is None:
        raise RuntimeError(f"No 3D widget at index {view_index}")
    view = widget.threeDView()
    view.forceRender()                            # synchronous displayable-manager update -> fresh actors
    rw = view.renderWindow()
    # PERSISTENT context: the vtk.js synchronizer is INCREMENTAL -- the context remembers what the client
    # already has so each serialize emits the right addViewProp/removeViewProp diff. Recreating it per call
    # (the old bug) erased that memory, so show/hide of actors applied only randomly. Reset it ONLY for a
    # full resync (a fresh/reconnected client that has no prior state).
    if _context is None or full:
        _context = rws.SynchronizationContext()
    rid = rws.getReferenceId(rw)
    state = rws.serializeInstance(None, rw, rid, _context, 0)
    _fix_volume_geometry(rw, state)               # Slicer stores volume geometry in the actor UserMatrix
    return state


def viewport_rect(view_index=0):
    """Where 3D view `view_index` sits on the streamed desktop, in SCREEN pixels. The video captures the
    Xvfb/Xwayland screen at native resolution, so screen px == stream px == the client's #v canvas pixels
    -- the browser maps these straight through #v's object-fit:contain to float the local vtk.js canvas
    over the video's 3D-view region. mapToGlobal gives the widget top-left in screen coords."""
    import qt
    widget = slicer.app.layoutManager().threeDWidget(view_index)
    if widget is None or not widget.visible:   # 3D view NOT in the current layout (e.g. a slice maximized) -> no
        return None                            # overlay -> the client hides host/out + skips the 3D decorations
    tdv = widget.threeDView()
    tl = tdv.mapToGlobal(qt.QPoint(0, 0))
    return {"x": tl.x(), "y": tl.y(), "w": tdv.width, "h": tdv.height}


def slice_viewports():
    """Screen-pixel rect of each 2D slice view on the streamed desktop (the 2D analog of viewport_rect),
    keyed by layoutName -- the client floats its locally-resliced slice over each keyholed region."""
    import qt
    lm = slicer.app.layoutManager()
    out = {}
    try:
        names = list(lm.sliceViewNames())
    except Exception:
        names = ["Red", "Yellow", "Green"]
    for name in names:
        w = lm.sliceWidget(name)
        if w is None or not w.visible:        # skip slice views NOT in the current layout (e.g. 3D-only) -- else the
            continue                          # client keeps compositing their slices on top of the 3D view
        sv = w.sliceView()
        tl = sv.mapToGlobal(qt.QPoint(0, 0))
        out[name] = {"x": tl.x(), "y": tl.y(), "w": sv.width, "h": sv.height}
    return out


def slice_nodes_state():
    """Re-serialize the current slice VIEW nodes (dimensions + xyToRAS, no blobs) so a geometry push carries
    the reslice geometry ATOMICALLY with the screen rects -- the client never reslices a NEW rect against
    STALE node dimensions (the transient wrong-aspect-on-resize bug). Cheap (matrices only); rides every
    state/pong/geometry-event push."""
    import mrml_sync
    out = {}
    lm = slicer.app.layoutManager()
    try:
        names = list(lm.sliceViewNames())
    except Exception:
        names = ["Red", "Yellow", "Green"]
    for name in names:
        w = lm.sliceWidget(name)
        if w is None or not w.visible:        # only slice views shown in the current layout (matches slice_viewports)
            continue
        sn = w.mrmlSliceNode()
        if sn is not None:
            out[sn.GetID()] = mrml_sync.serialize_node(sn)
    return out


def segment_editor_state():
    """The active Segment Editor effect + its parameters (so the client can draw the brush cursor LOCALLY
    over the keyholed slices -- the server's own brush feedback is suppressed by the slice keyhole, and a
    client-drawn cursor is instant instead of riding the video round-trip). Non-intrusive: only reads the
    transient vtkMRMLSegmentEditorNode if the Segment Editor module created one. None when not editing."""
    sen = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLSegmentEditorNode")
    if sen is None:
        return None
    eff = sen.GetActiveEffectName()
    if not eff:
        return None
    try:
        attrs = {n: sen.GetAttribute(n) for n in sen.GetAttributeNames()}
    except Exception:
        attrs = {}
    return {"effect": eff, "attrs": attrs}


def set_camera(view_index, cam):
    """Apply a camera (from the client's local interaction) to Slicer's 3D view via its MRML camera node,
    so the server stays consistent (screenshots, linked views, other viewers). Runs on the main Qt thread
    (WebServer dispatch), so MRML access is safe."""
    widget = slicer.app.layoutManager().threeDWidget(view_index)
    if widget is None:
        return {"error": f"no 3D widget {view_index}"}
    camNode = slicer.modules.cameras.logic().GetViewActiveCameraNode(widget.mrmlViewNode())
    if camNode is None:
        return {"error": "no camera node"}
    c = camNode.GetCamera()
    if cam.get("position"):     c.SetPosition(*cam["position"])
    if cam.get("focalPoint"):   c.SetFocalPoint(*cam["focalPoint"])
    if cam.get("viewUp"):       c.SetViewUp(*cam["viewUp"])
    if cam.get("viewAngle"):    c.SetViewAngle(cam["viewAngle"])
    if cam.get("parallelScale"): c.SetParallelScale(cam["parallelScale"])
    if "parallelProjection" in cam: c.SetParallelProjection(1 if cam["parallelProjection"] else 0)
    camNode.Modified()                                 # propagate to the view (+ linked views/marker)
    return {"ok": True}


def _scene_fingerprint():
    """A cheap change signal: sum of MRML modified-times for the nodes that affect the 3D scene (data,
    display, transfer functions, transforms) -- EXCLUDING camera nodes (the client owns the camera, so a
    camera change must NOT trigger a scene re-fetch). Changes iff something the client should re-sync did."""
    fp = int(slicer.mrmlScene.GetMTime())
    for cls in ("vtkMRMLDisplayNode", "vtkMRMLVolumePropertyNode", "vtkMRMLVolumeNode",
                "vtkMRMLModelNode", "vtkMRMLSegmentationNode", "vtkMRMLTransformNode",
                "vtkMRMLMarkupsNode",                       # ROI/fiducials/etc. -- catch ROI crop moves
                "vtkMRMLSliceNode", "vtkMRMLSliceCompositeNode"):   # slice scroll/pan/zoom + bg/fg selection
        for n in slicer.util.getNodesByClass(cls):
            fp += int(n.GetMTime())
            # Several edits live in objects OWNED by a node whose own MTime can lag what the 3D view shows.
            # Fold those inner MTimes in so the change is caught on the fast event path (not just reconcile):
            try:
                if cls == "vtkMRMLSegmentationNode":               # segment paint -> inner vtkSegmentation
                    seg = n.GetSegmentation()
                    if seg is not None:
                        fp += int(seg.GetMTime())
                elif cls == "vtkMRMLVolumePropertyNode":           # transfer-function drag -> the TFs
                    vp = n.GetVolumeProperty()
                    if vp is not None:
                        fp += int(vp.GetMTime())
                        rgb = vp.GetRGBTransferFunction()
                        sop = vp.GetScalarOpacity()
                        gop = vp.GetGradientOpacity() if vp.GetDisableGradientOpacity() == 0 else None
                        for f in (rgb, sop, gop):
                            if f is not None:
                                fp += int(f.GetMTime())
                elif cls == "vtkMRMLMarkupsNode":                  # control-point moves -> fold positions in so a
                    arr = slicer.util.arrayFromMarkupsControlPoints(n)   # curve/line/ROI drag triggers a re-push
                    if arr is not None and arr.size:
                        import numpy as _np
                        fp += int(_np.abs(arr).sum() * 1000)
            except Exception:
                pass
    return fp


def view_state(view_index=0):
    """Cheap, poll-this-fast endpoint: the scene fingerprint (re-fetch /scene only when it changes) plus
    the current viewport rect (so the overlay tracks the 3D view live, incl. during window drags)."""
    return {"version": _scene_fingerprint(), "viewport": viewport_rect(view_index),
            "sliceViewports": slice_viewports(), "sliceNodes": slice_nodes_state(),
            "segEdit": segment_editor_state()}


def get_camera(view_index=0):
    """Read back Slicer's current 3D camera (for verifying camera sync)."""
    widget = slicer.app.layoutManager().threeDWidget(view_index)
    camNode = slicer.modules.cameras.logic().GetViewActiveCameraNode(widget.mrmlViewNode())
    c = camNode.GetCamera()
    return {"position": list(c.GetPosition()), "focalPoint": list(c.GetFocalPoint()),
            "viewUp": list(c.GetViewUp()), "parallelScale": c.GetParallelScale()}


_keyhole_state = {}    # view_index -> {"cover": vtkRenderer, "suppressed": [(renderer, prevDraw), ...]}

# Chroma key painted over the ENTIRE server 3D render. The client keys this color OUT and renders its OWN
# 3D in its place. Pure magenta is distinct from Slicer's slice colors (red/yellow/green) and UI grays.
# Keep in sync with KEY in offload-overlay.js.
_KEYHOLE_RGB = (255, 0, 255)


def _make_keyhole_cover():
    """A viewport-filling vtkActor2D painting the flat chroma-key color (normalized viewport coords)."""
    import vtk
    pts = vtk.vtkPoints()
    pts.InsertNextPoint(0, 0, 0); pts.InsertNextPoint(1, 0, 0)
    pts.InsertNextPoint(1, 1, 0); pts.InsertNextPoint(0, 1, 0)
    cells = vtk.vtkCellArray(); cells.InsertNextCell(4)
    for i in range(4):
        cells.InsertCellPoint(i)
    pd = vtk.vtkPolyData(); pd.SetPoints(pts); pd.SetPolys(cells)
    coord = vtk.vtkCoordinate(); coord.SetCoordinateSystemToNormalizedViewport()
    mapper = vtk.vtkPolyDataMapper2D(); mapper.SetInputData(pd); mapper.SetTransformCoordinate(coord)
    actor = vtk.vtkActor2D(); actor.SetMapper(mapper)
    actor.GetProperty().SetColor(*[c / 255.0 for c in _KEYHOLE_RGB]); actor.GetProperty().SetOpacity(1.0)
    return actor


def set_keyhole(view_index, on):
    """Keyhole = the server-render replacement signal. ON: (1) SUPPRESS the expensive scene render --
    SetDraw(0) on every existing (scene) renderer, so the server stops doing the software-GL work (volume
    rendering, models, slice-interaction re-renders) for a 3D view the CLIENT is rendering anyway; (2) add a
    DEDICATED TOP-LAYER renderer that fills the viewport with the chroma key (self-clearing magenta + an
    opaque viewport actor) so the client keys it out and draws its own 3D in its place. Measured: a 3D render
    drops from ~tens of ms (more on llvmpipe / with volume rendering) to ~0.2 ms -- which is the dominant
    cost of slice-view interaction lag, since each active move re-renders the 3D view too. OFF: restore each
    renderer's SetDraw and remove the cover (so screenshots / other viewers / the /_shot path see the real
    scene again).

    This is the pure-Python 5.10 stand-in for a proper Slicer feature: a per-view 'render pathway' flag on
    the view node telling the app this view is rendered remotely, so it short-circuits its own scene render."""
    import vtk
    rw = _three_d_render_window(view_index)
    st = _keyhole_state.get(view_index)
    if on and st is None:
        suppressed = []
        rens = rw.GetRenderers(); rens.InitTraversal()
        r = rens.GetNextItem()
        while r is not None:                            # record + stop drawing every current (scene) renderer
            suppressed.append((r, r.GetDraw()))
            r.SetDraw(0)
            r = rens.GetNextItem()
        cover = vtk.vtkRenderer()                       # cheap magenta placeholder on a new top layer
        layer = rw.GetNumberOfLayers()
        rw.SetNumberOfLayers(layer + 1)
        cover.SetLayer(layer)                           # topmost -> rendered last
        cover.InteractiveOff()
        cover.SetViewport(0, 0, 1, 1)
        cover.SetBackground(*[c / 255.0 for c in _KEYHOLE_RGB]); cover.EraseOn()   # clears the buffer to magenta
        cover.AddActor2D(_make_keyhole_cover())         # + an opaque viewport fill (belt & suspenders at edges)
        rw.AddRenderer(cover)
        _keyhole_state[view_index] = {"cover": cover, "suppressed": suppressed}
        rw.Render()
    elif not on and st is not None:
        for r, prev in st["suppressed"]:                # restore real rendering
            r.SetDraw(prev)
        rw.RemoveRenderer(st["cover"])
        maxlayer = 0
        rens = rw.GetRenderers(); rens.InitTraversal()
        r = rens.GetNextItem()
        while r is not None:
            maxlayer = max(maxlayer, r.GetLayer()); r = rens.GetNextItem()
        rw.SetNumberOfLayers(maxlayer + 1)
        _keyhole_state.pop(view_index, None)
        rw.Render()
    return {"ok": True, "keyhole": bool(on)}


_slice_keyhole_state = {}   # layoutName -> {"cover": vtkRenderer, "suppressed": [(renderer, prevDraw), ...]}


def set_slice_keyhole(on):
    """The 2D analog of set_keyhole: for EACH slice view, suppress the server's scene render (SetDraw(0))
    and paint the magenta keyhole, so the client's locally-resliced slice shows through. The client reads
    the slice node (xyToRAS etc.) from MRML and reslices the volume itself, so the server never needs to
    render the slice pixels. OFF restores real slice rendering (screenshots / non-offload viewers)."""
    import vtk
    lm = slicer.app.layoutManager()
    try:
        names = list(lm.sliceViewNames())
    except Exception:
        names = ["Red", "Yellow", "Green"]
    for name in names:
        w = lm.sliceWidget(name)
        if w is None:
            continue
        rw = w.sliceView().renderWindow()
        st = _slice_keyhole_state.get(name)
        if on and st is None:
            suppressed = []
            rens = rw.GetRenderers(); rens.InitTraversal(); r = rens.GetNextItem()
            while r is not None:
                suppressed.append((r, r.GetDraw())); r.SetDraw(0); r = rens.GetNextItem()
            cover = vtk.vtkRenderer()
            layer = rw.GetNumberOfLayers(); rw.SetNumberOfLayers(layer + 1)
            cover.SetLayer(layer); cover.InteractiveOff(); cover.SetViewport(0, 0, 1, 1)
            cover.SetBackground(*[c / 255.0 for c in _KEYHOLE_RGB]); cover.EraseOn()
            cover.AddActor2D(_make_keyhole_cover())
            rw.AddRenderer(cover)
            _slice_keyhole_state[name] = {"cover": cover, "suppressed": suppressed}
            rw.Render()
        elif not on and st is not None:
            for r, prev in st["suppressed"]:
                r.SetDraw(prev)
            rw.RemoveRenderer(st["cover"])
            maxlayer = 0
            rens = rw.GetRenderers(); rens.InitTraversal(); r = rens.GetNextItem()
            while r is not None:
                maxlayer = max(maxlayer, r.GetLayer()); r = rens.GetNextItem()
            rw.SetNumberOfLayers(maxlayer + 1)
            _slice_keyhole_state.pop(name, None)
            rw.Render()
    return {"ok": True, "sliceKeyhole": bool(on)}


def get_array_bytes(md5):
    """Raw bytes for a data array the client requested by hash (from the live context)."""
    if _context is None:
        return None
    return _context.getCachedDataArray(md5, binary=True)


# ─── WebSocket event channel (qt.QTcpServer; Qt event loop -- no asyncio/threads) ───────────────
# Event-driven offload control channel, SEPARATE from the Desktopia video transport. Pushes scene-change
# events (so the client re-fetches /scene ONLY when something changed) + the live viewport rect, and
# receives camera updates. Minimal RFC6455 (handshake + frames). Upstreamable to the WebServer module.

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSServer:
    def __init__(self, port, on_message, on_open=None):
        import qt
        self.on_message = on_message
        self.on_open = on_open            # called with the new client socket right after the handshake
        self.clients = []
        self._buf = {}
        self._up = set()
        self.server = qt.QTcpServer()
        self.server.newConnection.connect(self._accept)
        # Only the Any enum binds in this PythonQt (AnyIPv4 / "0.0.0.0" string make listen() fail); Any is
        # IPv6 "::" with dual-stack, which still accepts IPv4 (incl. docker-proxy's v4-mapped backend connect).
        ok = self.server.listen(qt.QHostAddress(qt.QHostAddress.Any), port)
        print(f"offload WS listen ok={ok} addr={self.server.serverAddress().toString()}", flush=True)
        self.port = self.server.serverPort()
        print(f"offload WS on tcp/{self.port}", flush=True)

    def _accept(self):
        while self.server.hasPendingConnections():
            s = self.server.nextPendingConnection()
            self._buf[s] = bytearray()
            s.readyRead.connect(lambda s=s: self._read(s))
            s.disconnected.connect(lambda s=s: self._drop(s))

    def _drop(self, s):
        self._up.discard(s); self._buf.pop(s, None)
        if s in self.clients:
            self.clients.remove(s)

    def _read(self, s):
        raw = s.readAll()
        # PythonQt: bytes(QByteArray) yields empty -- go through .data() to get the real payload bytes.
        chunk = bytes(raw.data()) if hasattr(raw, "data") else bytes(raw)
        self._buf[s] += chunk
        if s not in self._up:
            if b"\r\n\r\n" in self._buf[s]:
                self._handshake(s)
        else:
            self._frames(s)

    def _handshake(self, s):
        req = bytes(self._buf[s]); self._buf[s] = bytearray()
        key = b""
        for line in req.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-key:"):
                key = line.split(b":", 1)[1].strip()
        accept = base64.b64encode(hashlib.sha1(key + _WS_GUID).digest())
        resp = (b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        s.write(resp); s.flush()
        self._up.add(s); self.clients.append(s)
        if self.on_open:
            try: self.on_open(s)
            except Exception: print("offload ws on_open:", traceback.format_exc(), flush=True)

    def _frames(self, s):
        buf = self._buf[s]
        while len(buf) >= 2:
            opcode = buf[0] & 0x0F
            masked = buf[1] & 0x80
            n = buf[1] & 0x7F
            i = 2
            if n == 126:
                if len(buf) < 4: break
                n = struct.unpack(">H", buf[2:4])[0]; i = 4
            elif n == 127:
                if len(buf) < 10: break
                n = struct.unpack(">Q", buf[2:10])[0]; i = 10
            if masked:
                if len(buf) < i + 4 + n: break
                m = buf[i:i + 4]; i += 4
                payload = bytes(buf[i + j] ^ m[j % 4] for j in range(n))
            else:
                if len(buf) < i + n: break
                payload = bytes(buf[i:i + n])
            i += n
            del buf[:i]
            if opcode == 0x8:
                s.close(); return
            elif opcode == 0x9:
                self._send(s, payload, 0xA)
            elif opcode in (0x1, 0x2):
                try:
                    self.on_message(payload.decode("utf-8", "replace"))
                except Exception:
                    print("offload ws msg:", traceback.format_exc(), flush=True)

    def _send(self, s, data, opcode=0x1):
        if isinstance(data, str):
            data = data.encode("utf-8")
        h = bytearray([0x80 | opcode]); n = len(data)
        if n < 126:
            h.append(n)
        elif n < 65536:
            h.append(126); h += struct.pack(">H", n)
        else:
            h.append(127); h += struct.pack(">Q", n)
        try:
            s.write(bytes(h) + data); s.flush()
        except Exception:
            pass

    def broadcast(self, obj):
        msg = json.dumps(obj)
        for s in list(self.clients):
            self._send(s, msg)


_ws = None
_last_fp = [None]
_ws_timers = []


def _set_markups_roi(node_id, center, size):
    """Apply a client-dragged ROI back to the MRML markups ROI node (the remote/eventual side of the
    impedance-matching loop). The client renders + crops locally at full rate; this lands the gated,
    drop-to-latest updates so the server's slice views + cropping converge."""
    if not node_id:
        return
    node = slicer.mrmlScene.GetNodeByID(node_id)
    if node is None or not node.IsA("vtkMRMLMarkupsROINode"):
        return
    # ATOMIC: SetCenter shifts the whole box and SetSize then fixes the near face; if a render interleaves
    # between them the FAR face jumps and snaps back (the "wobble"). Batch so only ONE Modified fires.
    was = node.StartModify()
    try:
        if center and len(center) == 3:
            node.SetCenter(*center)
        if size and len(size) == 3:
            node.SetSize(*size)
    finally:
        node.EndModify(was)


def _set_markup_point(node_id, index, pos):
    """Apply a client-dragged control point back to a markups node (the general markup-handle write-back)."""
    if not node_id or index is None or not pos or len(pos) != 3:
        return
    node = slicer.mrmlScene.GetNodeByID(node_id)
    if node is None or not node.IsA("vtkMRMLMarkupsNode"):
        return
    if 0 <= int(index) < node.GetNumberOfControlPoints():
        node.SetNthControlPointPositionWorld(int(index), pos[0], pos[1], pos[2])


def _set_transform_matrix(node_id, matrix):
    """Apply a client-edited matrix back to a LINEAR transform node (the transform interaction widget)."""
    if not node_id or not matrix or len(matrix) != 16:
        return
    node = slicer.mrmlScene.GetNodeByID(node_id)
    if node is None or not node.IsA("vtkMRMLTransformNode") or not node.IsLinear():
        return
    import vtk
    m = vtk.vtkMatrix4x4()
    for r in range(4):
        for c in range(4):
            m.SetElement(r, c, matrix[r * 4 + c])   # row-major (matches mrml_sync._matrix4x4)
    node.SetMatrixTransformToParent(m)


def _ws_on_message(text):
    try:
        m = json.loads(text)
    except Exception:
        return
    if m.get("t") == "camera":
        set_camera(int(m.get("view", 0)), m.get("cam", {}))
    elif m.get("t") == "keyhole":
        set_keyhole(int(m.get("view", 0)), bool(m.get("on", True)))
    elif m.get("t") == "roi":
        _set_markups_roi(m.get("id"), m.get("center"), m.get("size"))
        if _ws is not None:
            _ws.broadcast({"t": "ack"})        # the client gates its next write on this -> rate adapts to RTT
    elif m.get("t") == "mcp":                  # markup control-point move (fiducials/line/curve handles)
        _set_markup_point(m.get("id"), m.get("index"), m.get("pos"))
        if _ws is not None:
            _ws.broadcast({"t": "ack"})
    elif m.get("t") == "transform":            # linear transform interaction widget -> new matrix
        _set_transform_matrix(m.get("id"), m.get("matrix"))
        if _ws is not None:
            _ws.broadcast({"t": "ack"})
    elif m.get("t") == "ping":
        # liveness heartbeat doubling as a reconcile: a wedged Slicer stops ponging (client blanks its
        # overlay); and carrying the current version/viewport lets the client catch any push it missed
        # (a dropped change-only push is never re-announced otherwise). Pong goes to all clients; harmless.
        if _ws is not None:
            try:
                st = view_state(0)
            except Exception:
                st = {}
            _ws.broadcast({"t": "pong", **st})


def _ws_on_open(client):
    """A freshly-connected client needs the current state immediately (the change-only pushes only fire on
    a CHANGE, which may never come while it waits). Send it the current view_state right after handshake."""
    if _ws is None:
        return
    try:
        st = view_state(0)
        _ws._send(client, json.dumps({"t": "state", **st}))
    except Exception:
        print("offload ws initial push:", traceback.format_exc(), flush=True)


_last_vp = [None]        # last viewport rect broadcast (so we push it as an event, only when it changes)
_push_pending = [False]  # coalesce bursts of MRML modifies (e.g. dragging a transfer-function handle)


def _ws_push(force=False):
    """Broadcast {t:state} ONLY when something the client cares about changed -- the scene fingerprint
    (gates the heavy /scene re-fetch) or the viewport rect. No change -> no traffic (no idle polling)."""
    if _ws is None or not _ws.clients:
        return
    try:
        st = view_state(0)
    except Exception:
        return
    if force or st["version"] != _last_fp[0] or st["viewport"] != _last_vp[0]:
        _last_fp[0] = st["version"]
        _last_vp[0] = st["viewport"]
        _ws.broadcast({"t": "state", **st})


def _schedule_push():
    """Push synchronously on EVERY MRML change -- no debounce/coalescing (events must not be dropped). The
    push itself is just a small {t:state, version} frame; the client decides whether to re-fetch /scene by
    comparing versions, so per-change pushes are cheap and never lose an update."""
    _ws_push(False)


# --- EVENT-DRIVEN geometry (NO QTimer screen-scraping). Push view/slice geometry when the actual Qt
# resize/move/layout events fire -- from a Slicer splitter/layout change OR a window-manager resize/move of
# the Slicer window (both deliver Resize/Move to the view widgets / main window). Replaces the old 50ms poll.
# A burst of events is coalesced into ONE push via a 0-delay single-shot: event-TRIGGERED, not periodic. ---
_geom_filters = []          # [(widget, filterObj)] kept alive so the filters aren't GC'd
_geom_pending = [False]


class _GeomFilter(qt.QObject):
    def eventFilter(self, obj, event):
        try:
            if event.type() in (qt.QEvent.Resize, qt.QEvent.Move):
                _schedule_geom_push()
        except Exception:
            pass
        return False            # never consume the event


def _schedule_geom_push():
    if _geom_pending[0]:
        return
    _geom_pending[0] = True
    qt.QTimer.singleShot(0, _flush_geom_push)     # coalesce a burst of resize/move events into one push


def _flush_geom_push():
    _geom_pending[0] = False
    _ws_push(force=True)        # geometry changed -> push new rects + slice-node geometry atomically


def _install_geom_filters():
    """(Re)install resize/move event filters on the main window + every view widget. Called once at setup
    and again on layoutChanged (the widget set changes)."""
    for w, f in _geom_filters:
        try:
            w.removeEventFilter(f)
        except Exception:
            pass
    _geom_filters.clear()
    lm = slicer.app.layoutManager()
    widgets = []
    mw = slicer.util.mainWindow()
    if mw is not None:
        widgets.append(mw)                        # WM move/resize of the whole Slicer window
    try:
        for name in lm.sliceViewNames():
            sw = lm.sliceWidget(name)
            if sw is not None:
                widgets.append(sw.sliceView())    # slice view resize (splitter/layout)
    except Exception:
        pass
    try:
        for i in range(lm.threeDViewCount):
            tw = lm.threeDWidget(i)
            if tw is not None:
                widgets.append(tw.threeDView())   # 3D view resize/move
    except Exception:
        pass
    for w in widgets:
        f = _GeomFilter()
        w.installEventFilter(f)
        _geom_filters.append((w, f))


def _setup_ws_events():
    """Fully event-driven: MRML changes push the new fingerprint immediately (coalesced ~60fps). New nodes
    get their own Modified observer as they're added. View/window GEOMETRY rides Qt resize/move/layout
    EVENTS (see _install_geom_filters) -- no QTimer screen-scraping."""
    import qt, vtk
    obs_classes = ("vtkMRMLDisplayNode", "vtkMRMLVolumePropertyNode", "vtkMRMLVolumeNode",
                   "vtkMRMLModelNode", "vtkMRMLSegmentationNode", "vtkMRMLTransformNode",
                   "vtkMRMLMarkupsNode", "vtkMRMLSliceNode", "vtkMRMLSliceCompositeNode")
    def observe(node):
        try:
            if node is not None and node.IsA("vtkMRMLNode"):
                node.AddObserver(vtk.vtkCommand.ModifiedEvent, lambda *a: _schedule_push())
        except Exception:
            pass
    s = slicer.mrmlScene

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def on_node_added(caller, event, calldata):    # calldata = the newly added node (observe it immediately)
        observe(calldata)
        _schedule_push()

    s.AddObserver(s.NodeAddedEvent, on_node_added)
    s.AddObserver(s.NodeRemovedEvent, lambda *a: _schedule_push())
    for cls in obs_classes:
        for n in slicer.util.getNodesByClass(cls):
            observe(n)
    _install_geom_filters()                       # event-driven view/window geometry (replaces the 50ms poll)
    try:
        lm = slicer.app.layoutManager()
        lm.connect('layoutChanged(int)', lambda *a: (_install_geom_filters(), _schedule_geom_push()))
    except Exception:
        pass


# ─── WebServer request handler ─────────────────────────────────────────────────

_CLIENT_DIR = ("/opt/desktopia/offload/spike/client"            # live bind mount in the local docker (edit -> reload)
               if os.path.isdir("/opt/desktopia/offload/spike/client")
               else os.path.join(os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd(), "client"))
_CTYPES = {"html": "text/html; charset=utf-8", "js": "text/javascript", "css": "text/css",
           "json": "application/json", "svg": "image/svg+xml", "png": "image/png", "ico": "image/x-icon"}


class SceneExportHandler(WebServerLib.BaseRequestHandler):

    def __init__(self, logMessage=None):           # BaseRequestHandler requires __init__ (abstract)
        self.logMessage = logMessage or (lambda *a, **k: None)

    def canHandleRequest(self, uri, **_kwargs):
        return 0.5                                 # the only handler here: serves /scene, /array, AND the client page

    def handleRequest(self, method, uri, requestBody, **_kwargs):
        parsed = urllib.parse.urlparse(uri)
        path = parsed.path
        try:
            if path == b"/_shot":                      # TEMP diagnostic: capture the server's real 3D render
                import vtk
                keep = b"keep=1" in (parsed.query or b"")
                if not keep:
                    set_keyhole(0, False)              # drop the cover so we see the actual volume
                w = _three_d_render_window(0); w.Render()
                w2i = vtk.vtkWindowToImageFilter(); w2i.SetInput(w); w2i.ReadFrontBufferOff(); w2i.Update()
                pw = vtk.vtkPNGWriter(); pw.SetFileName("/opt/desktopia/offload/spike/_shot.png")
                pw.SetInputConnection(w2i.GetOutputPort()); pw.Write()
                if not keep:
                    set_keyhole(0, True)
                return b"application/json", b'{"ok":true}'

            if path == b"/mrml":                       # MRML-level sync: the 3D view's reference closure
                import mrml_sync
                qs = urllib.parse.parse_qs(parsed.query)
                raw = (qs.get(b"view") or qs.get("view") or [b"0"])[0]
                view = int(raw.decode() if isinstance(raw, bytes) else raw)
                return b"application/json", json.dumps(mrml_sync.mrml_state(view)).encode()

            if path == b"/blob":                       # content-addressed binary (VTP/VTI) for the MRML path
                import mrml_sync
                qs = urllib.parse.parse_qs(parsed.query)
                raw = (qs.get(b"hash") or qs.get("hash") or [b""])[0]
                data = mrml_sync.get_blob(raw.decode() if isinstance(raw, bytes) else raw)
                if data is None:
                    return b"application/json", b'{"error":"unknown blob; GET /mrml first"}'
                return b"application/octet-stream", bytes(data)

            if path == b"/scene":
                qs = urllib.parse.parse_qs(parsed.query)
                raw = (qs.get(b"view") or qs.get("view") or [b"0"])[0]
                view = int(raw.decode() if isinstance(raw, bytes) else raw)
                full = bool((qs.get(b"full") or qs.get("full") or [b""])[0])   # reset the incremental context
                state = export_scene_state(view, full=full)
                return b"application/json", json.dumps(state).encode()

            if path == b"/array":
                qs = urllib.parse.parse_qs(parsed.query)
                raw = (qs.get(b"hash") or qs.get("hash") or [b""])[0]
                md5 = raw.decode() if isinstance(raw, bytes) else raw
                data = get_array_bytes(md5)
                if data is None:
                    return b"application/json", b'{"error":"unknown hash; GET /scene first"}'
                return b"application/octet-stream", bytes(data)

            if path in (b"/viewport", b"/state"):
                qs = urllib.parse.parse_qs(parsed.query)
                raw = (qs.get(b"view") or qs.get("view") or [b"0"])[0]
                view = int(raw.decode() if isinstance(raw, bytes) else raw)
                fn = view_state if path == b"/state" else viewport_rect
                return b"application/json", json.dumps(fn(view)).encode()

            if path in (b"/camera", b"/keyhole"):
                qs = urllib.parse.parse_qs(parsed.query)
                raw = (qs.get(b"view") or qs.get("view") or [b"0"])[0]
                view = int(raw.decode() if isinstance(raw, bytes) else raw)
                if path == b"/camera":
                    if requestBody:                    # POST: client's local camera -> Slicer MRML camera
                        return b"application/json", json.dumps(set_camera(view, json.loads(requestBody))).encode()
                    return b"application/json", json.dumps(get_camera(view)).encode()   # GET: read back
                rawon = (qs.get(b"on") or qs.get("on") or [b"1"])[0]
                on = (rawon.decode() if isinstance(rawon, bytes) else rawon) not in ("0", "false", "")
                return b"application/json", json.dumps(set_keyhole(view, on)).encode()

            # everything else: serve the client page from offload/spike/client/ (same-origin -> no CORS)
            rel = path.decode("utf-8", "replace").split("?", 1)[0].lstrip("/") or "index.html"
            fp = os.path.normpath(os.path.join(_CLIENT_DIR, rel))
            if (fp == _CLIENT_DIR or fp.startswith(_CLIENT_DIR + os.sep)) and os.path.isfile(fp):
                ext = fp.rsplit(".", 1)[-1].lower() if "." in os.path.basename(fp) else ""
                with open(fp, "rb") as f:
                    return _CTYPES.get(ext, "application/octet-stream").encode(), f.read()
        except Exception:
            return b"application/json", json.dumps({"error": traceback.format_exc()}).encode()

        return b"application/json", b'{"error":"not found"}'


# ─── Start ─────────────────────────────────────────────────────────────────────

def startSceneExport(port=2027, ws_port=2028, enable_keyhole=True, enable_mcp=False, logMessage=None):
    """Start the scene-export server. Returns the WebServerLogic; stop with .stop().

    ws_port: the offload control WebSocket port (2028 in prod; the DM-debug harness uses a free pair, e.g.
             port=2030/ws_port=2031, so it doesn't collide with the Colima container's forwarded 2027/2028).
    enable_keyhole: prod (over-video) wants the server's own 3D/slice render suppressed + magenta-keyed; the
             standalone DM harness sets False so it does NOT paint over THIS Slicer's own views."""
    log = logMessage or (lambda *a, **k: None)
    handlers = [SceneExportHandler(logMessage=log)]
    if enable_mcp:                                # dev: co-mount the MCP on THIS server (one WebServerLogic; a 2nd
        try:                                      # WebServerLogic instance doesn't bind reliably). MCP wins /mcp (0.9>0.5).
            import slicer_mcp_server
            handlers.append(slicer_mcp_server.MCPRequestHandler(autoAllow=True, logMessage=log))
            print("offload: MCP handler co-mounted at /mcp", flush=True)
        except Exception:
            print("offload: MCP mount FAILED:", traceback.format_exc(), flush=True)
    logic = WebServer.WebServerLogic(
        port=port,
        logMessage=log,
        enableSlicer=False,
        enableExec=False,
        enableStaticPages=False,
        enableDICOM=False,
        enableCORS=True,                          # so the standalone client page can fetch us
        requestHandlers=handlers,
    )
    logic.start()
    global _ws
    try:
        _ws = WSServer(ws_port, _ws_on_message, on_open=_ws_on_open)   # event channel (push scene/viewport, receive camera)
        _setup_ws_events()
    except Exception:
        print("offload WS failed:", traceback.format_exc(), flush=True)
    # Enable the keyhole by default once the 3D view + content exist (offload's whole point: the client
    # renders the 3D, so the server should not also show it). The client can toggle it via {t:keyhole}.
    def _enable_keyhole():
        try:
            set_keyhole(0, True)
            set_slice_keyhole(True)                   # 2D slice views are client-resliced too
            print("offload: keyhole ON (server 3D + slice content hidden; client renders it)", flush=True)
        except Exception:
            print("offload keyhole enable failed:", traceback.format_exc(), flush=True)
    if enable_keyhole:
        try:
            import qt
            qt.QTimer.singleShot(8000, _enable_keyhole)
        except Exception:
            pass
    print(f"\n  Desktopia scene-export: http://localhost:{logic.port}/scene  (events: ws://localhost:{ws_port})")
    print("  Stop with: sceneLogic.stop()\n")
    return logic


if __name__ == "__main__":
    try:
        sceneLogic.stop()
    except NameError:
        pass
    sceneLogic = startSceneExport()
