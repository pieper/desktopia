---
name: vast-slicer
description: Bring up a vast.ai GPU box running desktopia's streamed Linux desktop with the latest stable 3D Slicer, tunnel Slicer's MCP server over ssh, and drive it from here - install and test a scripted module or extension (e.g. SlicerHaversack) on a real CUDA machine. Use when asked to "bring up a vast Slicer", "get a Slicer GPU box", "test <module> on vast", or to drive/inspect a Slicer already running there.
---

# vast-slicer: a Slicer dev box driven over MCP

Everything runs from the desktopia repo (`~/slicer/desktopia`) through `./vast.sh` (`make` has the
same targets). The pieces:

- `launch.py`: rents `DESKTOPIA_CANDIDATES` (3) RTX 4090s from different providers at once and
  measures each from this machine: `nvidia-drm modeset=Y` (else no hardware GLX), RTT, ssh
  throughput, and the box's speed fetching Slicer. It keeps the best that passes, destroys the
  rest, then boots the stack and opens the stream in Chrome. GL-bad machines/providers go in
  `.bad-machines`; every measurement goes in `.host-tests.jsonl`, which `scripts/offers.py` ranks
  by instead of vast's (unreliable) geolocation. `./vast.sh probe` runs one round and destroys
  all of them, to build that history.
- `entrypoint-wayland.sh`: fetches the latest **stable** Slicer
  (`download.slicer.org/...stability=release`) into `/opt` on the box.
- `session-wayland.sh`: launches Slicer as `user` on `DISPLAY=:2`. With `DESKTOPIA_MCP=1` it also
  writes a `~/.slicerrc.py` that starts the MCP server and loads `~/slicer-modules/*`.
  `launch.py` sets that flag through `/root/desktopia/.desktopia.env`, which also survives a
  vast stop/start.
- `scripts/slicer/desktopia_dev.py` (in Slicer): `start_mcp()` on :2026 with no access dialog,
  and `load_modules()`. The vendored MCP server is `scripts/slicer/slicer_mcp_server.py`.
- `scripts/mcp_call.py` (here, stdlib only): the client behind `./vast.sh mcp ...`.

## Before anything: cost

**Renting costs money: an RTX 4090 is about $0.5–0.7/h, billed until `stop` or `down`.**
- Ask the user before `slicer-up`.
- At the end, ask whether to `stop` (keeps the disk; restarts in about a minute) or `down`
  (destroys everything).
- Never leave a box running without saying so. `./vast.sh ls` shows what is running.

Prerequisites, all read-only to check:
- `~/.config/vastai/vast_api_key`
- `~/.ssh/vast-ai-rsa`, registered with vast
- `desktopia/.venv/bin/vastai` (vast.sh puts the venv on PATH)
- `./vast.sh ls` and `./vast.sh search` cost nothing

## Bring-up

```bash
cd ~/slicer/desktopia
./vast.sh slicer-up          # run in the BACKGROUND; it keeps serving the page after READY
```
- Watch its output for `=== READY === id=<instance> ...`. That takes about 5–10 min: renting,
  image pull, deps, compositor.
- Chrome opens `http://localhost:8765/index.html`, which is the user's view of the desktop.
- On "Gave up after 3 hosts", or "SSH never authorized", read `vastai logs <id>` before
  retrying.
- With more than one instance, set `DESKTOPIA_INSTANCE=<id>` for every later command.

Then:
```bash
./vast.sh tunnel             # localhost:2026 -> the box's :2026 (backgrounded; tunnel-close ends it)
./vast.sh mcp wait-ready     # polls until Slicer answers; its download+start can trail READY by minutes
./vast.sh mcp tools
./vast.sh mcp py 'import slicer; __result = slicer.app.applicationVersion'
./vast.sh mcp screenshot /tmp/slicer.png      # then Read the PNG to see the desktop
```
For native MCP tools in a *new* Claude Code session (optional; the CLI needs no restart):
`claude mcp add --transport http slicer-vast http://localhost:2026/mcp`.

## Installing code to test

```bash
./vast.sh push-module ~/path/to/Extension/ModuleName   # rsync to ~/slicer-modules/ModuleName + load now
./vast.sh push ~/src/somepackage /home/user/src/somepackage   # any file/dir, owned by `user`
./vast.sh run 'nvidia-smi; tail -20 /tmp/slicer.log'         # one command on the box (as root)
./vast.sh slicer-restart && ./vast.sh mcp wait-ready    # re-import changed module code
```
- `push-module` expects a scripted module directory: `ModuleName/ModuleName.py` plus its `Lib/`
  and `Resources/`.
- Modules under `~/slicer-modules` are loaded again on every Slicer start.
- A module that is already loaded is not re-imported: push again, then `slicer-restart`.

## Rules for driving Slicer over MCP

- **Never call `slicer.app.processEvents()`** in `mcp py` code. The request is being served
  from Slicer's event loop (`offload/SLICERLIVE.md`).
- **Long work must not block a call.** Start it so it returns at once (a QProcess, a
  `qt.QTimer.singleShot`), then poll with short `mcp py` calls: haversack installs and
  segmentations, downloads, `slicer.util.pip_install`.
- **Modal dialogs block the call that opened them.** `QMessageBox.exec_()`, `slicer.util.errorDisplay`,
  `pip_install`'s "Installation Failed", license prompts and file dialogs all run a nested event
  loop. The MCP call that opened one does not return until the dialog closes; later calls still
  answer, from the dialog's own loop. Drive logic rather than dialogs. If a call times out:
  1. list the windows: `./vast.sh run 'sudo -u user DISPLAY=:2 wmctrl -l'`;
  2. close the dialog: `./vast.sh run 'sudo -u user DISPLAY=:2 wmctrl -c "<title>"'`;
  3. or have the user click it in the stream.
- **Return values:** set `__result` in the code. A raised exception comes back as text starting
  `Error:`, and `mcp py` exits 1.
- **Big files:** use `./vast.sh pull REMOTE [LOCAL]` and `./vast.sh push`, not `read_file` or
  `write_file`.
- **Security:** `execute_python` is arbitrary code execution (`SECURITY.md`). :2026 is not in
  vast's port map, so the tunnel is the only way in. Keep it that way: never add 2026 to
  `ENVOPTS`.
- **Don't `vastai attach ssh`.** It rewrites the key and re-breaks the perms the onstart loop
  heals.

## Recipe: SlicerHaversack on a CUDA box

The goal is to verify the module end to end: environment install, a catalog model, LNQ, and
PanTS's region decode.

1. **Push the module and haversack.** Until haversack's `region-models` branch is merged, the
   module's default git source lacks `--json-progress`/`--folds`, so push the local checkout:
   ```bash
   ./vast.sh push-module ~/slicer/latest/SlicerHaversack/HaversackSegment
   ./vast.sh push ~/slicer/latest/haversack/ /home/user/src/haversack/
   ./vast.sh push ~/slicer/latest/lnq-segmenter/ /home/user/src/lnq-segmenter/
   ./vast.sh mcp py 'import qt
   qt.QSettings().setValue("HaversackSegment/HaversackSource", "/home/user/src/haversack")
   qt.QSettings().setValue("HaversackSegment/LNQSource", "/home/user/src/lnq-segmenter")
   __result = "sources set"'
   ```
2. **Run the module's self-test:**
   ```bash
   ./vast.sh mcp py 'import HaversackSegment as HS; HS.HaversackSegmentTest().runTest(); __result = "ok"'
   ```
3. **Install the environment.** It runs as QProcess steps, so the call returns at once; then
   poll. It downloads about 2 GB, and the first time can take several minutes.
   ```bash
   ./vast.sh mcp py 'import slicer
   slicer.util.selectModule("HaversackSegment")
   w = slicer.modules.haversacksegment.widgetRepresentation().self()
   w._onInstallClicked(); __result = w._statusLabel.text'
   # poll:
   ./vast.sh mcp py 'import slicer; w = slicer.modules.haversacksegment.widgetRepresentation().self()
   __result = (w._statusLabel.text, w._envStatus.text, w._runner.isRunning())'
   ```
   It is done when `_envStatus` reads `Ready: ...`. On failure, read the log view:
   `__result = w._logView.toPlainText()[-3000:]`.
4. **Segment with a catalog model:**
   ```bash
   ./vast.sh mcp py 'import qt, slicer, SampleData; v = SampleData.downloadSample("CTChest")
   w = slicer.modules.haversacksegment.widgetRepresentation().self()
   w._inputSelector.setCurrentNode(v); w._deviceCombo.setCurrentIndex(w._deviceCombo.findText("cuda"))
   w._modelTree.findItems("ts.v2:total_fast", qt.Qt.MatchExactly | qt.Qt.MatchRecursive)[0].setSelected(True)
   w._onRunClicked(); __result = w._statusLabel.text'
   ```
   Poll `_statusLabel.text` until it reads `Loaded ...: N structures in T s`. Check the
   segmentation node with `list_nodes` and take a screenshot.
5. **LNQ:** the same, selecting an `lnq:` entry. It downloads through lnq-segmenter, then
   haversack runs the registry's folds.
6. **PanTS.** Its license dialog is modal (see the rules), so **ask the user to accept the
   PanTS terms in chat first**: weights MIT, training data CC BY-NC-ND 4.0, non-commercial
   research only. Then fetch without the dialog:
   ```bash
   ./vast.sh run "sudo -u user -H /home/user/.slicer-haversack/env/bin/python \
     /home/user/slicer-modules/HaversackSegment/HaversackSegmentLib/fetch_model.py pants-regions \
     --dest /home/user/.slicer-haversack/models \
     --accept-license 'pants-regions@2f3e1b5:MIT-weights+CC-BY-NC-ND-4.0-data' --json-progress"
   ```
   Then segment with the `extra:pants-regions` entry. Expect `pancreas_other`, the
   head/body/tail/duct segments, and `pancreatic_lesion` painted last.

## Teardown

```bash
./vast.sh tunnel-close
./vast.sh stop      # or: ./vast.sh down  - ask the user which
```
- After `./vast.sh start`, the onstart reboot brings the desktop and MCP back on its own, via
  `.desktopia.env`.
- Then run `./vast.sh tunnel` and `./vast.sh mcp wait-ready` again. There is no need to
  `slicer-up`: that rents a new box.
