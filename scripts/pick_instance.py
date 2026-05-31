#!/usr/bin/env python3
"""Pick the Desktopia instance id from `vastai show instances-v1 --raw` (stdin).

argv = image substrings that identify "our" instances (BASE_IMAGE, GHCR_IMAGE, "desktopia").
Prefers instances whose image matches one of those; among the pool, picks the newest
(highest id). Warns on stderr when several instances exist so a wrong pick (esp. for
`down`, which destroys) is visible. Set DESKTOPIA_INSTANCE to override entirely.
"""
import sys, json


def instances(data):
    if isinstance(data, dict):
        return data.get("instances") or data.get("results") or data.get("data") or []
    return data if isinstance(data, list) else []


def image_of(i):
    return " ".join(str(i.get(k, "")) for k in ("image_uuid", "image", "image_runtype"))


def main():
    subs = [s for s in sys.argv[1:] if s]
    data = json.loads(sys.stdin.read() or "[]")
    xs = instances(data)
    if not xs:
        sys.exit("no instances (launch one with `make up-best`)")
    matches = [i for i in xs if any(s in image_of(i) for s in subs)]
    pool = matches or xs
    chosen = max(pool, key=lambda i: i.get("id", 0) or 0)
    if len(xs) > 1:
        kind = "matched our image" if matches else "NO image match — newest of all"
        print(f"note: selected instance {chosen.get('id')} ({kind}); "
              f"{len(xs)} instances exist. Set DESKTOPIA_INSTANCE to override.", file=sys.stderr)
    print(chosen.get("id"))


if __name__ == "__main__":
    main()
