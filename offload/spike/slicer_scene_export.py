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

import json
import math
import os
import traceback
import urllib

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


# Keyhole: the server hides the 3D content (so the streamed video shows a flat key color where the 3D
# view is) while the CLIENT renders it. So the content must still SERIALIZE even though it's hidden on
# the server -- wrap the actor/volume serializers to force visibility during serialization and emit
# visibility=1, regardless of the server-side (keyhole) visibility.
def _force_visible(base):
    def wrapped(parent, actor, actorId, context, depth):
        v = actor.GetVisibility()
        if not v:
            actor.SetVisibility(1)
        try:
            st = base(parent, actor, actorId, context, depth)
        finally:
            if not v:
                actor.SetVisibility(v)
        if isinstance(st, dict):
            st.setdefault("properties", {})["visibility"] = 1
        return st
    return wrapped

for _cls, _base in (("vtkVolume", rws.genericVolumeSerializer),
                    ("vtkActor", rws.genericActorSerializer),
                    ("vtkOpenGLActor", rws.genericActorSerializer)):
    try:
        rws.registerInstanceSerializer(_cls, _force_visible(_base))
    except Exception as _ex:
        print("offload: could not wrap serializer", _cls, _ex, flush=True)

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


def export_scene_state(view_index=0):
    """Serialize 3D view `view_index`'s render window to the vtk.js synchronizable state dict."""
    global _context
    rw = _three_d_render_window(view_index)
    rw.Render()                                   # make sure displayable managers have built actors
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
    if widget is None:
        raise RuntimeError(f"No 3D widget at index {view_index}")
    tdv = widget.threeDView()
    tl = tdv.mapToGlobal(qt.QPoint(0, 0))
    return {"x": tl.x(), "y": tl.y(), "w": tdv.width, "h": tdv.height}


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


def get_camera(view_index=0):
    """Read back Slicer's current 3D camera (for verifying camera sync)."""
    widget = slicer.app.layoutManager().threeDWidget(view_index)
    camNode = slicer.modules.cameras.logic().GetViewActiveCameraNode(widget.mrmlViewNode())
    c = camNode.GetCamera()
    return {"position": list(c.GetPosition()), "focalPoint": list(c.GetFocalPoint()),
            "viewUp": list(c.GetViewUp()), "parallelScale": c.GetParallelScale()}


def set_keyhole(view_index, on):
    """Hide the 3D content on the SERVER (the client renders it locally) so the server stops ray-casting
    the volume -- the CPU win. The client overlay covers the now-content-free 3D view region, which keeps
    the SAME (synced) background, so it looks identical. force_visible keeps the hidden content in the
    serialized state so the client still renders it. (A flat key color for popup-perfect chroma-key
    compositing is the next step.)"""
    for cls in ("vtkMRMLVolumeRenderingDisplayNode", "vtkMRMLModelDisplayNode"):
        for dn in slicer.util.getNodesByClass(cls):
            dn.SetVisibility(0 if on else 1)               # hidden on server; force_visible re-adds to the state
    slicer.app.layoutManager().threeDWidget(view_index).threeDView().forceRender()
    return {"ok": True, "keyhole": bool(on)}


def get_array_bytes(md5):
    """Raw bytes for a data array the client requested by hash (from the live context)."""
    if _context is None:
        return None
    return _context.getCachedDataArray(md5, binary=True)


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
            if path == b"/scene":
                qs = urllib.parse.parse_qs(parsed.query)
                raw = (qs.get(b"view") or qs.get("view") or [b"0"])[0]
                view = int(raw.decode() if isinstance(raw, bytes) else raw)
                state = export_scene_state(view)
                return b"application/json", json.dumps(state).encode()

            if path == b"/array":
                qs = urllib.parse.parse_qs(parsed.query)
                raw = (qs.get(b"hash") or qs.get("hash") or [b""])[0]
                md5 = raw.decode() if isinstance(raw, bytes) else raw
                data = get_array_bytes(md5)
                if data is None:
                    return b"application/json", b'{"error":"unknown hash; GET /scene first"}'
                return b"application/octet-stream", bytes(data)

            if path == b"/viewport":
                qs = urllib.parse.parse_qs(parsed.query)
                raw = (qs.get(b"view") or qs.get("view") or [b"0"])[0]
                view = int(raw.decode() if isinstance(raw, bytes) else raw)
                return b"application/json", json.dumps(viewport_rect(view)).encode()

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

def startSceneExport(port=2027, logMessage=None):
    """Start the scene-export server. Returns the WebServerLogic; stop with .stop()."""
    log = logMessage or (lambda *a, **k: None)
    logic = WebServer.WebServerLogic(
        port=port,
        logMessage=log,
        enableSlicer=False,
        enableExec=False,
        enableStaticPages=False,
        enableDICOM=False,
        enableCORS=True,                          # so the standalone client page can fetch us
        requestHandlers=[SceneExportHandler(logMessage=log)],
    )
    logic.start()
    print(f"\n  Desktopia scene-export: http://localhost:{logic.port}/scene")
    print(f"  Open offload/spike/client/index.html and point it at http://localhost:{logic.port}")
    print("  Stop with: sceneLogic.stop()\n")
    return logic


if __name__ == "__main__":
    try:
        sceneLogic.stop()
    except NameError:
        pass
    sceneLogic = startSceneExport()
