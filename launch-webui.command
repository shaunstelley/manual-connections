#!/bin/bash
cd "$(dirname "$0")"
( sleep 1 && open http://localhost:8765 ) &
exec python3 webui/server.py
