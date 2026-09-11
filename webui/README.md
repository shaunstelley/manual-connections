# Local web UI

Generates a ready-to-import PIA WireGuard client config (correct `Address = .../32` and `DNS =` line, no manual editing) for loading into a router's WireGuard VPN client. The `.conf` is standard wg-quick format.

## Setup (macOS)

    brew install jq wireguard-tools

curl and python3 already ship with macOS.

## Run

    python3 webui/server.py

Then open http://127.0.0.1:8765, enter your PIA username/password, pick a region, and click **Generate Config**. The browser downloads the finished `.conf` file directly.

The server only listens on `127.0.0.1` (not reachable from other devices on your network), and credentials are used in-memory for the single login request only — never logged or written to disk.
