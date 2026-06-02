# Running Desktopia on other GPU backends

Desktopia is published as a container image (`ghcr.io/pieper/desktopia:latest`) and the
`vast.sh`/`Makefile` tooling targets [vast.ai](https://vast.ai/), but the image itself is just a
standard NVIDIA‑GPU Docker container. It runs on **any host that can give a container a graphics‑
capable NVIDIA GPU and a public UDP port** — AWS, Google Cloud, Azure, the GPU‑rental clouds, or
your own workstation. This guide lists those backends and how to test the image on each.

---

## What the image needs (the universal checklist)

1. **An NVIDIA GPU with the full proprietary driver** on the host — *not* a CUDA‑only/"headless"
   driver. The GL/EGL and NVENC user‑space libraries must be present so the Container Toolkit can
   inject them.
2. **Docker + the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)**
   (so `--gpus all` works).
3. **The graphics/display/video capabilities**, via `-e NVIDIA_DRIVER_CAPABILITIES=all`.
   ⚠️ This is the #1 gotcha: `--gpus all` *alone* defaults to **compute,utility** — which has no
   OpenGL/EGL and no NVENC. Without `=all` you get a software (`llvmpipe`) renderer or no encoder.
4. **A render node** (`/dev/dri/renderD128`). The Container Toolkit exposes it automatically once
   a graphics/display capability is requested; the compositor needs no X server and no display.
5. **A publicly reachable UDP port** for QUIC (default `4433/udp`). ⚠️ This is the #2 gotcha:
   QUIC is UDP, so a backend that only proxies HTTP/TCP (some managed GPU clouds) cannot carry the
   stream — you need a real public IP with inbound UDP, or UDP port mapping.
6. **(For hardware encode) an NVENC‑capable GPU.** Without one the image still runs but falls back
   to the CPU `x264` encoder (works; higher CPU, lower frame rate).

### GPU → NVENC cheat‑sheet

| GPU (typical instance) | NVENC | Notes |
|---|---|---|
| **T4** (AWS g4dn, Azure NCas_T4_v3, GCE T4) | ✅ | cheapest good streaming GPU |
| **A10 / A10G** (AWS g5, Azure NVads A10 v5) | ✅ | visualization‑oriented, great fit |
| **L4 / L40S** (AWS g6/g6e, GCE G2) | ✅ | Ada; also supports AV1 (`nvav1enc`) |
| **RTX 40‑series** (vast.ai) | ✅ | best NVENC + AV1 |
| **V100** (AWS p3, GCE) | ✅ | works, pricey |
| **A100** (AWS p4, GCE A2, Azure NC A100 v4) | ❌ | **no NVENC engine** → x264 fallback |
| **H100 / H200** (AWS p5, GCE A3) | ❌ | **no NVENC engine** → x264 fallback |

For streaming, prefer a **T4 / A10 / L4** instance. Avoid A100/H100 unless CPU encoding is fine.

---

## The universal run command

```bash
docker run --rm --gpus all \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -p 4433:4433/udp \
  ghcr.io/pieper/desktopia:latest
```

The container brings up the headless Wayland compositor, the H.264/QUIC server, and the desktop
(3D Slicer). It prints a `CERT_SHA256_BASE64=` line at startup — read it with `docker logs <id>`.
Paste the host's **public IP : mapped UDP port** and that **cert hash** into `client/index.html`
(the `IP_PORT` and `CERT_B64` constants) and open the page in a Chromium‑based browser.

---

## Pre‑flight checks (validate a host before a full run)

Run these against the image to confirm the backend gives the container what it needs. They use
`--entrypoint` so they exit immediately instead of starting the desktop.

```bash
IMG=ghcr.io/pieper/desktopia:latest

# 1. GPU + driver visible in the container
docker run --rm --gpus all -e NVIDIA_DRIVER_CAPABILITIES=all --entrypoint nvidia-smi "$IMG"

# 2. NVENC present? (libnvidia-encode is injected only by the 'video' capability)
docker run --rm --gpus all -e NVIDIA_DRIVER_CAPABILITIES=all --entrypoint bash "$IMG" -c \
  'ls /usr/lib/x86_64-linux-gnu/libnvidia-encode.so* >/dev/null 2>&1 \
     && echo "NVENC: available" || echo "NVENC: MISSING -> CPU x264 fallback"'

# 3. Render node present + the compositor element registers (hardware GL path)
docker run --rm --gpus all -e NVIDIA_DRIVER_CAPABILITIES=all --device /dev/dri \
  --entrypoint bash "$IMG" -c \
  'ls -l /dev/dri/renderD* ; \
   LD_LIBRARY_PATH=/usr/local/lib/x86_64-linux-gnu \
   GST_PLUGIN_PATH=/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0 \
   gst-inspect-1.0 waylanddisplaysrc | head -3'
```

`nvidia-smi` must list your GPU; check #2 should say *available* on a T4/A10/L4; check #3 must show
`/dev/dri/renderD128` and print the `waylanddisplaysrc` element (not "No such element").

---

## Backends

### Generic recipe (any cloud or bare metal)

1. Launch a GPU instance whose host has the NVIDIA driver, Docker, and the Container Toolkit
   (a "deep learning"/GPU image gives you all three; otherwise install the driver +
   `nvidia-container-toolkit`).
2. Open inbound **UDP** on your chosen port (default 4433) in the firewall/security group.
3. Run the [universal command](#the-universal-run-command); read the cert hash from `docker logs`.
4. Point `client/index.html` at the instance's public IP and that port.

### AWS EC2

- **Instance types:** `g4dn.xlarge` (T4) or `g5.xlarge` (A10G) are the sweet spot; `g6` (L4) for
  AV1. Avoid `p4d`/`p5` (A100/H100, no NVENC).
- **Image:** the *AWS Deep Learning Base GPU AMI* ships the driver + Docker + Container Toolkit.
  (Otherwise: Ubuntu 24.04 AMI + install the NVIDIA driver and `nvidia-container-toolkit`.)
- **Open UDP:** in the instance's **Security Group**, add an inbound rule
  *Custom UDP, port 4433, source = your IP*.
- Run the universal command. Connect to the instance's **public IPv4 : 4433**.

### Google Cloud (Compute Engine)

- **Instance types:** an `n1-standard-*` with a **T4** accelerator, or a `g2-standard-*` (**L4**).
  Avoid `a2`/`a3` (A100/H100).
- **Image:** the *Deep Learning VM* images (driver + Docker + Container Toolkit preinstalled), or
  Ubuntu + driver install.
- **Open UDP:**
  ```bash
  gcloud compute firewall-rules create desktopia-quic \
    --allow udp:4433 --direction=INGRESS --source-ranges=<YOUR_IP>/32
  ```
- Run the universal command. Connect to the VM's external IP : 4433.

### Azure

- **Instance types:** the **NV‑series** is purpose‑built for GPU visualization — `NVads A10 v5`
  (A10) or `NCas_T4_v3` (T4) both have NVENC. Avoid `NC A100 v4`.
- **Image:** Ubuntu + the *NVIDIA GPU Driver* VM extension (and Docker + Container Toolkit), or a
  Data Science VM.
- **Open UDP:** add an inbound **Network Security Group** rule for *UDP / 4433* from your IP.
- Run the universal command. Connect to the VM's public IP : 4433.

### GPU‑rental clouds (vast.ai, RunPod, Lambda, Paperspace, CoreWeave)

These rent single GPUs cheaply and are the closest match to the reference setup.

- **vast.ai** — the built‑in path; use `make up-ghcr OFFER=<id>` (or set the image in the web UI)
  with `-p 4433:4433/udp` and `NVIDIA_DRIVER_CAPABILITIES=all`. See [SETUP.md](SETUP.md).
- **Lambda / Paperspace / CoreWeave** — give you a VM with a **public IP**; install Docker +
  Container Toolkit if not present, open UDP 4433 in the host firewall (`ufw allow 4433/udp`), and
  run the universal command.
- **RunPod** — supports custom images and GPU pods, but its standard port proxy is HTTP/TCP. For
  QUIC you need a pod that exposes a **public IP with direct UDP** (RunPod "community cloud" hosts
  that offer it) and map `4433/udp`. If only TCP proxying is available, the QUIC stream can't reach
  the browser — pick a different backend.

### Local / on‑prem workstation

The easiest way to test the image. Any machine with an NVIDIA GPU + Docker + Container Toolkit:

```bash
docker run --rm --gpus all -e NVIDIA_DRIVER_CAPABILITIES=all -p 4433:4433/udp \
  ghcr.io/pieper/desktopia:latest
```

Open `client/index.html` with `IP_PORT = "localhost:4433"` (or the LAN IP for another machine on
your network) and the printed cert hash. No firewall changes needed for `localhost`.

---

## Troubleshooting

- **Software renderer (`llvmpipe`) / black 3D** — the graphics capability is missing. Confirm
  `-e NVIDIA_DRIVER_CAPABILITIES=all` and that the host has the *full* driver, not a CUDA‑only one.
- **`NVENC: MISSING` / encoder errors** — the GPU has no NVENC (A100/H100) or the `video`
  capability wasn't injected. The image falls back to CPU `x264` automatically; for hardware
  encode switch to a T4/A10/L4.
- **Browser never connects** — the UDP port isn't reachable: firewall/security‑group rule missing,
  or the backend only proxies TCP/HTTP. QUIC needs end‑to‑end UDP.
- **`waylanddisplaysrc` not found / compositor fails** — `/dev/dri/renderD128` wasn't exposed; add
  `--device /dev/dri` to the `docker run`, and confirm the graphics capability.
- **Certificate rejected / expired** — the cert is short‑lived; restart the container to mint a
  fresh one, and re‑copy the new `CERT_SHA256_BASE64` hash into the client.
