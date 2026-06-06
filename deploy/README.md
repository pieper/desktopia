# Deploying Desktopia on NRP/Nautilus (MGHPCC)

Desktopia streams over **WebSocket (TCP/WSS)** here instead of QUIC/UDP, so it rides NRP's HTTPS
ingress — no public UDP port needed. The pod also serves the client page, so you just open a URL.

## Test path (now): generic Ubuntu pod, install + run at runtime — NO custom image
Uses a stock `ubuntu:24.04` pod; we push the repo in and `entrypoint-wayland.sh` installs deps +
fetches the compositor/Slicer and launches. Nothing to build or push; private repo stays private.

1. **Log in & namespace** — https://portal.nrp-nautilus.io/ (CILogon), portal *Get Config* →
   `~/.kube/config`. Namespace = `slicer-dev`.
2. **Deploy the pod (waits for code):**
   ```
   kubectl apply -f deploy/nrp-desktopia.yaml
   kubectl get pod desktopia -n slicer-dev -w        # wait for Running
   ```
3. **Push the code in** (from the desktopia repo root):
   ```
   tar czf - --exclude=.git --exclude=.venv --exclude=__pycache__ . \
     | kubectl exec -i -n slicer-dev desktopia -- bash -c 'mkdir -p /opt/desktopia && tar xzf - -C /opt/desktopia'
   ```
   The pod's wait-loop then runs `entrypoint-wayland.sh` (installs deps + fetches compositor/Slicer + launches).
4. **Watch & open:**
   ```
   kubectl logs -f desktopia -n slicer-dev
   ```
   then open **https://desktopia-slicer-dev.nrp-nautilus.io/**
5. **Debug interactively:** `kubectl exec -it desktopia -n slicer-dev -- bash`
6. **Tear down:** `kubectl delete -f deploy/nrp-desktopia.yaml`

### What to look for in the logs (paste me these)
- `WebSocket streamer on tcp/4434 (plain)` — WS server up.
- `Supported DMA formats: [ ... ]` **non-empty** — GPU render node works (else see gotcha 1).
- Slicer launching; `ws viewer connected; sessions: 1` when your browser hits it.
- `nvh264enc` vs `x264enc` in the pipeline line (A10 has NVENC; NRP allows it).

### Likely iteration points
1. **`Supported DMA formats: []`** → render node `/dev/dri` not exposed. Uncomment the
   `volumeMounts`/`volumes` (`/dev/dri`) lines in the manifest; if Pod Security Admission blocks the
   hostPath, that's a one-line Matrix ask to NRP admins.
2. **Pod denied for running as root** (restricted PSA) → the runtime install needs root. Then we switch
   to the baked image below (deps installed at build time; runs as the unprivileged `user`).

## Future path: a self-contained baked image (after the pieces are proven)
`deploy/build-image.sh` + `deploy/Dockerfile.nrp` bake everything into a vanilla `ubuntu:24.04` at build
time → fast cold pods, no runtime downloads, runs without root. Build via CI → push to GHCR (public →
NRP pulls with no secret), then change the manifest `image:` + drop the wait-for-code `command`.
**Deferred until the generic-Ubuntu test above works end-to-end.**
