# Desktopia on Google Colab (CPU / software path)

[`desktopia_colab.ipynb`](desktopia_colab.ipynb) runs Desktopia's **no-GPU software path** on a Colab
backend and embeds the live desktop in an output cell:

- 3D Slicer renders with Mesa **llvmpipe** under **Xvfb** (no GPU, no DRM render node),
- GStreamer captures it (`ximagesrc`) and software-encodes **H.264 (x264)**,
- the stream reaches the cell over a **WebSocket** (Colab has no public UDP, so the QUIC/WebTransport
  transport can't be used here), tunneled via `google.colab.output.serve_kernel_port_as_iframe`,
- mouse/keyboard travel back via XTEST.

Good for slice viewing, segmentation overlays, and slow 3D. Use a **Chrome**-based browser (WebCodecs +
WebSocket). A plain CPU runtime is enough.

## Use it

Open the notebook in Colab (Upload, or **File → Open notebook → GitHub**) and run the cells top to
bottom. The desktop appears in cell 4. To restart the stack, re-run cell 3.

Override the source branch/repo by setting `DESKTOPIA_BRANCH` / `DESKTOPIA_REPO` env before cell 2
(the notebook defaults to the `software-render` branch until it merges to `main`).

## Known caveat

The one thing that varies by Colab proxy behavior is whether the **WebSocket `Upgrade` passes through**
`serve_kernel_port_as_iframe`. If the frame never connects, the troubleshooting cell falls back to
`serve_kernel_port_as_window` (a real browser tab), which is the more reliable path for WebSockets.
