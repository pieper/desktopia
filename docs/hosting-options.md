# Desktopia hosting options — research notes

Reference notes (researched 2026-06-06) on where to run Desktopia: vast.ai (current), the National
Research Platform / MGHPCC, vs Jetstream2. Covers what's knowable about hosts, who provides them, and
the one real blocker for non-vast hosts (public UDP for our QUIC/WebTransport transport).

---

## 1. vast.ai — what's knowable about hosts / datacenters

Two pools, set by **query filters** (`datacenter`, `verified`) → the **result field is `hosting_type`**
(`1` = datacenter/"Secure Cloud", `0` = community). Verified pool ≈ **11% datacenter / 89% community**.

**Location/identity fields (offer keys):**
| Field | Meaning | Locality value |
|---|---|---|
| `geolocation` | "California, US" — country + often state, **never city** | coarse |
| `geolocode` | integer **country-only** hash | ❌ useless sub-country |
| `public_ipaddr` | host egress IP | ✅ **best** — geo-IP for city/ASN/RTT |
| `machine_id` | the physical box (finest identity) | ✅ pin a warmed box |
| `host_id` | the provider (owns many machines) | ✅ |
| `cluster_id` | a host's shared-LAN site | ✅ strongest, but only ~1.5% populated |
| `static_ip` | IP stable across restarts (~51%) | ✅ warm-spare reconnect |
| `direct_port_count` | open ports (>0 = direct, non-proxy) | ✅ **hard filter for QUIC** |
| `inet_up`/`inet_down` | host bandwidth Mb/s | ✅ |
| `reliability`(=`reliability2`) | 0–1 future-uptime estimate | DC median .996 vs community .994 |

**"Datacenter" (`hosting_type=1`) certifies:** registered business + verified owners + ≥5 GPU servers +
≥1 cert (ISO 27001) + "increased reliability scoring." **Not exposed:** tier (2/3), redundancy, SLA,
operator name/address, city. **`verified`** is fully automated (reliability, infra/GPU config, DLPerf,
~2.6 Mb/s per GiB VRAM bandwidth) — **not** a security audit; individual hosts aren't audited.

**NOT knowable at all:** DC operator/address, tier/SLA, city, **whether a host has our image cached**
(no field), host age/reputation. (`metrics gpu-locations` exists but needs `machine_read` perm — 403 for renter keys.)

**"Same datacenter" proxy ranking:** same `machine_id` > same `cluster_id` > same `host_id`+`geolocation`
> same `public_ipaddr`/subnet > same region. **Pin a warmed box with a local volume on a chosen `machine_id`**
(volumes are machine-bound and force re-placement onto that machine → co-locates data + docker cache).
Good selection filter: `reliability>0.98 inet_up>1000 direct_port_count>100 static_ip=True` + geo-IP the top IPs.

## 2. vast.ai — who the providers actually are

**Mostly individuals renting hardware they already own, not serious datacenter businesses** — with a
growing professional tier on top. ("Airbnb for GPUs.")
- **Bottom (bulk by host count):** gamers w/ spare 3090/4090, homelabbers, **ex-crypto miners** who
  pivoted idle rigs after Ethereum's 2022 PoS Merge. ~$0.30–0.60/GPU-hr, ~$500–1,400/mo passive income.
- **Middle:** small colos / small AI shops monetizing spare capacity.
- **Top (minority of hosts, growing share of high-end GPU-hrs):** vetted Secure-Cloud DC partners & pro
  farms (often ex-Bitcoin miners w/ real power) running H100/H200 on AI contracts.
- Evidence: founder Jake Cannell says early base was *primarily crypto miners*; a journalist found clusters
  "in some cases the owners' **garages**"; vast rents consumer RTX 4090s AWS won't (only possible because
  individuals supply them); ~17k GPUs / ~1,400 hosts ≈ **~12 GPUs/host** (long tail of small operators).
- Consequence = the reliability complaints we hit (instances vanish, GPUs reclaimed, "reliability tax"
  ~20–55% over list on unverified hosts). The Quebec box that failed = a community host. Our
  `reliability>0.98` + retry-on-bad-host + (optionally) `datacenter=true` is the right mitigation.

## 3. National Research Platform (NRP/Nautilus) + MGHPCC — assessment

**NRP = a shared multi-tenant Kubernetes cluster** (75+ sites, 420+ nodes, 1,400+ GPUs), **free for
research/education**, CILogon access + `kubectl` in a namespace. Containers/pods, **not VMs** like Jetstream2.

- **MGHPCC is an NRP partner** (co-operated by SDSC, **MGHPCC**, UNL) with GPUs physically in **Holyoke, MA
  — closest site to a Boston user**. Pin via `topology.kubernetes.io/region` (East Coast) + `nvidia.com/gpu.product`.
- **Image caching = the boot-time win we want:** node containerd cache (`IfNotPresent`) + in-cluster registry
  `gitlab-registry.nrp-nautilus.io` → warm nodes skip the multi-GB pull (vast re-pulls per rent). Caveat:
  cache is per-node; pin region+GPU to keep landing on warm nodes.
- **Rendering + NVENC work:** NRP already runs GPU EGL desktops streamed to browser (Selkies-GStreamer) in
  **unprivileged** pods; **NVENC is NOT blocked** (unlike vast). Our render-node-only Wayland design fits the
  unprivileged model; `renderD128`/GBM specifics may need a small admin device exception — smoke-test.
- **Policy constraints:** bare pods die at 6h; GPU pods can't be Deployments by default; >40% GPU util
  required; idle `sleep` jobs get you banned. → fits **"spin up a 6h session pod on demand"**, not "always warm."
- **THE BLOCKER:** NRP exposes only **HTTP/HTTPS via a shared L7 ingress — no public UDP/TCP, no public pod
  IPs, no NodePort by default.** So our **QUIC/WebTransport on UDP 4433 with a pinned self-signed cert can't
  be exposed as-is.** (Jetstream2 *does* allow it: OpenStack IaaS gives per-tenant floating IP + security groups.)

## 4. Can NRP be persuaded to allow a UDP port? (researched 2026-06-06)

**Relaxable default policy, NOT a fundamental constraint.**
- Their ingress doc literally says *"In general we don't allow exposing non-http applications via TCP ports,
  but if you really need to do that, **contact us on Matrix**"* — default-with-exception-path, not "forbidden."
- Their admin docs design nodes to be **"open to the world"** with a **central Calico GlobalNetworkPolicy**
  firewall — so opening one more UDP port is a config rule, not a redesign.
- **Decisive precedent: NRP already runs public UDP itself** — a public **coTURN** server (`turn.nrp-nautilus.io`)
  relays its own Selkies/WebRTC GPU desktops to browsers **over UDP**, and **perfSONAR** uses UDP node-to-node.
  So they've already accepted the public-UDP risk class for *the same use case* — just centralized.
- **On the JS2 analogy:** weak as logic (JS2 = IaaS per-tenant IP+security-group; NRP = shared PaaS ingress),
  useful only as "the research-cloud community treats user UDP as normal." **The strong argument is internal:
  NRP itself already does public UDP (coTURN, perfSONAR).**

**Most-likely-approved mechanism:** a **UDP NodePort or hostPort on a public-IP node, scoped to one namespace,
opened via a Calico GlobalNetworkPolicy rule**; offer **dual-stack with IPv6-only as a fallback** (NRP is
IPv6-heavy → no IPv4 burned; catch: client browser needs IPv6, and WebTransport has **no TCP fallback**).
LoadBalancer/MetalLB (burns IPv4) = least likely.

**The ask (essence):** "NRP already relays interactive GPU desktops to browsers over public UDP via
`turn.nrp-nautilus.io`. I run an equivalent (3D Slicer medical-imaging research) over QUIC/WebTransport, which
needs one public UDP port and — unlike WebRTC — has no TURN-over-TCP fallback, so it can't use the L7 ingress.
Requesting **one UDP port, scoped to namespace X**, via NodePort/hostPort + a Calico rule. Concessions: IPv6-only
if IPv4 is the blocker; time-boxed; or I'll relay through your existing coTURN instead."

## 5. Net recommendation
- **vast.ai** — keep for the raw-QUIC transport we've built; filter reliability/static-ip/direct-ports; pin
  warmed boxes by `machine_id`+local volume. Supply is flaky long-tail → retry + datacenter tier when it matters.
- **NRP @ MGHPCC** — very attractive (free, fast cached boot, working NVENC, lowest latency for Boston). Two paths:
  (1) add a **WebRTC+TURN transport** to Desktopia (use NRP's coTURN; works today, no admin ask), or
  (2) **ask admins for one UDP port** (well-founded; internal precedent strong). Fastest PoC: run Slicer inside
  NRP's existing `nvidia-egl-desktop` Selkies container.
- **Jetstream2** — the easy UDP path (IaaS floating IP), but farther (Indiana) and VM-not-container.
- **Transport abstraction** is the strategic unlock: a QUIC path (vast/JS2) + a WebRTC/TURN path (NRP) lets the
  same desktop run anywhere.
