# Deploying Desktopia on NRP/Nautilus (MGHPCC)

Desktopia streams over **WebSocket (TCP/WSS)** here instead of QUIC/UDP, so it rides NRP's HTTPS
ingress — no public UDP port needed. The pod also serves the client page, so you just open a URL.

## Concrete steps
1. **Log in & get your namespace** — https://portal.nrp-nautilus.io/ → CILogon (MGB/InCommon, or the
   IdP you used). Portal **Namespaces** page shows your reserved 3D-Slicer namespace. Download the
   kubeconfig (profile → *Get Config*) to `~/.kube/config`; `brew install kubectl`.
2. **Find an MGHPCC GPU + the region label:**
   ```
   kubectl get nodes -L topology.kubernetes.io/region,topology.kubernetes.io/zone,nvidia.com/gpu.product
   ```
   Note the East-Coast region value and a GPU product present there; put them in `nrp-desktopia.yaml`.
3. **Build & push the image** (to the in-cluster registry → fast node pulls; needs a GitLab token from
   gitlab.nrp-nautilus.io, or use Docker Hub):
   ```
   docker build -f deploy/Dockerfile.nrp -t gitlab-registry.nrp-nautilus.io/<you>/desktopia:latest .
   docker push gitlab-registry.nrp-nautilus.io/<you>/desktopia:latest
   ```
4. **Edit `nrp-desktopia.yaml`**: replace `<NS>`, `<REGISTRY/IMAGE>`, and the `region` value.
5. **Deploy & open:**
   ```
   kubectl apply -f deploy/nrp-desktopia.yaml
   kubectl get pod desktopia -n <NS> -w        # wait for Running
   ```
   Open **https://desktopia-<NS>.nrp-nautilus.io/** — the page connects over WSS through the ingress.

## What to verify on the live pod (the unknowns)
- **Render node:** `kubectl exec -it desktopia -n <NS> -- ls -l /dev/dri` — the compositor needs
  `renderD128`. If it's absent under NRP's Pod Security Admission, ask admins (Matrix) for a device mount.
- **Stack came up:** `kubectl logs desktopia -n <NS>` should show `WebSocket streamer on tcp/4434 (plain)`,
  the compositor's `Supported DMA formats: [...]` (non-empty), and Slicer launching.
- **NVENC:** NRP allows it (unlike vast) — to use GPU encode, ensure the `nvcodec`/`nvh264enc` GStreamer
  plugin loads; `encoder_bin()` auto-prefers it.

## Caveats
- **GPU Pod, not Deployment** (NRP policy) → ~6h interactive cap; fine for a session, ask for an
  exception for always-on. Also: GPU pods must keep >40% utilization; an idle desktop may get reaped.
- **TCP head-of-line blocking** is the only quality risk vs QUIC, and it's negligible on a local
  MGHPCC link (low RTT/loss).

## Testing the WS transport on vast first (optional, before NRP)
`DESKTOPIA_TRANSPORT=websocket python3 launch.py` launches a vast box whose `status.json` selects the
WS path (`wss://<ip>:<4434 port>`, our self-signed cert). Because it's self-signed, first visit
`https://<ip>:<wsport>/` in the browser once and accept the cert warning, then reload the desktop tab.
(On NRP this isn't needed — the ingress provides a real cert.)
