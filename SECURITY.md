# Security model

Desktopia runs on rented, multi-tenant GPU hosts (vast.ai today). This file records
the access-control decisions so they aren't lost to chat history.

## Threat model (what we're actually defending against)

- **Leaked vast.ai API key** → unauthorized instance spend / account changes.
- **Leaked instance credentials** (SSH, the WebTransport cert) → someone else views/drives
  the running desktop.
- **Data at rest on an untrusted host** → DTLS/QUIC protects data *in transit*, not bytes
  sitting on someone else's disk or in GPU memory. **Use anonymized / synthetic / research
  data only.** Real PHI requires HIPAA-compliant infra + a signed BAA, not vast.ai.
- The `mcp-slicer` / `execute_python` surface is **arbitrary code execution by design** — the
  container is the trust boundary. Treat the whole instance as untrusted-after-launch.

## vast.ai API key — minimal scope

vast.ai keys default to **full account access**. Always create a *scoped* key for the CLI loop.
The key is **one-time-viewable** at creation — copy it immediately into your keychain/secret
manager; it cannot be re-read.

Console (`cloud.vast.ai/manage-keys/?tab=api-keys` → **+ New**) — set exactly this:

| Type | Read | Write |
|---|---|---|
| **User** | ✅ | ⬜️ off |
| **Instances** | ✅ | ✅ |
| **Billing/Earning** | ⬜️ off | ⬜️ off |
| **Miscellaneous** | ⬜️ off | — |

Master **"Require 2FA for this key"**: **off** (see 2FA section). Name it `desktopia`.

CLI equivalent (needs an existing full key once):

```bash
vastai create api-key --name desktopia \
  --permissions '{"manage_instances": true, "manage_billing": false}'
```

**Rationale**
- `Instances Read+Write` = the entire `vast.sh` loop (search / create / show / logs / ssh-url / destroy).
- `User Read` only — **never User Write**: Write there can manage SSH keys and *mint other API
  keys*, letting a leaked key escalate past its own scope.
- `Billing/Earning` fully off — renting is covered by Instances Write; Billing Write is the
  dangerous one (payment methods / withdrawals).
- If `search`/`logs` ever returns an insufficient-permissions error, enable in order and
  re-test: (1) Miscellaneous → Enabled, then (2) Billing/Earning → Read (never Write).

## Spend cap = prepaid balance, not key scope

There is **no scope that allows "launch but not spend"** — creating an instance *is* spending.
So the financial blast radius is bounded by the **prepaid account balance**, not the key.
Keep a low balance (top up in small increments, e.g. $20–40). A leaked Instances key can at
most burn the loaded credit. Pair with a **dedicated low-limit / virtual card** on the account.

## 2FA — where it helps vs. where it hurts

vast.ai supports per-key and per-permission 2FA.

- **On the CLI/automation key: leave 2FA OFF.** 2FA gates the *action*; with 2FA on Instances
  Write, every `make up` / `make down` would block on an interactive code and break
  non-interactive runs. Security for this key comes from scope + low balance + secrecy.
- **On your web login: ON.** Interactive, once per device — zero CLI friction (the CLI auths
  with the bearer key, never prompts for an OTP).
- **On the email account used for vast.ai: ON (TOTP or passkey).** Account-recovery-via-email
  is the real takeover path; this is the highest-leverage 2FA.
- **On any full-access admin key you keep:** 2FA-gate it and use it rarely.

## Instance / streaming hygiene

- **`destroy` when done** (`make down`) — stops billing and disposes of any data on the host.
- The WebTransport server uses a **self-signed ECDSA P-256 cert, ≤14-day validity**, pinned
  client-side via `serverCertificateHashes`. It's per-instance and ephemeral — fine, but it
  means anyone with the IP:PORT *and* the cert hash can connect. Don't post both publicly.
- vast.ai maps `4433/udp` to a public `IP:PORT`. That endpoint is internet-reachable; the only
  gate is the cert hash. For a stronger gate, add an app-level token check in `server.py`
  before adding a session to the broadcaster.
- Rotate the API key ~every 90 days; revoke by name if a laptop is lost.

## Secrets — what lives where

- **API key:** stored by `vastai set api-key` in `~/.config/vastai/` (or `~/.vast_api_key`),
  **not** in this repo. Never commit it. `vast.sh` reads it via the CLI, never embeds it.
- **WebTransport cert/key:** minted on the instance at boot in `/tmp`, never leaves it.
  `*.pem` is gitignored.
- **GHCR:** pushed by GitHub Actions using the workflow's `GITHUB_TOKEN` — no PAT stored.
