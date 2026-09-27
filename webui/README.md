# Local web UI

Generates a PIA WireGuard config and adds it to a UniFi router (tested on a Dream Router) as a WireGuard VPN client, optionally routing chosen networks or devices through it.

## Setup (macOS)

    brew install jq wireguard-tools

curl and python3 already ship with macOS.

## Run

Double-click `launch-webui.command` in the repo root. It starts the server and opens the page. Or from a terminal:

    python3 webui/server.py

then open http://localhost:8765. Close the Terminal window (or press Ctrl+C) to stop it.

## Steps

1. **Generate config**: PIA login, region, and optionally a local test that briefly brings the tunnel up on this Mac to confirm it handshakes (macOS asks for your password or Touch ID). **Download .conf** saves the file if you want it.
2. **Connect to router**: router address and local admin login. A Ubiquiti SSO-linked account asks for an emailed verification code as a second step.
3. **Add to router**: name the entry, optionally tick networks or devices to send through the VPN (with the kill switch on, the router's default), then **Add to router**. "Show request details" shows the exact requests before they're sent.

## Privacy

- The server listens on `127.0.0.1` only.
- PIA and router passwords are used for the one request that needs them and are never logged or written to disk.
- The router session is kept in memory for 10 minutes, so steps 2 and 3 don't need a second MFA code.
- The browser remembers the router address and username (in localStorage), not the password.
