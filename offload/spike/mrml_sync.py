"""MRML-level scene sync for the desktopia 3D-offload (replaces the VTK-render-window serialization).

The client keeps a mirror of the MRML nodes in the 3D view's *reference closure* and runs JS displayable
managers that translate node state -> vtk.js actors -- the same model Slicer uses (examine nodes by type,
follow references, observe). Show/hide etc. become semantic node attributes a DM applies, not fragile
VTK-call-list diffs.

This module is pure functions over the MRML scene; slicer_scene_export.py wires the /mrml + /blob routes
and the WS push. Big binary (polydata/imagedata) is content-addressed by md5 (the client fetches /blob?hash).
Serialization is deliberately TARGETED per node class for the types the demo uses (model, scalar volume +
GPU volume rendering, segmentation, plus display/transform/camera/view); unknown classes fall back to a
generic id/class/name/refs record so the closure is still complete. Extending = add a _ser_<class> case.
"""

import gzip
import hashlib

import numpy
import slicer
import vtk
from vtk.util import numpy_support as ns

# content-addressed blob cache: md5 -> gzip bytes. Persists for the session; client fetches by hash.
_blobs = {}

# Memo for the EXPENSIVE per-node geometry serialization, keyed by the source data object's MTime. A
# transfer-function edit modifies the volume PROPERTY, not the image data, so the image-data MTime is
# unchanged -> cache hit -> we skip re-running vtk_to_numpy + gzip on the 17 MB volume every /mrml (the
# cause of TF-edit lag). Invalidated automatically when the data actually changes (MTime bumps).
_geom_cache = {}


def _cached(key, data_obj, compute):
    mt = int(data_obj.GetMTime()) if data_obj is not None else -1
    c = _geom_cache.get(key)
    if c is not None and c[0] == mt:
        return c[1]
    val = compute()
    _geom_cache[key] = (mt, val)
    return val


def _blob(data):
    """Content-address RAW bytes (md5) but STORE them gzip-compressed (the client gunzips with the native
    DecompressionStream). Compression on the fly: level 1 is fast and still shrinks int16 volumes/meshes
    a lot. Hashing the raw bytes keeps dedup independent of compression."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    md5 = hashlib.md5(data).hexdigest()
    if md5 not in _blobs:
        _blobs[md5] = gzip.compress(data, 1)
    return md5


def get_blob(md5):
    return _blobs.get(md5)   # gzip bytes


def _arr(np_arr, comps):
    """A typed-array blob descriptor: raw bytes (content-hashed) + how to interpret them on the client.
    No XML -- the client builds vtk.js vtkPolyData/vtkImageData directly from these arrays."""
    np_arr = numpy.ascontiguousarray(np_arr)
    return {"hash": _blob(np_arr.tobytes()), "dtype": str(np_arr.dtype), "count": int(np_arr.size), "comps": comps}


def _pd_blobs(pd):
    """vtkPolyData -> {points, polys[, normals]} typed-array blobs (points xyz f32; polys legacy
    [n,i0,i1,..] u32; normals xyz f32). vtk.js: getPoints().setData / getPolys().setData."""
    if pd is None or pd.GetNumberOfPoints() == 0:
        return None
    b = {"points": _arr(ns.vtk_to_numpy(pd.GetPoints().GetData()).astype("float32"), 3)}
    legacy = vtk.vtkIdTypeArray()
    pd.GetPolys().ExportLegacyFormat(legacy)
    polys = ns.vtk_to_numpy(legacy).astype("uint32") if legacy.GetNumberOfTuples() else numpy.zeros(0, "uint32")
    b["polys"] = _arr(polys, 1)
    nrm = pd.GetPointData().GetNormals()
    if nrm is not None:
        b["normals"] = _arr(ns.vtk_to_numpy(nrm).astype("float32"), 3)
    return b


def _img_blobs(img):
    """vtkImageData -> ({scalars}, {dims, comps}). Scalars keep their native dtype (e.g. int16); the
    client builds a vtkImageData + vtkDataArray. Geometry (spacing/origin) comes from the IJK->RAS attr."""
    if img is None:
        return None, None
    sc = img.GetPointData().GetScalars()
    comps = sc.GetNumberOfComponents()
    return {"scalars": _arr(ns.vtk_to_numpy(sc), comps)}, {"dims": list(img.GetDimensions()), "comps": comps}


def _matrix4x4(m):
    return [m.GetElement(r, c) for r in range(4) for c in range(4)] if m is not None else None


def _node_refs(node):
    """Generic MRML node references by role -> the MRML-native 'what relates to me' graph edges. Plus the
    transform and display convenience references."""
    refs = {}
    try:
        for i in range(node.GetNumberOfNodeReferenceRoles()):
            role = node.GetNthNodeReferenceRole(i)
            ids = [node.GetNthNodeReferenceID(role, j) for j in range(node.GetNumberOfNodeReferences(role))]
            ids = [x for x in ids if x]
            if ids:
                refs[role] = ids
    except Exception:
        pass
    if node.IsA("vtkMRMLTransformableNode") and node.GetTransformNodeID():
        refs.setdefault("transform", []).append(node.GetTransformNodeID())
    if node.IsA("vtkMRMLDisplayableNode"):
        dids = [node.GetNthDisplayNodeID(i) for i in range(node.GetNumberOfDisplayNodes())]
        dids = [x for x in dids if x]
        if dids:
            refs["display"] = dids
    return refs


def _tf_points(fn, keep):
    """vtkColorTransferFunction / vtkPiecewiseFunction -> list of node tuples for the client. VTK's
    GetNodeValue fills a fixed-size array (color: [x,r,g,b,mid,sharp]; piecewise: [x,y,mid,sharp]); we
    keep the first `keep` (4 = x,r,g,b for color; 2 = x,y for opacity)."""
    if fn is None:
        return None
    full = 6 if keep >= 4 else 4
    out = []
    for i in range(fn.GetSize()):
        val = [0.0] * full
        fn.GetNodeValue(i, val)
        out.append([val[k] for k in range(keep)])
    return out


def _segments(seg):
    out = []
    if seg is not None:
        seg.CreateRepresentation("Closed surface")
        for sid in [seg.GetNthSegmentID(i) for i in range(seg.GetNumberOfSegments())]:
            s = seg.GetSegment(sid)
            rep = s.GetRepresentation("Closed surface")
            pd = rep if isinstance(rep, vtk.vtkPolyData) else None
            out.append({"id": sid, "name": s.GetName(), "color": list(s.GetColor()), "mesh": _pd_blobs(pd)})
    return out


def _seg_labelmap(node):
    """A segmentation's merged binary labelmap -> a compact 3D blob (each voxel = the segment's label value)
    for client-side 2D slice OVERLAY (the client reslices it like the bg volume, maps label->segment color).
    Cropped to the nonzero bounding box so a sparse segment is a small texture (not the whole reference grid).
    Updates on segment-editor edits (the segmentation MTime changes -> _cached refreshes -> re-shipped)."""
    seg = node.GetSegmentation()
    if seg is None or seg.GetNumberOfSegments() == 0:
        return None
    if not seg.ContainsRepresentation("Binary labelmap"):   # closed-surface-only seg: convert (needs ref geometry)
        try:
            vols = slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
            if vols:
                node.SetReferenceImageGeometryParameterFromVolumeNode(vols[0])
            seg.CreateRepresentation("Binary labelmap")
        except Exception:
            pass
    merged = slicer.vtkOrientedImageData()
    if not node.GenerateMergedLabelmapForAllSegments(merged, 0, None):   # 0 = EXTENT_UNION_OF_SEGMENTS
        return None
    sc = merged.GetPointData().GetScalars()
    if sc is None:
        return None
    dx, dy, dz = merged.GetDimensions()
    arr = ns.vtk_to_numpy(sc).reshape(dz, dy, dx)                        # (k,j,i), x-fastest == VTK order
    nz = numpy.argwhere(arr > 0)
    if nz.size == 0:
        return None
    (kmin, jmin, imin), (kmax, jmax, imax) = nz.min(0), nz.max(0) + 1
    sub = numpy.ascontiguousarray(arr[kmin:kmax, jmin:jmax, imin:imax])  # crop to the segment bbox
    m = vtk.vtkMatrix4x4(); merged.GetImageToWorldMatrix(m)              # IJK -> RAS for the labelmap
    for r in range(3):                                                  # shift origin to the cropped corner
        m.SetElement(r, 3, m.GetElement(r, 0) * imin + m.GetElement(r, 1) * jmin + m.GetElement(r, 2) * kmin + m.GetElement(r, 3))
    colors = []
    for sid in [seg.GetNthSegmentID(i) for i in range(seg.GetNumberOfSegments())]:
        s = seg.GetSegment(sid); c = s.GetColor()
        colors.append([int(s.GetLabelValue()), c[0], c[1], c[2]])
    dn = node.GetDisplayNode()
    op = dn.GetOpacity2DFill() if dn is not None and hasattr(dn, "GetOpacity2DFill") else 0.5
    return {"blob": _arr(sub.astype("float32"), 1), "dims": [int(imax - imin), int(jmax - jmin), int(kmax - kmin)],
            "ijkToRAS": _matrix4x4(m), "colors": colors, "opacity": op}


def serialize_node(node):
    """One MRML node -> a JSON-able state dict: {id, class, name, refs, attrs, blobs}."""
    state = {
        "id": node.GetID(),
        "class": node.GetClassName(),
        "name": node.GetName(),
        "refs": _node_refs(node),
        "attrs": {},
        "blobs": {},
    }
    a, b = state["attrs"], state["blobs"]
    cls = node.GetClassName()

    if node.IsA("vtkMRMLDisplayNode"):
        a["visibility"] = node.GetVisibility()
        a["visibility3D"] = node.GetVisibility3D()
        a["color"] = list(node.GetColor())
        a["opacity"] = node.GetOpacity()
        a["ambient"] = node.GetAmbient(); a["diffuse"] = node.GetDiffuse(); a["specular"] = node.GetSpecular()
        a["edgeVisibility"] = node.GetEdgeVisibility()
        a["representation"] = node.GetRepresentation()   # 0 points,1 wireframe,2 surface
        if node.IsA("vtkMRMLMarkupsDisplayNode"):        # markups render in SELECTED color (points default to selected)
            a["selectedColor"] = list(node.GetSelectedColor())   # the color Slicer actually draws (not GetColor)
            a["activeColor"] = list(node.GetActiveColor())       # hover/active glyph color
            a["glyphScale"] = node.GetGlyphScale()               # screen-relative glyph size (% when useGlyphScale)
            a["glyphSize"] = node.GetGlyphSize()                 # absolute glyph size (mm) when !useGlyphScale
            a["useGlyphScale"] = bool(node.GetUseGlyphScale())
            a["lineThickness"] = node.GetLineThickness()
            a["textScale"] = node.GetTextScale()
            a["pointLabelsVisibility"] = bool(node.GetPointLabelsVisibility())      # per-control-point name labels
            a["propertiesLabelVisibility"] = bool(node.GetPropertiesLabelVisibility())  # one name+measurements label
        if node.IsA("vtkMRMLTransformDisplayNode"):      # the interaction widget toggle (linear transform editing)
            a["editorVisibility"] = bool(node.GetEditorVisibility())
        if node.IsA("vtkMRMLScalarVolumeDisplayNode"):   # grayscale window/level for 2D slice rendering
            a["window"] = node.GetWindow(); a["level"] = node.GetLevel()
        if node.IsA("vtkMRMLVolumeRenderingDisplayNode"):
            a["kind"] = "volumeRendering"
            a["croppingEnabled"] = node.GetCroppingEnabled()   # ROI is followed via the "roi" reference
        if node.IsA("vtkMRMLModelDisplayNode") and node.GetScalarVisibility():
            a["scalarVisibility"] = True
            a["activeScalarName"] = node.GetActiveScalarName()
            rng = list(node.GetScalarRange())
            a["scalarRange"] = rng
            cnode = node.GetColorNode()
            lut = cnode.GetLookupTable() if cnode is not None else None
            if lut is not None:                            # stretch the LUT across the display scalar range
                lr = lut.GetRange(); lo, hi = rng[0], rng[1]
                N = 64; tf = []
                for i in range(N):
                    t = i / (N - 1)
                    c = [0.0, 0.0, 0.0]; lut.GetColor(lr[0] + (lr[1] - lr[0]) * t, c)
                    tf.append([lo + (hi - lo) * t, c[0], c[1], c[2]])
                a["colorTF"] = tf

    if node.IsA("vtkMRMLModelNode"):
        pd = node.GetPolyData()
        pdb = _cached(node.GetID() + ":pd", pd, lambda: _pd_blobs(pd))
        if pdb:
            b.update(pdb)                                  # points / polys / normals
        dn = node.GetDisplayNode()                         # ship the active scalar array for scalar coloring
        if dn is not None and dn.IsA("vtkMRMLModelDisplayNode") and dn.GetScalarVisibility() and pd is not None:
            name = dn.GetActiveScalarName()
            arr = pd.GetPointData().GetArray(name) if name else None
            if arr is not None:
                b["scalars"] = _arr(ns.vtk_to_numpy(arr).astype("float32"), arr.GetNumberOfComponents())

    elif node.IsA("vtkMRMLScalarVolumeNode"):
        img = node.GetImageData()
        scb, meta = _cached(node.GetID() + ":img", img, lambda: _img_blobs(img))
        if scb:
            b.update(scb)                                  # scalars (memoized: not re-gzipped per TF edit)
            a.update(meta)                                 # dims, comps
        m = vtk.vtkMatrix4x4(); node.GetIJKToRASMatrix(m)
        a["ijkToRAS"] = _matrix4x4(m)

    elif node.IsA("vtkMRMLVolumePropertyNode"):
        vp = node.GetVolumeProperty()
        if vp is not None:
            a["shade"] = vp.GetShade()
            a["interpolationType"] = vp.GetInterpolationType()
            a["color"] = _tf_points(vp.GetRGBTransferFunction(), 4)        # x, r, g, b
            a["scalarOpacity"] = _tf_points(vp.GetScalarOpacity(), 2)      # x, a
            if vp.GetDisableGradientOpacity() == 0:
                a["gradientOpacity"] = _tf_points(vp.GetGradientOpacity(), 2)

    elif node.IsA("vtkMRMLSegmentationNode"):
        seg = node.GetSegmentation()
        a["segments"] = _cached(node.GetID() + ":seg", seg, lambda: _segments(seg))   # closed-surface (3D)
        lm = _cached(node.GetID() + ":labelmap", seg, lambda: _seg_labelmap(node))     # merged labelmap (2D slices)
        if lm:
            b["labelmap"] = lm["blob"]
            a["labelmapDims"] = lm["dims"]; a["labelmapIjkToRAS"] = lm["ijkToRAS"]
            a["segmentColors"] = lm["colors"]; a["seg2DOpacity"] = lm["opacity"]

    elif node.IsA("vtkMRMLMarkupsROINode"):
        # oriented box for volume cropping: world center + 3 unit axes + half-sizes (client -> 6 clip planes)
        o2w = node.GetObjectToWorldMatrix()
        a["center"] = [o2w.GetElement(r, 3) for r in range(3)]
        axes = []
        for c in range(3):
            v = [o2w.GetElement(r, c) for r in range(3)]
            n = (sum(x * x for x in v)) ** 0.5 or 1.0
            axes.append([x / n for x in v])
        a["axes"] = axes
        a["halfSizes"] = [s / 2.0 for s in node.GetSize()]

    elif node.IsA("vtkMRMLAnnotationROINode"):
        a["center"] = list(node.GetXYZ())
        a["halfSizes"] = list(node.GetRadiusXYZ())
        a["axes"] = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]

    elif node.IsA("vtkMRMLMarkupsNode"):                   # fiducials / line / angle / curve (ROI handled above)
        try:
            arr = slicer.util.arrayFromMarkupsControlPoints(node, world=True)
        except TypeError:
            arr = slicer.util.arrayFromMarkupsControlPoints(node)
        a["controlPoints"] = arr.tolist() if arr is not None else []
        a["pointLabels"] = [node.GetNthControlPointLabel(i) for i in range(node.GetNumberOfControlPoints())]
        a["selectedFlags"] = [bool(node.GetNthControlPointSelected(i)) for i in range(node.GetNumberOfControlPoints())]
        meas = []                                          # enabled measurements Slicer prints in the properties label
        for i in range(node.GetNumberOfMeasurements()):
            mm = node.GetNthMeasurement(i)
            if mm is None or not mm.GetEnabled():
                continue
            val = ""
            try:                                           # Slicer's own print format applied to the value -> "144.6mm"
                fmt = mm.GetPrintFormat()
                if fmt and mm.GetValue() is not None:
                    val = fmt % mm.GetValue()
            except Exception:
                pass
            if not val:
                try:
                    val = ("%.1f %s" % (mm.GetValue(), mm.GetUnits() or "")).strip()
                except Exception:
                    pass
            meas.append({"name": mm.GetName(), "value": val})
        a["measurements"] = meas
        a["closed"] = bool(node.IsA("vtkMRMLMarkupsClosedCurveNode"))
        a["connect"] = bool(node.IsA("vtkMRMLMarkupsLineNode") or node.IsA("vtkMRMLMarkupsAngleNode")
                            or node.IsA("vtkMRMLMarkupsCurveNode"))
        if node.IsA("vtkMRMLMarkupsCurveNode"):           # the interpolated (spline) line for curves
            cp = node.GetCurvePointsWorld()
            if cp is not None and cp.GetNumberOfPoints() > 1:
                a["linePoints"] = [list(cp.GetPoint(i)) for i in range(cp.GetNumberOfPoints())]

    elif node.IsA("vtkMRMLTransformNode"):
        m = vtk.vtkMatrix4x4()
        node.GetMatrixTransformToParent(m)
        a["matrixToParent"] = _matrix4x4(m)
        a["linear"] = bool(node.IsLinear())
        if node.IsLinear():                            # interaction-widget geometry (linear only -- see TODO/vtk.js)
            a["widgetCenter"] = [m.GetElement(r, 3) for r in range(3)]   # where the origin maps (widget sits here)
            axes = []
            for c in range(3):
                v = [m.GetElement(r, c) for r in range(3)]
                nrm = (sum(x * x for x in v)) ** 0.5 or 1.0
                axes.append([x / nrm for x in v])
            a["axes"] = axes                            # the transform's local axes (= world for identity rotation)

    elif node.IsA("vtkMRMLSliceNode"):                     # a 2D slice view: the plane + viewport sizing
        a["layoutName"] = node.GetLayoutName()             # Red / Yellow / Green
        a["xyToRAS"] = _matrix4x4(node.GetXYToRAS())       # slice-viewport pixel (x,y,0,1) -> RAS (client reslices with this)
        a["dimensions"] = list(node.GetDimensions())       # viewport pixel size
        a["fieldOfView"] = list(node.GetFieldOfView())     # mm extent (changes on zoom)
        a["sliceToRAS"] = _matrix4x4(node.GetSliceToRAS())
        a["orientation"] = node.GetOrientation()

    elif node.IsA("vtkMRMLSliceCompositeNode"):            # which volumes fill this slice view
        a["layoutName"] = node.GetLayoutName()
        a["backgroundVolumeID"] = node.GetBackgroundVolumeID()
        a["foregroundVolumeID"] = node.GetForegroundVolumeID()
        a["labelVolumeID"] = node.GetLabelVolumeID()
        a["foregroundOpacity"] = node.GetForegroundOpacity()
        a["labelOpacity"] = node.GetLabelOpacity()

    elif node.IsA("vtkMRMLCameraNode"):
        c = node.GetCamera()
        a["position"] = list(c.GetPosition()); a["focalPoint"] = list(c.GetFocalPoint())
        a["viewUp"] = list(c.GetViewUp()); a["viewAngle"] = c.GetViewAngle()
        a["parallelProjection"] = c.GetParallelProjection(); a["parallelScale"] = c.GetParallelScale()

    elif node.IsA("vtkMRMLViewNode"):
        a["backgroundColor"] = list(node.GetBackgroundColor())
        a["backgroundColor2"] = list(node.GetBackgroundColor2())
        a["boxVisible"] = node.GetBoxVisible()
        a["axisLabelsVisible"] = node.GetAxisLabelsVisible()
        a["orientationMarkerType"] = node.GetOrientationMarkerType()   # 0 none,1 cube,2 human,3 axes

    return state


def _add(closure, node):
    if node is not None and node.GetID() not in closure:
        closure[node.GetID()] = node


def view_closure(view_index=0):
    """The MRML reference closure for 3D view `view_index`: the nodes a renderer for this view needs.
    Mirrors how a displayable manager decides what to draw -- view + camera, then every data node that has
    a display node visible IN THIS VIEW, plus that data node's display/transform/property/color references."""
    lm = slicer.app.layoutManager()
    widget = lm.threeDWidget(view_index)
    viewNode = widget.mrmlViewNode()
    viewId = viewNode.GetID()
    closure = {}
    _add(closure, viewNode)
    try:
        _add(closure, slicer.modules.cameras.logic().GetViewActiveCameraNode(viewNode))
    except Exception:
        pass

    def consider(dataNode):
        # Include the data node whenever it has a display node FOR THIS VIEW -- regardless of visibility.
        # The client DM keeps the actor and applies visibility as a pure attribute toggle (deterministic
        # show/hide), instead of the node entering/leaving the closure. (A hidden node still ships its
        # geometry; fine for now, optimize later by deferring blobs for hidden nodes.)
        relevant = False
        for i in range(dataNode.GetNumberOfDisplayNodes()):
            dn = dataNode.GetNthDisplayNode(i)
            if dn is None:
                continue
            inView = dn.IsDisplayableInView(viewId) if hasattr(dn, "IsDisplayableInView") else True
            if inView:
                relevant = True
                _add(closure, dn)
                # follow the display node's own references (e.g. VR display -> volume property, color)
                for ids in _node_refs(dn).values():
                    for nid in ids:
                        _add(closure, slicer.mrmlScene.GetNodeByID(nid))
        if relevant:
            _add(closure, dataNode)
            tid = dataNode.GetTransformNodeID() if dataNode.IsA("vtkMRMLTransformableNode") else None
            if tid:
                tnode = slicer.mrmlScene.GetNodeByID(tid)
                _add(closure, tnode)
                if tnode is not None:                  # + the transform's display node (carries the interaction-widget flag)
                    for i in range(tnode.GetNumberOfDisplayNodes()):
                        _add(closure, tnode.GetNthDisplayNode(i))

    for cls in ("vtkMRMLModelNode", "vtkMRMLScalarVolumeNode", "vtkMRMLSegmentationNode",
                "vtkMRMLMarkupsNode"):                     # markups (fiducials/line/curve/ROI) shown in 3D
        for n in slicer.util.getNodesByClass(cls):
            consider(n)
    return closure


def slice_closure(closure):
    """Add the 2D slice views' reference closure: every slice node + its composite node + the referenced
    background/foreground/label volumes (+ their display nodes for window/level/LUT). The client reslices
    those volumes on the slice plane (xyToRAS) -- the 2D analog of the 3D displayable-manager closure."""
    for cn in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
        _add(closure, cn)
        for vid in (cn.GetBackgroundVolumeID(), cn.GetForegroundVolumeID(), cn.GetLabelVolumeID()):
            vol = slicer.mrmlScene.GetNodeByID(vid) if vid else None
            if vol is not None:
                _add(closure, vol)
                for i in range(vol.GetNumberOfDisplayNodes()):
                    _add(closure, vol.GetNthDisplayNode(i))
    for sn in slicer.util.getNodesByClass("vtkMRMLSliceNode"):
        _add(closure, sn)
    return closure


def mrml_state(view_index=0):
    """Full closure as {id: node-state} -- the initial snapshot a client pulls; deltas come over the WS.
    Includes BOTH the 3D view closure and the 2D slice views' closure (so one /mrml pull mirrors all views)."""
    closure = slice_closure(view_closure(view_index))
    return {nid: serialize_node(node) for nid, node in closure.items()}
