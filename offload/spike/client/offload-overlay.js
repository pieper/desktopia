/*
 * Desktopia 3D-offload OVERLAY — loaded by the streamed-desktop client (:4434 page) from the scene
 * server (:2027). Floats a local vtk.js render of Slicer's 3D view over the video's 3D-view region, so
 * 3D interaction happens on the client GPU while the rest of the desktop keeps streaming as video.
 * Increment #1+#2: position/track the overlay over the viewport rect (no key-color hole yet).
 *
 * Bundled with vtk.js by build.sh -> offload-bundle.js (the standalone UMD is gone in vtk.js 36).
 */
import '@kitware/vtk.js/Rendering/Profiles/All';
import vtkSynchronizableRenderWindow from '@kitware/vtk.js/Rendering/Misc/SynchronizableRenderWindow';
import vtkOpenGLRenderWindow from '@kitware/vtk.js/Rendering/OpenGL/RenderWindow';
import vtkRenderWindowInteractor from '@kitware/vtk.js/Rendering/Core/RenderWindowInteractor';
import vtkInteractorStyleTrackballCamera from '@kitware/vtk.js/Interaction/Style/InteractorStyleTrackballCamera';

const SCENE = `http://${location.hostname}:2027`;   // the Slicer scene-export server
const VIEW = 0;

// overlay container, floated over the video's 3D-view region (above #v, below the top bar / control panel)
const host = document.createElement('div');
host.id = 'offload3d';
host.style.cssText = 'position:fixed; z-index:5; background:transparent; display:none; pointer-events:auto;';
document.body.appendChild(host);

const syncCtx = vtkSynchronizableRenderWindow.getSynchronizerContext('offload');
syncCtx.setFetchArrayFunction((h) => fetch(`${SCENE}/array?hash=${h}`).then((r) => r.arrayBuffer()));
const renderWindow = vtkSynchronizableRenderWindow.newInstance({ synchronizerContext: syncCtx });
const glWindow = vtkOpenGLRenderWindow.newInstance();
glWindow.setContainer(host);
renderWindow.addView(glWindow);
const interactor = vtkRenderWindowInteractor.newInstance();
interactor.setView(glWindow);
interactor.initialize();
interactor.setInteractorStyle(vtkInteractorStyleTrackballCamera.newInstance());
let bound = false;
let lastRect = null;     // last /viewport result (3D view rect in screen px + screen size)

// inverse of the client's toBitmap(): stream/screen px -> CSS px, honoring #v's object-fit: contain
function videoMap() {
  const v = document.getElementById('v');
  if (!v) return null;
  const r = v.getBoundingClientRect();
  const sw = v.width || 1600, sh = v.height || 1000;      // canvas internal size == stream resolution
  const scale = Math.min(r.width / sw, r.height / sh);     // contain
  return { left: r.left + (r.width - sw * scale) / 2, top: r.top + (r.height - sh * scale) / 2, scale, sw, sh };
}

function positionOverlay() {
  if (!lastRect) return;
  const m = videoMap();
  if (!m) return;
  const rx = m.sw / (lastRect.screenW || m.sw), ry = m.sh / (lastRect.screenH || m.sh);  // screen px -> stream px
  const x = lastRect.x * rx, y = lastRect.y * ry, w = lastRect.w * rx, h = lastRect.h * ry;
  const cw = Math.max(1, Math.round(w * m.scale)), ch = Math.max(1, Math.round(h * m.scale));
  host.style.left = (m.left + x * m.scale) + 'px';
  host.style.top = (m.top + y * m.scale) + 'px';
  host.style.width = cw + 'px';
  host.style.height = ch + 'px';
  host.style.display = 'block';
  glWindow.setSize(cw, ch);
  renderWindow.render();
}

async function refreshViewport() {
  try { lastRect = await fetch(`${SCENE}/viewport?view=${VIEW}`).then((r) => r.json()); positionOverlay(); }
  catch (e) { /* scene server not reachable -> leave the overlay hidden, desktop streams normally */ }
}

function mainRenderer() {
  const rs = renderWindow.getRenderers();
  return rs.find((r) => (r.getVolumes && r.getVolumes().length) || r.getActors().length) || rs[0];
}

// camera sync: push the LOCAL camera to Slicer's MRML camera (keeps the server consistent)
function postCamera() {
  const ren = mainRenderer();
  if (!ren) return;
  const c = ren.getActiveCamera();
  const body = JSON.stringify({
    position: c.getPosition(), focalPoint: c.getFocalPoint(), viewUp: c.getViewUp(),
    viewAngle: c.getViewAngle(), parallelScale: c.getParallelScale(),
    parallelProjection: c.getParallelProjection(),
  });
  fetch(`${SCENE}/camera?view=${VIEW}`, { method: 'POST', body }).catch(() => {});
}

// incremental/live: re-sync the scene, preserving the LOCAL camera so Slicer-side changes (transfer
// functions, new data) appear without snapping the view. synchronize() dedups arrays by hash, so an
// unchanged volume isn't re-downloaded -- only what changed.
async function reSync() {
  let state;
  try { state = await fetch(`${SCENE}/scene?view=${VIEW}`).then((r) => r.json()); } catch (e) { return; }
  if (!state || state.error) return;
  const ren = mainRenderer();
  const c = ren && ren.getActiveCamera();
  const saved = c && { p: c.getPosition(), f: c.getFocalPoint(), u: c.getViewUp(), s: c.getParallelScale(), a: c.getViewAngle() };
  await renderWindow.synchronize(state);
  if (saved) {
    const c2 = mainRenderer().getActiveCamera();
    c2.setPosition(...saved.p); c2.setFocalPoint(...saved.f); c2.setViewUp(...saved.u);
    c2.setParallelScale(saved.s); c2.setViewAngle(saved.a);
  }
  renderWindow.render();
}

async function loadScene() {
  let state;
  try { state = await fetch(`${SCENE}/scene?view=${VIEW}`).then((r) => r.json()); }
  catch (e) { console.warn('[offload] scene server not reachable; overlay disabled'); return false; }
  if (state && state.error) { console.error('[offload] scene error', state.error); return false; }
  await renderWindow.synchronize(state);
  if (!bound) { interactor.bindEvents(host); bound = true; }   // 3D interaction is LOCAL on this canvas
  renderWindow.render();
  return true;
}

(async () => {
  if (!(await loadScene())) return;          // no offload available -> normal video desktop, no overlay
  fetch(`${SCENE}/keyhole?on=1`).catch(() => {});   // server stops ray-casting the 3D (CPU win); overlay covers it
  await refreshViewport();
  // camera sync (poll-based -- robust vs interactor event names): push the LOCAL camera to Slicer on change
  let lastCam = '';
  setInterval(() => {
    const ren = mainRenderer();
    if (!ren) return;
    const c = ren.getActiveCamera();
    const sig = JSON.stringify([c.getPosition(), c.getFocalPoint(), c.getViewUp(), c.getParallelScale()]);
    if (sig !== lastCam) { lastCam = sig; postCamera(); }
  }, 150);
  window.addEventListener('resize', positionOverlay);
  setInterval(refreshViewport, 500);         // track the 3D-view rect as the Slicer layout/window changes
  setInterval(reSync, 2000);                 // live: pick up Slicer-side changes (transfer functions, data)
  console.log('[offload] overlay active (camera sync + keyhole + live updates)');
})();
