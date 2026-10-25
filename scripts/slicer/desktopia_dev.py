"""In-Slicer side of the vast dev box (`./vast.sh slicer-up`), imported by ~/.slicerrc.py.

- start_mcp(): the MCP server (slicer_mcp_server.py, vendored beside this file) on port 2026
  (DESKTOPIA_MCP_PORT), with its access dialog off - nobody is at the box to click it. The port
  is not in vast's port map, so the only way in is the ssh tunnel (`./vast.sh tunnel`).
  execute_python is arbitrary code execution by design (SECURITY.md).
- load_modules(): registers and loads every scripted module under ~/slicer-modules/<dir>/
  (a directory holding <Name>.py), the way the Extension Wizard's "Select Extension" does
  (ExtensionWizard.loadModules) minus its dialog. `./vast.sh push-module` rsyncs a module there
  and calls this over MCP, and the slicerrc calls it again after every Slicer restart.
"""
import glob
import os

MODULES_ROOT = os.path.expanduser("~/slicer-modules")
mcpLogic = None


def start_mcp(port=None):
    global mcpLogic
    import slicer_mcp_server
    if mcpLogic is not None:
        mcpLogic.stop()
    port = int(port or os.environ.get("DESKTOPIA_MCP_PORT") or 2026)
    mcpLogic = slicer_mcp_server.startMcpServer(port=port, autoAllow=True)
    print(f"DESKTOPIA dev: MCP on :{mcpLogic.port}/mcp", flush=True)
    return mcpLogic


def module_files(root=MODULES_ROOT):
    """``<root>/<dir>/<Name>.py`` where <Name> is the directory's name - a scripted module's
    layout (HaversackSegment/HaversackSegment.py)."""
    out = []
    for d in sorted(glob.glob(os.path.join(root, "*", ""))):
        name = os.path.basename(os.path.normpath(d))
        f = os.path.join(d, name + ".py")
        if os.path.isfile(f):
            out.append(f)
    return out


def load_modules(root=MODULES_ROOT):
    """Register and load the scripted modules under ``root``; returns {name: status}."""
    import qt
    import slicer
    factory = slicer.app.moduleManager().factoryManager()
    loaded = set(factory.instantiatedModuleNames())
    status, todo = {}, []
    for f in module_files(root):
        name = os.path.splitext(os.path.basename(f))[0]
        if name in loaded:
            status[name] = "already loaded (restart Slicer to pick up changes: ./vast.sh slicer-restart)"
            continue
        factory.registerModule(qt.QFileInfo(f))
        if not factory.isRegistered(name):
            status[name] = "could not be registered"
            continue
        todo.append(name)
    if todo:
        ok = factory.loadModules(todo)
        for name in todo:
            status[name] = "loaded" if ok else "load reported an error (see the Python console)"
    return status
