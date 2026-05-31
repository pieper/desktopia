#!/usr/bin/env python3
"""Format `vastai search offers --raw` JSON (read from stdin), ranked by estimated
latency tier then price.

Usage:
  vastai search offers ... --raw | python3 scripts/offers.py list   # table
  vastai search offers ... --raw | python3 scripts/offers.py best   # one ID for `make up`

Ranking: each offer's `geolocation` ("US, Texas" / "CA, Quebec") is mapped to a centroid,
great-circle distance to your origin gives an estimated RTT, offers are bucketed into
TIER_MS-wide latency tiers, and within a tier the cheaper wins. So a closer host always
beats a farther one, but among comparably-close hosts cost decides.

Origin defaults to Boston; override with DESKTOPIA_ORIGIN="lat,lon".
The RTT number is a coarse *model estimate* (distance + routing overhead), not a measurement
— confirm with a real ping once an instance is up.
"""
import sys, os, json, math

MIN_PCIE = 23.0   # GB/s ~ PCIe 4.0 x16; our GPU->CPU->GPU readback crosses this each frame
TIER_MS  = 20.0   # latency bucket width; within a bucket, price decides
# RTT model: rtt_ms = dist_km * (2 / 200 km-per-ms) * 1.6 routing-overhead + 5 ms fixed
RTT_PER_KM = (2.0 / 200.0) * 1.6
RTT_FIXED  = 5.0
UNKNOWN_RTT = 999.0

ORIGIN = (42.36, -71.06)  # Boston
_env = os.environ.get("DESKTOPIA_ORIGIN")
if _env:
    try:
        ORIGIN = tuple(float(x) for x in _env.split(",")[:2])
    except ValueError:
        pass

# Centroids (lat, lon). US states / DC keyed by lowercase full name.
US = {
 "alabama":(32.8,-86.8),"alaska":(64.0,-152.0),"arizona":(34.2,-111.7),"arkansas":(34.9,-92.4),
 "california":(37.2,-119.4),"colorado":(39.0,-105.5),"connecticut":(41.6,-72.7),"delaware":(39.0,-75.5),
 "florida":(28.6,-82.4),"georgia":(32.6,-83.4),"hawaii":(20.3,-156.4),"idaho":(44.3,-114.6),
 "illinois":(40.0,-89.2),"indiana":(39.9,-86.3),"iowa":(42.0,-93.5),"kansas":(38.5,-98.4),
 "kentucky":(37.5,-85.3),"louisiana":(31.0,-92.0),"maine":(45.4,-69.2),"maryland":(39.0,-76.8),
 "massachusetts":(42.3,-71.8),"michigan":(44.3,-85.4),"minnesota":(46.3,-94.3),"mississippi":(32.7,-89.7),
 "missouri":(38.4,-92.5),"montana":(47.0,-109.6),"nebraska":(41.5,-99.8),"nevada":(39.3,-116.6),
 "new hampshire":(43.7,-71.6),"new jersey":(40.2,-74.7),"new mexico":(34.4,-106.1),"new york":(42.9,-75.6),
 "north carolina":(35.6,-79.4),"north dakota":(47.5,-100.3),"ohio":(40.3,-82.8),"oklahoma":(35.6,-97.5),
 "oregon":(44.0,-120.5),"pennsylvania":(40.9,-77.8),"rhode island":(41.7,-71.5),"south carolina":(33.9,-80.9),
 "south dakota":(44.4,-100.2),"tennessee":(35.9,-86.4),"texas":(31.5,-99.3),"utah":(39.3,-111.7),
 "vermont":(44.1,-72.7),"virginia":(37.5,-78.9),"washington":(47.4,-120.4),"west virginia":(38.6,-80.6),
 "wisconsin":(44.6,-89.9),"wyoming":(43.0,-107.5),"district of columbia":(38.9,-77.0),
}
# Canadian provinces, anchored near the major metro / datacenter rather than geographic centroid.
CAN = {
 "alberta":(51.0,-114.0),"british columbia":(49.2,-123.1),"manitoba":(49.9,-97.1),
 "new brunswick":(45.9,-66.5),"newfoundland and labrador":(47.6,-52.7),"nova scotia":(44.6,-63.6),
 "ontario":(43.7,-79.4),"prince edward island":(46.2,-63.1),"quebec":(45.5,-73.6),
 "saskatchewan":(50.4,-104.6),"yukon":(60.7,-135.1),"northwest territories":(62.5,-114.4),"nunavut":(63.7,-68.5),
}
US_CENTER, CAN_CENTER = (39.8,-98.6), (56.0,-106.0)
# Country centroids by ISO-2 (vast.ai's country token), for non-US/CA offers.
COUNTRY = {
 "US":US_CENTER,"CA":CAN_CENTER,"MX":(23.6,-102.5),"BR":(-10.0,-52.0),"AR":(-38.4,-63.6),"CL":(-35.7,-71.5),
 "GB":(54.0,-2.0),"IE":(53.4,-8.0),"FR":(46.6,2.2),"DE":(51.2,10.4),"NL":(52.1,5.3),"BE":(50.6,4.7),
 "SE":(59.3,18.1),"NO":(60.5,8.5),"FI":(61.9,25.7),"DK":(56.0,9.5),"IS":(64.9,-19.0),"PL":(51.9,19.1),
 "CZ":(49.8,15.5),"AT":(47.6,14.1),"CH":(46.8,8.2),"ES":(40.2,-3.7),"PT":(39.5,-8.0),"IT":(41.9,12.6),
 "RO":(45.9,24.9),"UA":(48.4,31.2),"EE":(58.6,25.0),"LT":(55.2,23.9),"LV":(56.9,24.6),"RU":(61.5,105.0),
 "TR":(39.0,35.2),"IL":(31.0,34.8),"AE":(24.0,54.0),"IN":(22.0,79.0),"CN":(35.9,104.2),"HK":(22.3,114.2),
 "TW":(23.7,121.0),"KR":(36.5,127.9),"JP":(36.2,138.3),"SG":(1.35,103.8),"VN":(16.0,108.0),"ID":(-2.5,118.0),
 "AU":(-25.3,133.8),"NZ":(-41.0,174.0),"ZA":(-29.0,24.0),
}


def haversine(a, b):
    (la1, lo1), (la2, lo2) = a, b
    r1, r2 = math.radians(la1), math.radians(la2)
    dla, dlo = math.radians(la2 - la1), math.radians(lo2 - lo1)
    h = math.sin(dla/2)**2 + math.cos(r1)*math.cos(r2)*math.sin(dlo/2)**2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def coords_for(geo):
    if not geo:
        return None
    parts = [p.strip() for p in str(geo).split(",")]
    country = parts[0].upper()
    region = parts[1].lower() if len(parts) > 1 else ""
    if country in ("US", "USA", "UNITED STATES"):
        return US.get(region, US_CENTER)
    if country in ("CA", "CAN", "CANADA"):
        return CAN.get(region, CAN_CENTER)
    return COUNTRY.get(country)


def est_rtt(o):
    c = coords_for(o.get("geolocation"))
    if c is None:
        return UNKNOWN_RTT
    return haversine(ORIGIN, c) * RTT_PER_KM + RTT_FIXED


def tier(rtt):
    return int(rtt // TIER_MS)


def load():
    raw = sys.stdin.read().strip()
    if not raw:
        sys.exit("no offers returned (empty response from vastai)")
    try:
        xs = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit("vastai did not return JSON:\n" + raw[:500])
    for o in xs:
        o["_rtt"] = est_rtt(o)
    # closest tier first, then cheapest within the tier
    xs.sort(key=lambda o: (tier(o["_rtt"]), o.get("dph_total", 1e9)))
    return xs


def fmt_table(xs):
    h = (f"{'ID':>9} {'~ms':>4} {'$/hr':>6} {'Geo':<16} {'Up':>5} {'Dn':>5} {'Rel%':>4} "
         f"{'PCIe':>5} {'vCPU':>4} {'RAM':>5} {'Disk':>5} {'CUDA':>4}")
    print(h)
    print('-' * len(h))
    for o in xs[:25]:
        rtt = o["_rtt"]
        ms = f"{rtt:.0f}" if rtt < UNKNOWN_RTT else "  ?"
        print(f"{o.get('id',0):>9} {ms:>4} {o.get('dph_total',0):>6.3f} "
              f"{str(o.get('geolocation') or '')[:16]:<16} "
              f"{o.get('inet_up',0) or 0:>5.0f} {o.get('inet_down',0) or 0:>5.0f} "
              f"{(o.get('reliability2',0) or 0)*100:>4.0f} {o.get('pcie_bw',0) or 0:>5.1f} "
              f"{o.get('cpu_cores_effective',0) or 0:>4.0f} {(o.get('cpu_ram',0) or 0)/1024:>5.1f} "
              f"{o.get('disk_space',0) or 0:>5.0f} {str(o.get('cuda_max_good') or ''):>4}")
    sys.stdout.flush()
    print(f"\n{len(xs)} offers, ranked by ~{TIER_MS:.0f}ms latency tier then price "
          f"(origin {ORIGIN[0]:.2f},{ORIGIN[1]:.2f}). ~ms is a model estimate, not a ping. "
          f"Prefer PCIe >= {MIN_PCIE:.0f}.", file=sys.stderr)


def fmt_best(xs):
    qualified = [o for o in xs if (o.get("pcie_bw", 0) or 0) >= MIN_PCIE]
    pool = qualified or xs
    o = pool[0]   # xs is already (tier, price)-sorted, so [0] is closest-then-cheapest
    note = "" if qualified else f" (no PCIe>={MIN_PCIE:.0f}; relaxed)"
    print(f"best: id={o.get('id')} ~{o['_rtt']:.0f}ms ${o.get('dph_total',0):.3f}/hr "
          f"{o.get('geolocation') or '?'} PCIe={o.get('pcie_bw',0) or 0:.1f} "
          f"up={o.get('inet_up',0) or 0:.0f}Mbps{note}", file=sys.stderr)
    print(o.get("id"))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "list"
    xs = load()
    (fmt_best if mode == "best" else fmt_table)(xs)


if __name__ == "__main__":
    main()
