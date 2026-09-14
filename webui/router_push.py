"""UniFi Network app (local controller) API client for pushing a generated
WireGuard config to a Dream Router's native VPN Client feature.

Only the auth flow (login) is implemented here. Confirmed by capturing a
real login against a Dream Router running UniFi OS (HAR export, see
HANDOFF.md) -- this superseded an earlier assumption based on the
reverse-engineered github.com/tmcpro/unifi-network-api spec, which described
a plain local-admin login. That assumption turned out to be wrong for an
account tied to Ubiquiti's SSO: real endpoint is POST /api/auth/login (not
/api/login), and it can come back requiring a second request with an MFA
code before it succeeds. Mutating calls proxy through
/proxy/network/v2/api/site/{site}/... (not /v2/api/site/{site}/... as
previously assumed) and need the CSRF token echoed back as a header.

The actual "add WireGuard VPN Client" request -- the part that would let
apply_wireguard_client() exist -- is deliberately NOT implemented. Nobody
publicly documents its endpoint or payload shape (checked: Ubiquiti's own
docs, tmcpro/unifi-network-api, and the awesome-unifi list all come up
empty), and a GET .../vpn/users seen in the same capture confirms VPN
endpoints live under that path but doesn't show the add-client shape. See
HANDOFF.md for how to capture the real request.
"""

import http.cookiejar
import json
import ssl
import urllib.error
import urllib.request


class RouterLoginError(RuntimeError):
    pass


class MfaRequiredError(RouterLoginError):
    """Login needs a second call to login() with mfa_token set. authenticators
    is the account's enrolled MFA methods, as the server reported them, so a
    caller can decide what to prompt for. This client only actually drives
    a numeric-code method (email or TOTP, sent as the `token` field) -- not
    WebAuthn/passkey, which needs real browser credential-manager UI."""

    def __init__(self, message, authenticators=None):
        super().__init__(message)
        self.authenticators = authenticators or []


def login(base_url, username, password, mfa_token="", verify_tls=False, timeout=10):
    """Authenticate to the router's local Network app (UniFi OS console).

    Returns (opener, csrf_token): an urllib opener carrying the session
    cookie for use on subsequent requests, and the CSRF token string to send
    as a header on mutating calls. username/password/mfa_token are used only
    for this one request; this function does not persist them anywhere.

    Raises MfaRequiredError if the account needs a second call with
    mfa_token set (e.g. to a one-time code emailed to the account) -- call
    login() again with the same username/password plus that code.
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

    # A bare login POST with no prior session gets rejected with a plain 401
    # rather than reaching the actual credential/MFA check -- confirmed via
    # a real captured login, whose request already carried a JSESSIONID
    # cookie set by an earlier, uncaptured page load. Prime one first.
    try:
        with opener.open(f"{base_url}/", timeout=timeout) as resp:
            resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError):
        # Any Set-Cookie header on the response is processed by
        # HTTPCookieProcessor regardless of status, so a non-2xx/redirect
        # here doesn't necessarily mean priming failed -- let the real
        # login attempt below surface the actual problem if it did.
        pass

    body = json.dumps(
        {
            "username": username,
            "password": password,
            "token": mfa_token,
            "rememberMe": True,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/auth/login",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "*/*"},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = resp.status
            headers = resp.headers
            resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        if payload.get("code") == "MFA_AUTH_REQUIRED":
            raise MfaRequiredError(
                "This account needs a verification code to finish logging in "
                "(check your email for a one-time code, then try again with it).",
                authenticators=payload.get("data", {}).get("authenticators", []),
            ) from e
        message = payload.get("message") or f"HTTP {e.code}"
        raise RouterLoginError(f"Login failed: {message}") from e
    except urllib.error.URLError as e:
        raise RouterLoginError(f"Could not reach router at {base_url}: {e.reason}") from e

    if status != 200:
        raise RouterLoginError(f"Login failed: HTTP {status}")

    # Seen as both x-csrf-token and x-updated-csrf-token on a real login
    # response (same value at login time; a mutating call's response may
    # only carry x-updated-csrf-token if the token rotates -- watch for
    # that header on every subsequent response and use its value going
    # forward instead of the one returned here).
    csrf_token = headers.get("X-Csrf-Token") or headers.get("X-Updated-Csrf-Token")
    if not csrf_token:
        for cookie in jar:
            if "csrf" in cookie.name.lower():
                csrf_token = cookie.value
                break
    if not csrf_token:
        raise RouterLoginError(
            "Login succeeded (HTTP 200) but no CSRF token was found in the response "
            "headers or cookies."
        )

    return opener, csrf_token
