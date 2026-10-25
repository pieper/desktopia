---
name: vast-slicer
description: Make a fresh vast.ai GPU box running desktopia's streamed Linux desktop with the latest stable 3D Slicer and its MCP server, tunnel MCP over ssh, and drive Slicer from here - install and test a scripted module or extension (e.g. SlicerHaversack) on a real CUDA machine. Use when asked to "make/bring up a fresh (vast) Slicer box/instance", "get a Slicer GPU box", "test <module> on vast", or to drive/inspect a Slicer already running there.
---

# vast-slicer: a Slicer dev box driven over MCP

Everything runs from the desktopia repo (`~/slicer/desktopia`) through `./vast.sh` (`make` has the
same targets).

## Quick start: a fresh MCP-enabled box (~5-10 min)

A user asking for a box IS the go-ahead to rent one (see Cost): don't ask again. Steps, in order:

1. **Preflight** (all free, read-only):
   ```bash
   cd ~/slicer/desktopia
   ls ~/.config/vastai/vast_api_key ~/.ssh/vast-ai-rsa .venv/bin/vastai
   ./vast.sh ls                         # note EVERY instance already running: they are not yours
   lsof -nP -iTCP:2026 -sTCP:LISTEN     # a local Slicer often holds :2026 (see step 4)
   pgrep -af launch.py                  # another launcher? it shares client/status.json; ask first
   ```
2. **Launch, in the background** (Bash `run_in_background`; it never exits on success, because it
   keeps serving the user's page):
   ```bash
   ./vast.sh slicer-up 2>&1
   ```
   Watch its output with a Monitor that matches success AND failure, e.g.
   `grep -E --line-buffered "^candidate|KEEP|reject|READY|Gave up|No candidate|never|bad host|has a passphrase"`.
   What happens: it rents 3 boxes from different providers at once, tests each (GPU GL, RTT,
   bandwidth, Slicer-download speed), keeps the best, destroys the other candidates, and provisions
   the winner. `KEEP <id> ... 57ms 99Mb/s` names the winner; then wait for
   `=== READY === id=<id> stream=... cert=...`.
   - "has a passphrase and is not in ssh-agent": the user must run `ssh-add --apple-use-keychain ~/.ssh/vast-ai-rsa`.
   - "No candidate passed" / "Gave up after N rounds": read the `reject` lines for why. Loosen with
     `DESKTOPIA_MAX_RTT_MS` (80), `DESKTOPIA_MIN_DOWN_MBPS` (30), `DESKTOPIA_MIN_FETCH_MBS` (8),
     or widen with `DESKTOPIA_CANDIDATES` (3) / `DESKTOPIA_ROUNDS` (2).
3. **Pin the new box** for every later command. With other instances running, `vast.sh` would
   otherwise pick the first one, which may be the user's:
   ```bash
   export DESKTOPIA_INSTANCE=<id from READY>      # put it on each command line: shell env doesn't persist
   ```
4. **Tunnel and wait for Slicer.** Slicer downloads (~475 MB) and starts after READY, so MCP can
   trail READY by a few minutes:
   ```bash
   DESKTOPIA_INSTANCE=<id> ./vast.sh tunnel           # localhost:2026 -> box :2026, backgrounded
   DESKTOPIA_INSTANCE=<id> ./vast.sh mcp wait-ready
   ```
   If a local Slicer already listens on :2026, the tunnel's localhost bind still wins and `tunnel`
   prints a warning. To avoid any doubt, use another port for all three commands:
   `DESKTOPIA_MCP_PORT=2036` on `slicer-up`, `tunnel` and `mcp`. Only one tunnel at a time is
   supported (`tunnel-close` first).
5. **Verify** it's the right box with hardware GL, and look at it:
   ```bash
   DESKTOPIA_INSTANCE=<id> ./vast.sh mcp py 'import slicer, os
   rw = slicer.app.layoutManager().threeDWidget(0).threeDView().renderWindow()
   __result = (slicer.app.applicationVersion, os.uname().nodename,
               [l for l in rw.ReportCapabilities().splitlines() if "renderer string" in l])'
   DESKTOPIA_INSTANCE=<id> ./vast.sh mcp screenshot <scratchpad>/slicer.png   # then Read the PNG
   ```
   Expect `NVIDIA GeForce RTX 4090`, not `llvmpipe`.
6. **Report to the user**: instance id, location and measured RTT (from the `KEEP` line), that the
   stream is in Chrome at `http://localhost:8765/index.html`, and that it bills until stopped.
   For native MCP tools in a *new* Claude Code session (optional; `./vast.sh mcp` needs nothing):
   `claude mcp add --transport http slicer-vast http://localhost:2026/mcp`.

**If Slicer never answers** (`wait-ready` times out):
`./vast.sh run 'tail -3 /tmp/slicer-fetch.log; pgrep -af SlicerApp-real; tail -20 /tmp/slicer.log'`.
If the fetch finished but Slicer isn't running: `./vast.sh slicer-restart`, then `mcp wait-ready`.

**If the stream picture freezes or lags** while Slicer answers MCP: the X screen is probably fine.
Reload the tab, and close any extra tabs: each is a full-rate viewer.

## How it works

- `launch.py`: the candidate rental and qualification above, then boots the stack and opens the
  stream in Chrome. GL-bad machines/providers (`nvidia-drm modeset=N`: no GBM, so Xwayland has no
  glamor and every GLX context fails, which shows as a black Slicer window) go in `.bad-machines`.
  Every measurement goes in `.host-tests.jsonl`, which `scripts/offers.py` ranks by instead of
  vast's geolocation (unreliable: a "Pennsylvania" host measured 240 ms from Boston). Both files are
  local and gitignored. `./vast.sh probe` runs one round, records it and destroys all the boxes:
  a cheap way to build the history. `./vast.sh candidates 3` shows what the next launch would try.
- `entrypoint-wayland.sh`: fetches the latest **stable** Slicer
  (`download.slicer.org/...stability=release`) into `/opt` on the box.
- `session-wayland.sh`: launches Slicer as `user` on `DISPLAY=:2` once the fetch finishes. With
  `DESKTOPIA_MCP=1` it also writes a `~/.slicerrc.py` that starts the MCP server and loads
  `~/slicer-modules/*`. `launch.py` sets that flag through `/root/desktopia/.desktopia.env`, which
  also survives a vast stop/start.
- `scripts/slicer/desktopia_dev.py` (in Slicer): `start_mcp()` on :2026 with no access dialog,
  and `load_modules()`. The vendored MCP server is `scripts/slicer/slicer_mcp_server.py`.
- `scripts/mcp_call.py` (here, stdlib only): the client behind `./vast.sh mcp ...`.

## Cost and other people's boxes

**Renting costs money: an RTX 4090 is about $0.5–0.7/h, billed until `stop` or `down`.**
- Rent only when the user asked for a box. Candidate testing adds a few cents (losers are destroyed
  within minutes).
- **Never stop or destroy an instance you didn't create in this task**, even one that looks idle or
  broken: the user may be running an experiment on it. `launch.py` only ever destroys the
  candidates it rented itself.
- At the end, ask whether to `stop` (keeps the disk; restarts in about a minute) or `down`
  (destroys everything). Never leave a box running without saying so.

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
DESKTOPIA_INSTANCE=<id> ./vast.sh tunnel-close
DESKTOPIA_INSTANCE=<id> ./vast.sh stop      # or: down - ask the user which; ONLY the box you made
```
- Stopping the background `slicer-up` launcher (TaskStop) only ends the local page server and
  status updates; it does not touch the instance.
- After `./vast.sh start`, the onstart reboot brings the desktop and MCP back on its own, via
  `.desktopia.env`.
- Then run `./vast.sh tunnel` and `./vast.sh mcp wait-ready` again. There is no need to
  `slicer-up`: that rents a new box.
