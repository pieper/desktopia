#!/usr/bin/env python3
"""Format `vastai search offers --raw` JSON (read from stdin).

Usage:
  vastai search offers ... --raw | python3 scripts/offers.py list   # table, cheapest first
  vastai search offers ... --raw | python3 scripts/offers.py best   # one ID for `make up`

`best` = cheapest offer with PCIe >= MIN_PCIE (the readback path wants high PCIe);
falls back to cheapest overall if none qualify. Prints the chosen ID to stdout and a
one-line summary to stderr.
"""
import sys, json

MIN_PCIE = 23.0   # GB/s ~ PCIe 4.0 x16; our GPU->CPU->GPU readback crosses this each frame


def load():
    raw = sys.stdin.read().strip()
    if not raw:
        sys.exit("no offers returned (empty response from vastai)")
    try:
        xs = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit("vastai did not return JSON:\n" + raw[:500])
    xs.sort(key=lambda o: o.get("dph_total", 1e9))
    return xs


def fmt_table(xs):
    h = (f"{'ID':>9} {'$/hr':>6} {'Geo':<16} {'Up':>5} {'Dn':>5} {'Rel%':>4} "
         f"{'PCIe':>5} {'vCPU':>4} {'RAM':>5} {'Disk':>5} {'CUDA':>4}")
    print(h)
    print('-' * len(h))
    for o in xs[:25]:
        print(f"{o.get('id',0):>9} {o.get('dph_total',0):>6.3f} "
              f"{str(o.get('geolocation') or '')[:16]:<16} "
              f"{o.get('inet_up',0) or 0:>5.0f} {o.get('inet_down',0) or 0:>5.0f} "
              f"{(o.get('reliability2',0) or 0)*100:>4.0f} {o.get('pcie_bw',0) or 0:>5.1f} "
              f"{o.get('cpu_cores_effective',0) or 0:>4.0f} {(o.get('cpu_ram',0) or 0)/1024:>5.1f} "
              f"{o.get('disk_space',0) or 0:>5.0f} {str(o.get('cuda_max_good') or ''):>4}")
    sys.stdout.flush()
    print(f"\n{len(xs)} offers match the region/net filters. "
          f"Prefer PCIe >= {MIN_PCIE:.0f} for the readback path.", file=sys.stderr)


def fmt_best(xs):
    qualified = [o for o in xs if (o.get("pcie_bw", 0) or 0) >= MIN_PCIE]
    pool = qualified or xs
    o = pool[0]
    note = "" if qualified else f" (no PCIe>={MIN_PCIE:.0f}; cheapest overall)"
    print(f"best: id={o.get('id')} ${o.get('dph_total',0):.3f}/hr "
          f"{o.get('geolocation') or '?'} PCIe={o.get('pcie_bw',0) or 0:.1f} "
          f"up={o.get('inet_up',0) or 0:.0f}Mbps{note}", file=sys.stderr)
    print(o.get("id"))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "list"
    xs = load()
    if mode == "best":
        fmt_best(xs)
    else:
        fmt_table(xs)


if __name__ == "__main__":
    main()
