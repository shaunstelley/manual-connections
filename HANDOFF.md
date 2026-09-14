# Handoff notes

This is a personal fork of [pia-foss/manual-connections](https://github.com/pia-foss/manual-connections) for generating a PIA WireGuard config for a Ubiquiti Dream Router (UDR), with a local web UI, plus (in progress) automation to push that config directly to the router.

- Fork: `shaunstelley/manual-connections` (`origin`), `upstream` = `pia-foss/manual-connections`
- Branch: `feature/wireguard-config-webui` (pushed, up to date as of this commit)
- Developed on Windows, **meant to run on macOS** — `webui/` and `validate_helper.sh` assume `wg-quick`, `wg`, `curl`, `jq`, and macOS's `osascript` specifically (for admin-privileges prompts).

## What's done

1. `connect_to_wireguard_with_token.sh`: `Address` now gets a `/32` suffix. Was missing before — harmless for the CLI's own `wg-quick up` path (which already defaults a bare IP to `/32` internally) but broke import into a router's own, less forgiving config parser.
2. `run_setup.sh`: the `resolvconf`-presence check now skips on Darwin. `wg-quick`'s macOS implementation sets DNS via `networksetup`, not `resolvconf`/`openresolv` — the old check falsely reported DNS as unsupported on every Mac, silently disabling `PIA_DNS`.
3. `webui/` — local web UI:
   - `server.py`: Python 3, stdlib only, binds `127.0.0.1` only. `GET /api/regions` lists WireGuard-capable PIA regions with concurrent TCP-connect latency (default 50ms timeout, matching the CLI's `MAX_LATENCY` default; adjustable via `?maxLatencyMs=`). `POST /api/generate` builds a config **and** immediately validates it in the same request, returning both the file and the test result together — it tests the literal artifact handed back, not a separately-generated twin.
   - `validate_helper.sh`: brings the generated conf up as a real local WireGuard interface, forces a handshake (WireGuard only handshakes lazily, on actual traffic), confirms via `wg show <iface> latest-handshakes`, always tears back down regardless of outcome. Invoked via `osascript ... with administrator privileges` (native macOS Touch ID-capable dialog) rather than `sudo`, specifically so the Mac's real login password never passes through this server or the browser.
   - `index.html`: single-page form driving the "Generate & Test Config" flow above.
   - `README.md`: setup (`brew install jq wireguard-tools`, then `python3 webui/server.py`).
4. Verified end-to-end on macOS (Generate & Test Config → real download → real handshake). Three bugs found and fixed along the way:
   - `server.py`'s PIA requests (`fetch_regions`, `fetch_token`) used Python's default `urllib` User-Agent (`Python-urllib/3.x`); Cloudflare (fronting both PIA hosts) blocks it with a `403`, even for an otherwise-identical request `curl` sends successfully. Fixed by sending a `curl`-matching User-Agent on both requests.
   - `validate_conf()`'s temp config file used `tempfile.mkstemp(prefix="pia-validate-", ...)`; the resulting basename (minus `.conf`) becomes the WireGuard interface name, and `wg-quick` requires that to be ≤15 chars — the old prefix alone was already 13, plus a random suffix. Fixed by generating a short `pia-<8 hex chars>` name directly.
   - `validate_helper.sh` queried `wg show <iface> ...` using the *friendly* config name. On macOS, `wg-quick` picks a real kernel interface (`utun8`, etc.) at runtime and only `wg-quick` itself (not the raw `wg` binary) knows the friendly-name → real-name mapping — so every `wg show` call failed with "Unable to access interface", reporting "no handshake" even when the tunnel was actually up and working (confirmed via a real `curl` request that got a genuine `200` through the tunnel). Fixed by parsing the real interface name out of `wg-quick up`'s own output and querying that instead.
   - Also added `-4` to the validation `curl` probe (IPv6 DNS could otherwise let it bypass the IPv4-only tunnel and give a false pass) and a rich diagnostic log at `/tmp/pia-validate-diag.log` for future debugging.

## What's next: push the generated config to the router automatically

The last manual step is importing the generated `.conf` into the Dream Router's native **WireGuard VPN Client** feature (Settings → VPN in the UniFi Network app — confirmed via Ubiquiti's own help docs to accept either an uploaded config file or manually-filled fields).

That feature is **controller-managed**, distinct from the older "hand-edit `/etc/wireguard/wg0.conf` over SSH" approach some UniFi guides describe. That distinction matters: community projects that manipulate UniFi OS network config *outside* its own controller (e.g. `split-vpn`) have to actively fight the controller reverting their changes. Whatever approach gets built here should go through whatever the official UI itself calls — **not** SSH/raw config-file editing.

The problem: nobody publicly documents the actual API call that UI action makes. Checked and came up empty on the specific payload shape: Ubiquiti's own docs, the reverse-engineered [tmcpro/unifi-network-api](https://github.com/tmcpro/unifi-network-api) spec, and the [awesome-unifi](https://github.com/wolffcatskyy/awesome-unifi) list. What **is** confirmed from `tmcpro/unifi-network-api`: the local controller uses session-cookie + CSRF-token auth (`POST /api/login`, then a CSRF header on mutating `/v2/api/site/{site}/...` calls). Local admin login (not just cloud/SSO) is confirmed to work for this specific router, so that's the auth path to use — no need for Ubiquiti cloud OAuth.

### Blocking on the human, not the agent

Before real push code can be written, someone with access to the router needs to do a one-time capture:

1. Log out of the Network app's local UI, open browser DevTools → Network tab, and *keep it open* while logging back in with the local admin account — this captures the login request/response (cookie + CSRF token) in the same trace.
2. Add one WireGuard VPN Client entry (upload a `.conf` or fill fields manually).
3. Right-click the login request and the add-VPN-client request in the Network tab → **Copy as cURL** for each, and hand both over.
4. Note: those captured commands contain a live session cookie/CSRF token — short-lived, but real auth material for the router until it expires.

### Once that capture exists

- New `webui/router_push.py` (or a new endpoint in `server.py` — decide once the real request shape is known): takes router hostname/IP + local admin username/password, entered fresh per use and never persisted (same discipline as the PIA credentials already in `server.py`).
- Replicates the captured login call to get a session cookie + CSRF token, then replicates the captured add-VPN-client call with the *generated* PIA config's real fields (private key, peer public key, endpoint, allowed IPs, DNS) substituted for whatever was entered during capture.
- **Defaults to preview-only**: constructs and displays the exact request it would send, without sending it. Actually applying it to the router should be a separate, explicitly-confirmed action — this pushes to live home-network infrastructure, so it deserves more caution than the local handshake test does, since a bad push here is more persistent than a temporary local tunnel.
- Wire into `webui/index.html` as a step that only appears after "Generate & Test Config" succeeds: enter router IP + local admin login → preview the request → explicit second confirmation → apply.

**Explicitly rejected approaches** (don't revisit without a new reason): SSH/raw `/etc/wireguard` file editing (bypasses the controller, risks reversion — see `split-vpn` precedent above); cloud/SSO-based auth (unnecessary — local admin is confirmed available and simpler).
