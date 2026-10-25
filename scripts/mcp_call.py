#!/usr/bin/env python3
"""Talk to a Slicer MCP server from the shell (stdlib only) - the dev box's over `vast.sh tunnel`.

    python3 scripts/mcp_call.py tools                         # the tools, one line each
    python3 scripts/mcp_call.py py 'import slicer; __result = slicer.app.applicationVersion'
    python3 scripts/mcp_call.py py -f script.py               # a file's code; set __result to return a value
    python3 scripts/mcp_call.py call list_nodes '{"class_name": "vtkMRMLScalarVolumeNode"}'
    python3 scripts/mcp_call.py screenshot shot.png
    python3 scripts/mcp_call.py wait-ready --timeout 600      # until Slicer is up and answering

The server (scripts/slicer/slicer_mcp_server.py) speaks MCP's Streamable HTTP transport in JSON mode,
statelessly: one POST of a JSON-RPC message per call. `py` exits 1 when the code raised - the server
reports that as text starting "Error:" rather than as a JSON-RPC error. Never call processEvents()
in that code (offload/SLICERLIVE.md): the request is being served from Slicer's event loop.
Default URL: http://localhost:$DESKTOPIA_MCP_PORT/mcp (2026), or --url.
"""
import argparse
import base64
import itertools
import json
import os
import sys
import time
import urllib.error
import urllib.request

_ids = itertools.count(1)


def rpc(url, method, params=None, timeout=600):
    body = json.dumps({"jsonrpc": "2.0", "id": next(_ids), "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        msg = json.loads(r.read().decode("utf-8"))
    if "error" in msg:
        raise RuntimeError(f"{method}: {msg['error'].get('message')}")
    return msg["result"]


def call(url, tool, arguments, timeout=600):
    return rpc(url, "tools/call", {"name": tool, "arguments": arguments}, timeout=timeout)


def _texts(result):
    return [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]


def _failed(result):
    return result.get("isError") or any(t.startswith("Error:\n") for t in _texts(result))


def main(argv=None):
    port = os.environ.get("DESKTOPIA_MCP_PORT") or "2026"
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=f"http://localhost:{port}/mcp")
    ap.add_argument("--timeout", type=float, default=600, help="seconds per request (default 600)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tools")
    p = sub.add_parser("call")
    p.add_argument("tool")
    p.add_argument("arguments", nargs="?", default="{}", help="JSON object")
    p = sub.add_parser("py")
    p.add_argument("code", nargs="?")
    p.add_argument("-f", "--file")
    p = sub.add_parser("screenshot")
    p.add_argument("out")
    p = sub.add_parser("wait-ready")
    p.add_argument("--for", dest="wait", type=float, default=600, help="seconds to keep trying")
    args = ap.parse_args(argv)

    if args.cmd == "wait-ready":
        deadline, last = time.time() + args.wait, ""
        while time.time() < deadline:
            try:
                rpc(args.url, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                             "clientInfo": {"name": "mcp_call", "version": "1"}}, timeout=10)
                r = call(args.url, "execute_python",
                         {"code": "import slicer; __result = slicer.app.applicationVersion"}, timeout=30)
                print("Slicer", " ".join(_texts(r)), "ready at", args.url)
                return 0
            except (OSError, urllib.error.URLError, RuntimeError, ValueError) as e:
                if str(e) != last:
                    print(f"waiting: {e}", file=sys.stderr)
                    last = str(e)
                time.sleep(5)
        print(f"no MCP answer at {args.url} after {args.wait:.0f} s", file=sys.stderr)
        return 1

    if args.cmd == "tools":
        for t in rpc(args.url, "tools/list", timeout=args.timeout)["tools"]:
            print(f"{t['name']:24s} {(t.get('description') or '').splitlines()[0][:100]}")
        return 0

    if args.cmd == "screenshot":
        r = call(args.url, "screenshot", {}, timeout=args.timeout)
        img = next((c for c in r.get("content", []) if c.get("type") == "image"), None)
        if img is None:
            print("\n".join(_texts(r)) or "no image returned", file=sys.stderr)
            return 1
        with open(args.out, "wb") as f:
            f.write(base64.b64decode(img["data"]))
        print(args.out)
        return 0

    if args.cmd == "py":
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                code = f.read()
        elif args.code is not None:
            code = args.code
        else:
            code = sys.stdin.read()
        tool, arguments = "execute_python", {"code": code}
    else:
        tool, arguments = args.tool, json.loads(args.arguments)
    r = call(args.url, tool, arguments, timeout=args.timeout)
    print("\n".join(_texts(r)))
    return 1 if _failed(r) else 0


if __name__ == "__main__":
    sys.exit(main())
