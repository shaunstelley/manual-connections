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
5. `webui/router_push.py`: new. Implements just the router auth flow from the `tmcpro/unifi-network-api` research below — `login(base_url, username, password)` does `POST /api/login` with local admin credentials over a stdlib `urllib` opener (self-signed cert on the router's local UI is expected and not verified — there's no public CA for a LAN-only address), extracts the CSRF token from response headers or cookies, and returns a cookie-carrying opener + token for subsequent calls. Raises `RouterLoginError` with a specific reason (unreachable vs. bad credentials vs. missing CSRF token) rather than swallowing failures.
   - Wired into `server.py` as `POST /api/router/test-login` (`{routerUrl, username, password}` → `{ok, message}`), and into `webui/index.html` as a new "Router login (test only)" section below the existing form. Credentials follow the same discipline as the PIA ones: used only for that one request, never persisted or logged.
   - Verified only against a deliberately unreachable address (`https://192.0.2.1` — TEST-NET-1) to confirm the error path works cleanly end-to-end (times out, surfaces a clear message, re-enables the form). **Not yet tested against a real router** — this dev machine wasn't on the router's network when this was built. That's the next concrete step: connect, open the webui, and try "Test Login" for real.

## What's next: push the generated config to the router automatically

The last manual step is importing the generated `.conf` into the Dream Router's native **WireGuard VPN Client** feature (Settings → VPN in the UniFi Network app — confirmed via Ubiquiti's own help docs to accept either an uploaded config file or manually-filled fields).

That feature is **controller-managed**, distinct from the older "hand-edit `/etc/wireguard/wg0.conf` over SSH" approach some UniFi guides describe. That distinction matters: community projects that manipulate UniFi OS network config *outside* its own controller (e.g. `split-vpn`) have to actively fight the controller reverting their changes. Whatever approach gets built here should go through whatever the official UI itself calls — **not** SSH/raw config-file editing.

The problem: nobody publicly documents the actual API call that UI action makes. Checked and came up empty on the specific payload shape: Ubiquiti's own docs, the reverse-engineered [tmcpro/unifi-network-api](https://github.com/tmcpro/unifi-network-api) spec, and the [awesome-unifi](https://github.com/wolffcatskyy/awesome-unifi) list. What **is** confirmed from `tmcpro/unifi-network-api`, and now implemented in `router_push.login()`: the local controller uses session-cookie + CSRF-token auth (`POST /api/login`, then a CSRF header on mutating `/v2/api/site/{site}/...` calls). Local admin login (not just cloud/SSO) is expected to work for this specific router, so that's the auth path used — no Ubiquiti cloud OAuth. This auth flow itself is still unverified against the real router (see above) — the "confirmed" claim is about the published spec, not a live test by this code.

### Immediate next step: verify the login flow for real

Next time this Mac (or whichever machine runs the webui) is on the router's LAN:

1. Run `python3 webui/server.py`, open the "Router login (test only)" section, fill in the router's real local URL + admin credentials, click **Test Login**.
2. If it fails, the error message should say why (unreachable / bad login / missing CSRF token). A missing-CSRF-token failure most likely means this router's firmware returns it differently than `tmcpro/unifi-network-api` documents — the message points at using the Browser pane's `read_network_requests` (or manual DevTools) to inspect the real login response and fix `router_push.login()` accordingly.
3. Agents with browser-automation tools (e.g. a Browser pane that can navigate the local network, fill in fields, and read network requests/response bodies) may be able to do the capture in step 4 below directly, with the human still typing the actual admin password into the browser — worth trying before falling back to fully manual DevTools capture.

### Blocking on the human, not the agent

Before real push code can be written, someone with access to the router still needs to do a one-time capture of the **add-VPN-client** request specifically (the login request is now handled by `router_push.login()` above, pending the real-router verification in step 1-2):

1. Log out of the Network app's local UI, open browser DevTools → Network tab, and *keep it open* while logging back in with the local admin account — this captures the login request/response (cookie + CSRF token) in the same trace, useful for cross-checking `router_push.login()`.
2. Add one WireGuard VPN Client entry (upload a `.conf` or fill fields manually).
3. Right-click the add-VPN-client request in the Network tab → **Copy as cURL**, and hand it over.
4. Note: the captured command contains a live session cookie/CSRF token — short-lived, but real auth material for the router until it expires.

### Once that capture exists

- Add the real request shape to `webui/router_push.py` as e.g. `apply_wireguard_client(opener, csrf_token, base_url, site, conf_fields)`, built from the captured payload with the *generated* PIA config's real fields (private key, peer public key, endpoint, allowed IPs, DNS) substituted in for whatever was entered during capture.
- **Defaults to preview-only**: constructs and displays the exact request it would send, without sending it. Actually applying it to the router should be a separate, explicitly-confirmed action — this pushes to live home-network infrastructure, so it deserves more caution than the local handshake test does, since a bad push here is more persistent than a temporary local tunnel.
- Wire into `webui/index.html` as a step that only appears after "Generate & Test Config" succeeds: router login (already built) → preview the add-client request → explicit second confirmation → apply.

**Explicitly rejected approaches** (don't revisit without a new reason): SSH/raw `/etc/wireguard` file editing (bypasses the controller, risks reversion — see `split-vpn` precedent above); cloud/SSO-based auth (unnecessary — local admin is confirmed available and simpler).
