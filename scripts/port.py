#!/usr/bin/env python3
"""Print the public IP:PORT mapped to 4433/udp from `vastai show instance <id> --raw` (stdin)."""
import sys, json

d = json.loads(sys.stdin.read() or "{}")
if isinstance(d, list):
    d = d[0] if d else {}
if isinstance(d, dict) and "instances" in d:
    xs = d["instances"]; d = xs[0] if xs else {}

ip = (d.get("public_ipaddr") or d.get("public_ip") or "").strip()
ports = d.get("ports") or {}
m = ports.get("4433/udp") or ports.get("4433/tcp")
if ip and m and m[0].get("HostPort"):
    print(f"{ip}:{m[0]['HostPort']}")
else:
    sys.exit(f"4433/udp not mapped yet (ip={ip!r}, mapped ports={list(ports)})")
