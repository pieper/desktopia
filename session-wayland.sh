#!/usr/bin/env bash
# Desktopia desktop session — runs as the unprivileged 'user' (started by entrypoint-wayland.sh).
# Brings up the GPU Wayland compositor + H.264/QUIC server, then Xwayland + openbox + Slicer.
set -uo pipefail
cd "$(dirname "$0")"

# Force HOME so every app (Slicer settings, pcmanfm, xterm, ~/Data) uses /home/user, not /root.
# The base's `user` acct + sudo -H proved unreliable here. (cwd stays the script dir for server.py.)
export HOME=/home/user

# --- capture source + screen geometry (env-overridable; the CPU/Colab path passes small values) ---
# DESKTOPIA_SOURCE=wayland|xvfb|auto. auto = GPU compositor when a DRM render node exists, else
# software Xvfb (no GPU). Resolution/rate flow to BOTH the X/compositor screen AND server.py.
SRC=${DESKTOPIA_SOURCE:-auto}
if [ "$SRC" = auto ]; then
  if ls /dev/dri/renderD* >/dev/null 2>&1; then SRC=wayland; else SRC=xvfb; fi
fi
SCR_W=${DESKTOPIA_WIDTH:-1920}; SCR_H=${DESKTOPIA_HEIGHT:-1080}
SCR_FPS=${DESKTOPIA_FPS:-60};   SCR_BR=${DESKTOPIA_BITRATE:-12000}

export GST_REGISTRY_FORK=no
export XDG_RUNTIME_DIR=/tmp/wl-rt-$(id -u); mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
SOCK="$XDG_RUNTIME_DIR/wayland-1"
SLICER_DIR=$(ls -d /opt/Slicer-*/ 2>/dev/null | head -1)
CERT=${DESKTOPIA_CERT:-/home/user/desktopia-cert.pem}
KEY=${DESKTOPIA_KEY:-/home/user/desktopia-key.pem}

cleanup() { kill $(jobs -p) 2>/dev/null; }
trap cleanup EXIT INT TERM

# NRP/appliance/Colab (DESKTOPIA_SERVE_PAGE): server.py serves the client page over HTTP on the SAME
# port as the WebSocket, so the ingress needs only one path '/' and the backend answers its HTTP health
# probes. status.json tells the page to use the websocket transport.
if [ -n "${DESKTOPIA_SERVE_PAGE:-}" ]; then
  printf '{"ready":true,"transport":"websocket"}' > client/status.json 2>/dev/null || true
fi

SRV_ARGS="--source $SRC --width $SCR_W --height $SCR_H --fps $SCR_FPS --bitrate $SCR_BR"
echo "source=$SRC  geometry=${SCR_W}x${SCR_H}@${SCR_FPS}  bitrate=${SCR_BR}k"

if [ "$SRC" = wayland ]; then
  # --- GPU path: server.py's waylanddisplaysrc IS the compositor, so it must come up FIRST (creating
  # the WL socket) before Xwayland connects as its client. Compositor-side env (NOT the NVIDIA GBM env
  # below — that is for the X clients only). ---
  export LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}   # libwayland 1.25
  export GST_PLUGIN_PATH=/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0:${GST_PLUGIN_PATH:-}
  export WAYLAND_DISPLAY=wayland-1
  rm -f "$SOCK"
  python3 server.py --cert "$CERT" --key "$KEY" --port 4433 $SRV_ARGS \
    ${DESKTOPIA_WS_PLAIN:+--ws-plain} ${DESKTOPIA_SERVE_PAGE:+--serve-dir client} >/tmp/server.log 2>&1 &
  for i in $(seq 1 100); do [ -S "$SOCK" ] && break; sleep 0.25; done
  if [ ! -S "$SOCK" ]; then echo "FAIL: compositor socket never appeared. server.log:"; tail -n 40 /tmp/server.log; exit 1; fi
  echo "compositor + stream server up as $(whoami) (socket $SOCK)"

  Xwayland :2 -geometry ${SCR_W}x${SCR_H} >/tmp/xway.log 2>&1 &   # Xwayland IS the compositor's WL client
  for i in $(seq 1 40); do [ -e /tmp/.X11-unix/X2 ] && break; sleep 0.25; done
  # Pure X11 clients of :2 from here. Unset WAYLAND_DISPLAY so X11 apps (Slicer, Chrome, esp. wgpu's
  # EGL backend) don't try the Wayland platform and crash (wl_drm BadAccess).
  unset WAYLAND_DISPLAY
  # --- X-client env for HARDWARE GL via the NVIDIA driver, inherited by openbox + every app ---
  export GBM_BACKEND=nvidia-drm
  export __GLX_VENDOR_LIBRARY_NAME=nvidia
  export __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json
else
  # --- software path: a plain Xvfb framebuffer (no GPU, no DRM node). Slicer renders with Mesa
  # llvmpipe. Xvfb must come up FIRST so server.py's ximagesrc can capture it. ---
  export LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe
  rm -f /tmp/.X2-lock /tmp/.X11-unix/X2 2>/dev/null || true   # clear a stale lock so Xvfb can reclaim :2 on restart
  Xvfb :2 -screen 0 ${SCR_W}x${SCR_H}x24 +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
  for i in $(seq 1 80); do [ -e /tmp/.X11-unix/X2 ] && break; sleep 0.25; done
  if [ ! -e /tmp/.X11-unix/X2 ]; then echo "FAIL: Xvfb :2 never appeared. xvfb.log:"; tail -n 40 /tmp/xvfb.log; exit 1; fi
  echo "Xvfb (software) up as $(whoami) on :2 (${SCR_W}x${SCR_H})"

  python3 server.py --cert "$CERT" --key "$KEY" --port 4433 $SRV_ARGS \
    ${DESKTOPIA_WS_PLAIN:+--ws-plain} ${DESKTOPIA_SERVE_PAGE:+--serve-dir client} >/tmp/server.log 2>&1 &
  sleep 1
  echo "ximagesrc stream server up as $(whoami) (capturing :2)"
fi

# --- common X-client environment, inherited by openbox AND every app it launches ---
[ -f "${DESKTOPIA_PRELOAD:-}" ] && export LD_PRELOAD="$DESKTOPIA_PRELOAD"   # close_range g_spawn fix
export DISPLAY=:2
ulimit -n 65536 2>/dev/null || true

# --- user folders for downloads / data to drag into Slicer (HOME is /home/user) ---
mkdir -p ~/Data ~/Downloads

# --- 3D-offload (DESKTOPIA_OFFLOAD): a ~/.slicerrc.py that, once Slicer is up, starts the scene-export
# server (serves the 3D view's VTK scene at :2027 for a browser client to render on the client GPU) and
# loads a demo volume so there's something to render. Spike path; serializer is vendored beside it. ---
if [ -n "${DESKTOPIA_OFFLOAD:-}" ]; then
  cat > ~/.slicerrc.py <<PY
import sys, qt
sys.path.insert(0, "$PWD/offload/spike")
def _offload_start():
    try:
        import slicer_scene_export as _e; _e.startSceneExport()
        print("DESKTOPIA offload: scene-export on :2027")
    except Exception as ex:
        print("DESKTOPIA offload: scene-export FAILED:", ex)
    try:
        import SampleData
        v = SampleData.downloadSample("MRHead")
        vr = slicer.modules.volumerendering.logic()
        dn = vr.CreateDefaultVolumeRenderingNodes(v); dn.SetVisibility(True)
        slicer.util.resetThreeDViews()
        print("DESKTOPIA offload: demo volume (MRHead) loaded + volume rendering on")
        try:                                              # ROI crop, so the offload exercises cropping
            dn.SetCroppingEnabled(True)
            roi = dn.GetMarkupsROINode() if hasattr(dn, "GetMarkupsROINode") else None
            if roi is None and hasattr(dn, "GetROINode"): roi = dn.GetROINode()
            if roi is None:
                roi = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsROINode", "CropROI")
                dn.SetAndObserveROINodeID(roi.GetID())
            b = [0.0] * 6; v.GetRASBounds(b)
            roi.SetCenter((b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2)
            roi.SetSize((b[1] - b[0]) * 0.7, (b[3] - b[2]) * 0.7, (b[5] - b[4]) * 0.55)
            print("DESKTOPIA offload: volume cropping ROI enabled")
        except Exception as ex:
            print("DESKTOPIA offload: ROI crop FAILED:", ex)
    except Exception as ex:
        print("DESKTOPIA offload: demo data FAILED:", ex)
    try:
        import vtk
        # a surface MODEL (vtkActor + polydata) so the offload exercises models, not just the volume
        sph = vtk.vtkSphereSource(); sph.SetCenter(0, 0, 40); sph.SetRadius(45)
        sph.SetThetaResolution(48); sph.SetPhiResolution(48); sph.Update()
        m = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", "OffloadSphere")
        m.SetAndObservePolyData(sph.GetOutput()); m.CreateDefaultDisplayNodes()
        m.GetDisplayNode().SetColor(1.0, 0.6, 0.1); m.GetDisplayNode().SetOpacity(0.5)
        # a SEGMENTATION with a closed-surface segment (its own displayable-manager actors)
        s2 = vtk.vtkSphereSource(); s2.SetCenter(55, 0, 40); s2.SetRadius(28); s2.Update()
        seg = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "OffloadSeg")
        seg.CreateDefaultDisplayNodes()
        seg.GetSegmentation().SetMasterRepresentationName("Closed surface")
        seg.AddSegmentFromClosedSurfaceRepresentation(s2.GetOutput(), "blob", [0.2, 0.8, 0.3])
        print("DESKTOPIA offload: demo model + segmentation added")
    except Exception as ex:
        print("DESKTOPIA offload: demo model/seg FAILED:", ex)
    try:
        import vtk                                           # markups to exercise the general control-point handles
        F = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "OffloadPoints")
        for p in ([60, 40, 70], [-50, 30, 60], [10, -30, 90]): F.AddControlPoint(vtk.vtkVector3d(*p))
        L = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsLineNode", "OffloadLine")
        for p in ([-70, -20, 20], [70, 10, 40]): L.AddControlPoint(vtk.vtkVector3d(*p))
        Cv = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsCurveNode", "OffloadCurve")
        for p in ([-50, -60, 100], [0, -80, 100], [50, -60, 100], [50, 20, 100]): Cv.AddControlPoint(vtk.vtkVector3d(*p))
        print("DESKTOPIA offload: demo markups (points/line/curve) added")
    except Exception as ex:
        print("DESKTOPIA offload: demo markups FAILED:", ex)
    try:
        import vtk                                           # a transformed model to exercise transforms
        sph2 = vtk.vtkSphereSource(); sph2.SetRadius(22); sph2.SetThetaResolution(32); sph2.SetPhiResolution(32); sph2.Update()
        m2 = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", "OffloadXformed")
        m2.SetAndObservePolyData(sph2.GetOutput()); m2.CreateDefaultDisplayNodes(); m2.GetDisplayNode().SetColor(0.2, 0.6, 1.0)
        T = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", "OffloadXform")
        mat = vtk.vtkMatrix4x4(); mat.SetElement(0, 3, 95); mat.SetElement(1, 3, -75); mat.SetElement(2, 3, 105)
        T.SetMatrixTransformToParent(mat); m2.SetAndObserveTransformNodeID(T.GetID())
        print("DESKTOPIA offload: demo transformed model added")
    except Exception as ex:
        print("DESKTOPIA offload: demo transform FAILED:", ex)
    try:
        import vtk                                           # a scalar-colored model (elevation + rainbow LUT + scalar bar)
        sph3 = vtk.vtkSphereSource(); sph3.SetRadius(26); sph3.SetCenter(-95, 85, -105)
        sph3.SetThetaResolution(48); sph3.SetPhiResolution(48); sph3.Update()
        elev = vtk.vtkElevationFilter(); elev.SetInputConnection(sph3.GetOutputPort())
        elev.SetLowPoint(0, 59, 0); elev.SetHighPoint(0, 111, 0); elev.SetScalarRange(0.0, 1.0); elev.Update()
        m3 = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", "OffloadScalars")
        m3.SetAndObservePolyData(elev.GetOutput()); m3.CreateDefaultDisplayNodes()
        d3 = m3.GetDisplayNode()
        d3.SetActiveScalarName("Elevation"); d3.SetScalarVisibility(True)
        d3.SetAndObserveColorNodeID("vtkMRMLColorTableNodeRainbow")
        print("DESKTOPIA offload: demo scalar-colored model added")
    except Exception as ex:
        print("DESKTOPIA offload: demo scalar model FAILED:", ex)
qt.QTimer.singleShot(6000, _offload_start)
PY
fi

# --- openbox config: single desktop + no wheel desktop-switching (resources/openbox-rc.xml), so the
# scroll wheel only ever reaches the app (Slicer) instead of flipping workspaces ---
mkdir -p ~/.config/openbox
cp "$(dirname "$0")/resources/openbox-rc.xml" ~/.config/openbox/rc.xml 2>/dev/null || true

# --- openbox menu: Terminal, Files, Chrome, Slicer, WM settings (no exit) ---
cat > ~/.config/openbox/menu.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_menu xmlns="http://openbox.org/3.4/menu">
  <menu id="root-menu" label="Desktopia">
    <item label="Terminal"><action name="Execute"><command>xterm</command></action></item>
    <item label="Files"><action name="Execute"><command>pcmanfm /home/user</command></action></item>
    <item label="Google Chrome"><action name="Execute"><command>/home/user/desktopia/scripts/chrome-launch.sh</command></action></item>
    <item label="3D Slicer"><action name="Execute"><command>sh -c 'D=$(ls -d /opt/Slicer-*/ 2>/dev/null | head -1); exec "$D/Slicer" --no-splash'</command></action></item>
    <separator/>
    <item label="Window Manager Settings"><action name="Execute"><command>obconf</command></action></item>
  </menu>
</openbox_menu>
EOF

openbox >/tmp/wm.log 2>&1 &

# --- loading splash (pre-rendered in CI; the box only needs feh): the Slicer logo + "please wait"
# on the X root, shown the instant the desktop is up so a browser that connects sees branded content
# while Slicer is still downloading. We swap to the no-text background once Slicer launches. ---
SPLASH=/usr/local/share/desktopia/splash.png       # logo + "Loading 3D Slicer... please wait"
BG=/usr/local/share/desktopia/background.png        # logo only (steady wallpaper)
setbg() {
  if [ -f "$1" ] && command -v xwallpaper >/dev/null 2>&1; then xwallpaper --zoom "$1" 2>/dev/null
  else xsetroot -solid '#15151f' 2>/dev/null || true; fi
}
setbg "$SPLASH"

# --- launch Slicer as soon as its background download lands (over the splash); the desktop + stream
# are already live by now (the cert is printed below before this returns). Clear the "please wait"
# afterward. Falls back to glxgears if Slicer never arrives. ---
(
  for _ in $(seq 1 150); do
    SDIR=$(ls -d /opt/Slicer-*/ 2>/dev/null | head -1)
    [ -n "$SDIR" ] && [ -x "$SDIR/Slicer" ] && break
    sleep 2
  done
  if [ -n "${SDIR:-}" ] && [ -x "$SDIR/Slicer" ]; then
    "$SDIR/Slicer" --no-splash >/tmp/slicer.log 2>&1 &
    sleep 8; wmctrl -r :ACTIVE: -b add,maximized_vert,maximized_horz 2>/dev/null || true
    setbg "$BG"
  else
    glxgears >/tmp/glxgears.log 2>&1 &
  fi
) &

echo "=================================================================="
echo -n "CERT_SHA256_BASE64="
openssl x509 -in "$CERT" -outform der | openssl dgst -sha256 -binary | base64
echo "Now: 'make port' for the public IP:PORT, paste both into client/index.html, open in Chrome."
echo "(logs: /tmp/server.log /tmp/slicer.log /tmp/xway.log)"
echo "=================================================================="

wait
