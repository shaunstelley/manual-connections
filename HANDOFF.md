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
5. `webui/router_push.py`: new. Implements the router auth flow — **verified against the real Dream Router**, not just the published spec. Superseded the `tmcpro/unifi-network-api` assumptions in two ways a real HAR capture (Safari Web Inspector, two logins) revealed:
   - Real endpoint is `POST /api/auth/login`, not `/api/login`. Mutating calls proxy through `/proxy/network/v2/api/site/{site}/...` (site slug confirmed as `default` via a captured `GET .../vpn/users` call), not the bare `/v2/api/site/{site}/...` originally assumed.
   - This account is Ubiquiti-SSO-linked and can require a second request with an emailed one-time code (`"token"` field in the login body) before succeeding — a captured attempt returned HTTP `499` with `{"code":"MFA_AUTH_REQUIRED", "data":{"authenticators":[...]}}` on the first try, then `200` on a second try with the code filled in. `login()` now takes an optional `mfa_token` and raises `MfaRequiredError` (carrying the account's `authenticators` list) when one's needed.
   - A bare login POST with no prior cookie got `401` — a captured real login already carried a `JSESSIONID` cookie from an earlier, uncaptured page load. Fixed by having `login()` prime a session with a GET to `base_url + "/"` before POSTing credentials.
   - CSRF token comes back as both `x-csrf-token` and `x-updated-csrf-token` response headers (same value at login time — a later mutating call's response may only carry the `-updated-` one if the token rotates; use that going forward once available).
   - Wired into `server.py` as `POST /api/router/test-login` (`{routerUrl, username, password, mfaToken}` → `{ok, mfaRequired, message}`), and into `webui/index.html` as a "Router login (test only)" section with an MFA code field that appears on demand. Credentials follow the same discipline as the PIA ones: used only for that one request, never persisted or logged.
   - **Confirmed working end-to-end against the real router**: `Login succeeded — session and CSRF token acquired.` (MFA wasn't re-prompted on this run, likely a recent-verification grace period from the Safari session used for the HAR capture — the `MfaRequiredError` path is implemented and ready if it resurfaces.)

## What's next: push the generated config to the router automatically

The last manual step is importing the generated `.conf` into the Dream Router's native **WireGuard VPN Client** feature (Settings → VPN in the UniFi Network app — confirmed via Ubiquiti's own help docs to accept either an uploaded config file or manually-filled fields).

That feature is **controller-managed**, distinct from the older "hand-edit `/etc/wireguard/wg0.conf` over SSH" approach some UniFi guides describe. That distinction matters: community projects that manipulate UniFi OS network config *outside* its own controller (e.g. `split-vpn`) have to actively fight the controller reverting their changes. Whatever approach gets built here should go through whatever the official UI itself calls — **not** SSH/raw config-file editing. (Re-raised and re-confirmed during this phase of work — still no new reason to revisit it: SSH could be worth a *look*, purely diagnostic, to check whether some local CLI on the device goes through the controller rather than around it, but that hasn't been checked and shouldn't be assumed to exist.)

Login is now solved (see above). What's left is the **add-VPN-client** request specifically — nobody publicly documents its endpoint or payload shape (checked: Ubiquiti's own docs, the reverse-engineered [tmcpro/unifi-network-api](https://github.com/tmcpro/unifi-network-api) spec, and the [awesome-unifi](https://github.com/wolffcatskyy/awesome-unifi) list all come up empty). A captured `GET /proxy/network/v2/api/site/default/vpn/users` (200) confirms VPN endpoints live under that path, but a GET-to-list isn't the same shape as the POST/PUT needed to add a client — that still needs its own real capture.

### Next concrete step: capture the add-VPN-client request

Same method as the login capture (Safari Web Inspector, Network tab, Preserve Log on):

1. In the Network app, go to Settings → VPN → add one WireGuard VPN Client entry (upload the generated `.conf` or fill fields manually — either way is fine, only the request shape matters).
2. Find that request in the Network tab (look for a POST/PUT under `/proxy/network/v2/api/site/default/...`, likely near `vpn/users` or similar given the GET above) and export as HAR, or Copy as cURL.
3. **Redact before sharing**: if fields carry secret material (the WireGuard private key, in particular) that's fine to see once for building the client — but flag it explicitly, since it's more sensitive than the login password (which should still be redacted/never shared).

### Once that capture exists

- Add the real request shape to `webui/router_push.py` as e.g. `apply_wireguard_client(opener, csrf_token, base_url, site, conf_fields)`, built from the captured payload with the *generated* PIA config's real fields (private key, peer public key, endpoint, allowed IPs, DNS) substituted in for whatever was entered during capture.
- **Defaults to preview-only**: constructs and displays the exact request it would send, without sending it. Actually applying it to the router should be a separate, explicitly-confirmed action — this pushes to live home-network infrastructure, so it deserves more caution than the local handshake test does, since a bad push here is more persistent than a temporary local tunnel.
- Wire into `webui/index.html` as a step that only appears after "Generate & Test Config" succeeds: router login (already built) → preview the add-client request → explicit second confirmation → apply.

**Explicitly rejected approaches** (don't revisit without a new reason): SSH/raw `/etc/wireguard` file editing (bypasses the controller, risks reversion — see `split-vpn` precedent above). Note: an earlier version of this doc also rejected cloud/SSO-based auth as "unnecessary — local admin is confirmed available and simpler" — that was wrong. This account's local admin login *is* SSO-linked and needs it (see `router_push.login()`'s MFA handling above); there was no local-only path to avoid it.
