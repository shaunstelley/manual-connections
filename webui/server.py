#!/usr/bin/env python3
"""Local web UI for generating a PIA WireGuard client config (no manual editing needed)."""

import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import router_push

PORT = 8765
# Same default as get_region.sh's MAX_LATENCY=0.05 (50ms) — a server slower than
# this is treated as unreachable rather than just "slow". User-adjustable in the UI.
DEFAULT_LATENCY_MS = 50
MIN_LATENCY_MS = 1
MAX_LATENCY_MS = 10000
REPO_ROOT = Path(__file__).resolve().parent.parent
CONNECT_SCRIPT = REPO_ROOT / "connect_to_wireguard_with_token.sh"
INDEX_HTML = Path(__file__).resolve().parent / "index.html"
VALIDATE_HELPER = Path(__file__).resolve().parent / "validate_helper.sh"

SERVERLIST_URL = "https://serverlist.piaservers.net/vpninfo/servers/v6"
TOKEN_URL = "https://www.privateinternetaccess.com/api/client/v2/token"
# Cloudflare (fronting both PIA hosts) blocks Python's default "Python-urllib/3.x"
# User-Agent with a 403, even for requests that are otherwise identical to curl's
# (which get_token.sh uses, and which passes) — so PIA_USER/PIA_PASS with correct
# credentials would still fail. Any non-blocked UA works; curl's is the simplest
# match for the reference implementation.
USER_AGENT = "curl/8.7.1"


def fetch_regions():
    req = urllib.request.Request(SERVERLIST_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        first_line = resp.read().decode("utf-8").splitlines()[0]
    data = json.loads(first_line)
    return [r for r in data["regions"] if r.get("servers", {}).get("wg")]


def measure_latency(ip, port=443, timeout_ms=DEFAULT_LATENCY_MS):
    """TCP-connect latency, in ms. Same idea as get_region.sh's printServerLatency:
    a raw connect time to the region's meta server, no auth or PIA login involved."""
    start = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=timeout_ms / 1000):
            return round((time.monotonic() - start) * 1000)
    except OSError:
        return None


def fetch_regions_with_latency(timeout_ms=DEFAULT_LATENCY_MS):
    regions = fetch_regions()
    with ThreadPoolExecutor(max_workers=40) as pool:
        latencies = list(
            pool.map(lambda r: measure_latency(r["servers"]["meta"][0]["ip"], timeout_ms=timeout_ms), regions)
        )
    result = [
        {
            "id": r["id"],
            "name": r["name"],
            "port_forward": r.get("port_forward", False),
            "latency_ms": latency,
        }
        for r, latency in zip(regions, latencies)
    ]
    result.sort(key=lambda r: (r["latency_ms"] is None, r["latency_ms"]))
    return result


def multipart_body(fields):
    boundary = uuid.uuid4().hex
    lines = []
    for name, value in fields.items():
        lines.append(f"--{boundary}")
        lines.append(f'Content-Disposition: form-data; name="{name}"')
        lines.append("")
        lines.append(value)
    lines.append(f"--{boundary}--")
    lines.append("")
    body = "\r\n".join(lines).encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


def fetch_token(username, password):
    body, content_type = multipart_body({"username": username, "password": password})
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"PIA login failed: HTTP {e.code}")
    token = data.get("token")
    if not token:
        raise RuntimeError("PIA login failed: invalid username or password")
    return token


def generate_conf(username, password, region_id, include_dns):
    regions = fetch_regions()
    region = next((r for r in regions if r["id"] == region_id), None)
    if region is None:
        raise RuntimeError(f"Unknown region: {region_id}")

    token = fetch_token(username, password)
    wg = region["servers"]["wg"][0]

    fd, conf_path = tempfile.mkstemp(prefix="pia-", suffix=".conf")
    os.close(fd)
    try:
        env = dict(os.environ)
        env.update(
            {
                "PIA_TOKEN": token,
                "WG_SERVER_IP": wg["ip"],
                "WG_HOSTNAME": wg["cn"],
                "PIA_CONNECT": "false",
                "PIA_DNS": "true" if include_dns else "false",
                "PIA_CONF_PATH": conf_path,
            }
        )
        result = subprocess.run(
            ["bash", str(CONNECT_SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Config generation failed:\n" + (result.stderr or result.stdout or "unknown error")
            )
        return Path(conf_path).read_text(encoding="utf-8")
    finally:
        Path(conf_path).unlink(missing_ok=True)


def run_admin(args, timeout=90):
    """Run a command with macOS admin privileges via the native GUI/Touch ID
    authorization dialog, instead of a terminal `sudo` prompt. The password is
    handled entirely by macOS's own Security Server — it never passes through
    this process, the browser, or any HTTP request."""
    shell_cmd = " ".join(shlex.quote(str(a)) for a in args)
    as_escaped = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    script = f'do shell script "{as_escaped}" with administrator privileges'
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)


def validate_conf(conf_text):
    """Bring conf_text up as a real local WireGuard interface, force a packet
    so the lazy WireGuard handshake actually happens, confirm it via `wg show
    latest-handshakes`, then always tear the interface back down. This is the
    only way to actually prove a config connects — there's no way to check a
    handshake without establishing one. The whole sequence runs as one script
    (validate_helper.sh) behind a single admin-privileges prompt."""
    # wg-quick requires the config's basename (minus .conf) to be a valid
    # interface name: 1-15 chars matching [a-zA-Z0-9_=+.-]. "pia-" + 8 hex
    # chars (12 total) fits with room to spare — a mkstemp-style random
    # suffix on a "pia-validate-" prefix does not (21+ chars).
    iface = "pia-" + uuid.uuid4().hex[:8]
    conf_path = str(Path(tempfile.gettempdir()) / f"{iface}.conf")
    Path(conf_path).write_text(conf_text, encoding="utf-8")
    try:
        result = run_admin(["bash", str(VALIDATE_HELPER), conf_path, iface])
        if "User canceled" in (result.stderr or ""):
            return False, "Cancelled — the admin authorization prompt was dismissed."
        if result.returncode != 0:
            return False, "wg-quick up failed:\n" + (result.stderr or result.stdout or "unknown error")

        match = re.search(r"^\S+\t(\d+)\s*$", result.stdout, re.MULTILINE)
        handshake_ts = int(match.group(1)) if match else 0
        warning = next((l for l in result.stdout.splitlines() if l.startswith("WARNING")), None)

        if handshake_ts > 0:
            age = time.time() - handshake_ts
            message = f"Handshake confirmed ({age:.1f}s ago) — this config connects."
            return True, message + (f"\n{warning}" if warning else "")
        message = "wg-quick came up but no handshake was seen — the server may not be responding."
        return False, message + (f"\n{warning}" if warning else "")
    finally:
        Path(conf_path).unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type="text/plain; charset=utf-8", extra_headers=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._send(200, INDEX_HTML.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        elif parsed.path == "/api/regions":
            try:
                params = parse_qs(parsed.query)
                try:
                    timeout_ms = float(params.get("maxLatencyMs", [DEFAULT_LATENCY_MS])[0])
                except ValueError:
                    timeout_ms = DEFAULT_LATENCY_MS
                timeout_ms = max(MIN_LATENCY_MS, min(timeout_ms, MAX_LATENCY_MS))
                payload = json.dumps(fetch_regions_with_latency(timeout_ms))
                self._send(200, payload, "application/json")
            except Exception as e:
                self._send(502, f"Could not fetch region list: {e}")
        else:
            self._send(404, "Not found")

    def do_POST(self):
        if self.path == "/api/generate":
            self._handle_generate()
        elif self.path == "/api/router/test-login":
            self._handle_router_test_login()
        else:
            self._send(404, "Not found")

    def _handle_generate(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            conf = generate_conf(
                body["username"],
                body["password"],
                body["region"],
                bool(body.get("includeDns", True)),
            )
            # Test the exact file we're about to hand back — not a separately
            # generated twin — so a pass/fail actually describes this artifact.
            test_ok, test_message = validate_conf(conf)
            payload = json.dumps(
                {
                    "conf": conf,
                    "filename": f"pia-{body['region']}.conf",
                    "test": {"ok": test_ok, "message": test_message},
                }
            )
            self._send(200, payload, "application/json")
        except Exception as e:
            self._send(400, json.dumps({"error": str(e)}), "application/json")

    def _handle_router_test_login(self):
        # Only exercises router_push.login() -- confirms this Mac can reach
        # the router and that the documented auth flow actually works
        # against its real firmware. Does not touch any router configuration.
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                router_push.login(body["routerUrl"], body["username"], body["password"])
                result = {"ok": True, "message": "Login succeeded — session and CSRF token acquired."}
            except router_push.RouterLoginError as e:
                result = {"ok": False, "message": str(e)}
            self._send(200, json.dumps(result), "application/json")
        except Exception as e:
            self._send(400, json.dumps({"error": str(e)}), "application/json")


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"PIA config generator running at http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
