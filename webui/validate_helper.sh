#!/usr/bin/env bash
# Brings a WireGuard conf up, forces and reports a real handshake, then always
# tears back down. Run as one script (rather than separate up/show/down calls)
# so the whole sequence happens behind a single admin-privileges prompt.
#
# Usage: validate_helper.sh <conf_path> <iface_name>
#
# Exit code reflects only whether `wg-quick up` itself succeeded — that's the
# one failure that matters to the caller. Teardown problems are reported as a
# warning line instead, since they shouldn't be reported as the test failing.
set -u
conf_path="$1"
iface="$2"

wg-quick up "$conf_path" > /tmp/pia-validate-up.log 2>&1
up_rc=$?

if [[ $up_rc -eq 0 ]]; then
  curl -s -m 8 -o /dev/null https://www.privateinternetaccess.com || true
  wg show "$iface" latest-handshakes
fi

wg-quick down "$conf_path" > /tmp/pia-validate-down.log 2>&1
down_rc=$?
if [[ $down_rc -ne 0 ]]; then
  echo "WARNING: wg-quick down failed (exit $down_rc) - tunnel may still be active, run: sudo wg-quick down $iface"
fi

exit $up_rc
