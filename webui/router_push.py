"""UniFi Network app (local controller) API client for pushing a generated
WireGuard config to a Dream Router's native VPN Client feature.

Only the auth flow (login) is implemented here. It follows the
reverse-engineered spec at github.com/tmcpro/unifi-network-api: POST
/api/login with local admin credentials sets a session cookie and returns a
CSRF token; mutating /v2/api/site/{site}/... calls must echo that token back
as a header. Local admin login (as opposed to cloud/SSO) is expected to work
for a Dream Router, per that same research -- but this hasn't yet been
tested against a real router by this code, only by hand in a browser.

The actual "add WireGuard VPN Client" request -- the part that would let
apply_wireguard_client() exist -- is deliberately NOT implemented. Nobody
publicly documents its endpoint or payload shape (checked: Ubiquiti's own
docs, tmcpro/unifi-network-api, and the awesome-unifi list all come up
empty). Guessing a payload here would produce code that fails silently or,
worse, "succeeds" against the wrong endpoint. See HANDOFF.md for how to
capture the real request once you have access to the router: either by hand
(DevTools -> Copy as cURL) or by driving the Network app's local UI through
a browser automation tool and reading its network requests.
"""

import http.cookiejar
import json
import ssl
import urllib.error
import urllib.request


class RouterLoginError(RuntimeError):
    pass


def login(base_url, username, password, verify_tls=False, timeout=10):
    """Authenticate to the router's local Network app.

    Returns (opener, csrf_token): an urllib opener carrying the session
    cookie for use on subsequent requests, and the CSRF token string to send
    as a header on mutating calls. username/password are used only for this
    one request; this function does not persist them anywhere.
    """
    base_url = base_url.rstrip("/")
    jar = http.cookiejar.CookieJar()
    ctx = ssl.create_default_context()
    if not verify_tls:
        # A Dream Router's local UI serves a self-signed cert by default --
        # there's no public CA to verify a LAN-only address against.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ctx),
    )

    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/login",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = resp.status
            headers = resp.headers
            resp.read()
    except urllib.error.HTTPError as e:
        raise RouterLoginError(f"Login failed: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RouterLoginError(f"Could not reach router at {base_url}: {e.reason}") from e

    if status != 200:
        raise RouterLoginError(f"Login failed: HTTP {status}")

    csrf_token = headers.get("X-CSRF-Token") or headers.get("x-csrf-token")
    if not csrf_token:
        # Some firmware versions may hand it back as a cookie instead.
        for cookie in jar:
            if "csrf" in cookie.name.lower():
                csrf_token = cookie.value
                break
    if not csrf_token:
        raise RouterLoginError(
            "Login succeeded (HTTP 200) but no CSRF token was found in the response "
            "headers or cookies. This router's firmware may return it differently "
            "than github.com/tmcpro/unifi-network-api documents -- inspect the raw "
            "response (e.g. via the Browser pane's read_network_requests) to find it."
        )

    return opener, csrf_token
