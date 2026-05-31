# Setup — replicate Desktopia from scratch

End-to-end, assuming nothing but a Mac (or any laptop) with a terminal. Reproduces the repo,
the CI→registry pipeline, the vast.ai account/key, and the full dev/test loop. No local Docker
is ever required — the Mac is arm64, vast.ai hosts are amd64, so all image builds happen in CI.

See `SECURITY.md` for the key-scope / balance / 2FA reasoning referenced below, and `README.md`
for the architecture and the in-container "sharp edges."

---

## 0. Prerequisites (on the laptop)

```bash
# GitHub CLI (repo + auth)
brew install gh
gh auth login                      # or: gh auth status   to confirm

# vast.ai CLI
pip install --user vastai          # provides the `vastai` command

# rsync + ssh are already on macOS; openssl is used only inside the container
```

A **Chromium-based browser** is needed for the client (WebTransport + WebCodecs are
Chromium-only today).

---

## 1. The repository

The repo is already created at `github.com/pieper/desktopia` (private). To recreate from an
empty directory:

```bash
mkdir desktopia && cd desktopia
git init
# ... add the files (Dockerfile, entrypoint.sh, server.py, client/index.html,
#     vast.sh, Makefile, .github/workflows/build.yml, README.md, SECURITY.md, SETUP.md,
#     .gitignore, .dockerignore) ...
git add -A && git commit -m "Scaffold Desktopia"
gh repo create desktopia --private --source=. --remote=origin \
  --description "vast.ai GPU desktop streamed to the browser over QUIC/WebTransport" --push
```

Repo layout:

| Path | Role |
|---|---|
| `Dockerfile` | Ubuntu 24.04 + GLVND (no host driver) + Xorg + GStreamer/NVENC + aioquic |
| `entrypoint.sh` | Derive Xorg BusID from `nvidia-smi`, write `xorg.conf`, start X, mint cert, run server |
| `server.py` | GStreamer appsink → QUIC-datagram fan-out to WebTransport sessions |
| `client/index.html` | WebTransport + WebCodecs client with the loss handler |
| `.github/workflows/build.yml` | Build amd64 image in CI, push to GHCR |
| `vast.sh` / `Makefile` | vast.ai CLI dev-loop wrapper |
| `README.md` / `SECURITY.md` / `SETUP.md` | Architecture / threat model / this guide |

---

## 2. CI → GHCR image pipeline

The workflow `.github/workflows/build.yml` builds on free amd64 GitHub runners and pushes
`ghcr.io/pieper/desktopia:latest` (+ a `:<sha>` tag). It triggers on pushes that touch
`Dockerfile`, `entrypoint.sh`, `server.py`, or the workflow itself, and via manual dispatch:

```bash
gh workflow run build        # manual trigger
gh run watch                 # follow the latest run
gh run list --limit 3        # history
```

It authenticates with the built-in `GITHUB_TOKEN` (workflow `permissions: packages: write`) —
**no PAT to manage.**

### Make the GHCR package public (one-time)

So vast.ai can pull the image without registry credentials:

1. `github.com/users/pieper/packages/container/desktopia/settings`
2. **Danger Zone → Change visibility → Public.**

Repo stays private; only the image package is public. (Alternative: keep it private and pass a
GHCR pull token to vast.ai — more setup, not worth it for this.)

---

## 3. vast.ai account + scoped key

1. Create / log into the vast.ai account. **Enable 2FA on the web login and on the account
   email** (see `SECURITY.md`).
2. Add a small amount of **prepaid credit** (e.g. $20–40 — this is your real spend cap), ideally
   on a dedicated low-limit / virtual card.
3. Create a **scoped** API key at `cloud.vast.ai/manage-keys/?tab=api-keys` → **+ New**:
   - Name: `desktopia`
   - **User**: Read ✅, Write ⬜️
   - **Instances**: Read ✅, Write ✅
   - **Billing/Earning**: Read ⬜️, Write ⬜️
   - **Miscellaneous**: ⬜️
   - "Require 2FA for this key": **off** (it's an automation key — see `SECURITY.md`)
4. **Copy the key immediately** (one-time-viewable) into your keychain, then register it:

```bash
vastai set api-key <THE_KEY>
make search        # confirms the key works; spends nothing
```

If `search`/`logs` errors with insufficient permissions, enable Miscellaneous, then
Billing/Earning Read (never Write), and retry.

---

## 4. The dev / test loop

Two phases (full detail in `README.md`). Phase 1 debugs in-container behavior on a stock CUDA
image without rebuilding; Phase 2 runs the baked GHCR image.

```bash
# --- Phase 1: interactive debugging on a CUDA base image (no Docker) ---
make search                 # pick an OFFER_ID (cheapest single RTX 4090 first)
make up OFFER=<id>          # launch CUDA base + UDP port 4433 + graphics caps
make ls                     # instance id + status
make sync                   # rsync the working tree to /root/desktopia
make ssh                    # shell in; run the apt block, then: bash entrypoint.sh
                            #   verify: glxinfo | grep renderer  -> "NVIDIA", not "llvmpipe"
                            #   verify: gst-inspect-1.0 nvh264enc -> confirm property names

# --- Phase 2: run the built image ---
git push                    # triggers CI -> ghcr.io/pieper/desktopia:latest
make up-ghcr OFFER=<id>     # launch the GHCR image instead of the CUDA base

# --- connect the browser ---
make port                   # public IP:PORT mapped to 4433/udp
make logs                   # grab the CERT_SHA256_BASE64= line printed at boot
#   paste both into client/index.html (IP_PORT and CERT_B64), open it in Chromium

# --- always clean up ---
make down                   # destroy the instance (stops billing)
```

`vast.sh` targets `the first running instance`; set `DESKTOPIA_INSTANCE=<id>` to pin a specific
one when several are running.

---

## 5. Launch parameters (reference)

Applied automatically by `vast.sh`; here for manual `vastai create instance` use:

```
--image  nvidia/cuda:12.4.1-runtime-ubuntu24.04   # Phase 1 (or ghcr.io/pieper/desktopia:latest)
--env    '-p 4433:4433/udp -e NVIDIA_DRIVER_CAPABILITIES=all -e NVIDIA_VISIBLE_DEVICES=all'
--disk   40
--ssh --direct
```

- Rent a **single** GPU (multi-GPU triggers NVIDIA enumeration bugs). Prefer **Ada (RTX 4090)** —
  best NVENC; `nvav1enc` available if you switch to AV1.
- `NVIDIA_DRIVER_CAPABILITIES=all` is **mandatory** — it must include `graphics,display,video`
  or Xorg/GLX/NVENC silently fail. Never `apt install nvidia-driver-*` in the image; the
  Container Toolkit injects matching host libs.
- `-p 4433:4433/udp` — QUIC is UDP; the `/udp` is required. Read the mapped public port from
  `make port` / the instance's Open Ports panel.

---

## 6. Recovery — if this chat is lost again

The full design discussion is preserved at
`~/.claude/projects/-Users-pieper/8747c79a-2aa5-43eb-abb4-27caaf3021ea.jsonl` (May 30–31 2026),
and summarized in Claude memory under `project_desktopia_quic_desktop`. This repo (README +
SECURITY + SETUP) is the authoritative, self-contained record — start here, not the transcript.
