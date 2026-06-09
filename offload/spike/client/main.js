/*
 * Desktopia 3D-offload spike — client logic (bundled with vtk.js by esbuild -> bundle.js).
 * Fetches the serialized Slicer 3D-view scene from the Python exporter (slicer_scene_export.py) and
 * renders it LOCALLY with vtk.js (this machine's GPU), with fully local camera interaction. Data-array
 * blobs are resolved on demand by md5 hash via /array.
 *
 * Build:  see build.sh (runs esbuild in a node container).  vtk.js's standalone UMD global was removed
 * in v36 and generic ESM CDNs (esm.sh/jsdelivr) break its singletons, so we bundle once with one
 * shared vtk-core.
 */
import '@kitware/vtk.js/Rendering/Profiles/All';   // registers OpenGL view-node factories (renderer/actor/mapper/volume)
import vtkSynchronizableRenderWindow from '@kitware/vtk.js/Rendering/Misc/SynchronizableRenderWindow';
import vtkOpenGLRenderWindow from '@kitware/vtk.js/Rendering/OpenGL/RenderWindow';
import vtkRenderWindowInteractor from '@kitware/vtk.js/Rendering/Core/RenderWindowInteractor';
import vtkInteractorStyleTrackballCamera from '@kitware/vtk.js/Interaction/Style/InteractorStyleTrackballCamera';

const container = document.getElementById('view');
const statEl    = document.getElementById('stat');
const serverEl  = document.getElementById('server');

const CONTEXT_NAME = 'slicer-offload';
const syncCtx = vtkSynchronizableRenderWindow.getSynchronizerContext(CONTEXT_NAME);

let renderWindow, glWindow, interactor, bound = false;

function setup() {
  renderWindow = vtkSynchronizableRenderWindow.newInstance({ synchronizerContext: syncCtx });
  glWindow = vtkOpenGLRenderWindow.newInstance();
  glWindow.setContainer(container);
  renderWindow.addView(glWindow);

  interactor = vtkRenderWindowInteractor.newInstance();
  interactor.setView(glWindow);
  interactor.initialize();
  interactor.setInteractorStyle(vtkInteractorStyleTrackballCamera.newInstance());
  // bindEvents is deferred to loadScene (after the first synchronize), so the interactor always has a
  // current renderer when events start flowing -> no "can not forward events" warning.

  const resize = () => glWindow.setSize(container.clientWidth, container.clientHeight);
  window.addEventListener('resize', resize); resize();
}

async function loadScene() {
  const server = serverEl.value.replace(/\/$/, '');
  const view   = document.getElementById('vidx').value || '0';
  // resolve array blobs the deserializer asks for, by md5 hash
  syncCtx.setFetchArrayFunction((hash) =>
    fetch(`${server}/array?hash=${hash}`).then((r) => r.arrayBuffer()));

  const t0 = performance.now();
  statEl.textContent = 'fetching /scene…';
  let state;
  for (let attempt = 1; ; attempt++) {              // retry: the server may still be starting after a restart
    try {
      state = await fetch(`${server}/scene?view=${view}`).then((r) => r.json());
      break;
    } catch (e) {
      if (attempt >= 5) { statEl.textContent = 'fetch failed: ' + e; return; }
      statEl.textContent = `fetching /scene… (retry ${attempt})`;
      await new Promise((res) => setTimeout(res, 500));
    }
  }
  if (state && state.error) { statEl.textContent = 'server error (see console)'; console.error(state.error); return; }

  const tFetch = performance.now();
  try {
    await renderWindow.synchronize(state);        // pulls arrays via fetchArray, rebuilds scene
  } catch (e) {
    statEl.textContent = 'synchronize failed (see console)'; console.error('[offload] synchronize threw', e); return;
  }
  // diagnostics: did the scene actually build? (renderers / surface actors / volumes)
  const rens = renderWindow.getRenderers();
  let actors = 0, volumes = 0;
  rens.forEach((r) => { actors += r.getActors().length; volumes += (r.getVolumes ? r.getVolumes().length : 0); });
  console.log(`[offload] synchronized: renderers=${rens.length} actors=${actors} volumes=${volumes}`);
  // use Slicer's actual camera (synced in the state) for a view identical to the server -- no resetCamera
  if (!bound) { interactor.bindEvents(container); bound = true; }   // enable interaction now a renderer exists
  renderWindow.render();
  const tDone = performance.now();

  const sz = JSON.stringify(state).length;
  // self-report in the status bar so we don't need the console: scene composition + sizes + timing
  statEl.textContent =
    `ren=${rens.length} actors=${actors} vol=${volumes} · ${(sz/1024).toFixed(0)}KB · ` +
    `fetch ${(tFetch-t0).toFixed(0)}ms · sync+render ${(tDone-tFetch).toFixed(0)}ms`;
}

document.getElementById('load').addEventListener('click', loadScene);

// auto-reload to stress the sync path and watch for the "gets slow over time" leak (the keri risk)
let timer = null;
document.getElementById('auto').addEventListener('change', (e) => {
  if (e.target.checked) timer = setInterval(loadScene, 1000);
  else { clearInterval(timer); timer = null; }
});

setup();
loadScene();          // auto-load on open so there's a renderer immediately (no need to click)
