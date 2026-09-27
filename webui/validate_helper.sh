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
  # On macOS the raw `wg` binary only knows the real kernel interface
  # (e.g. utun8), not the friendly config name — wg-quick prints the mapping.
  real_iface="$(sed -n "s/.*Interface for .* is \(.*\)/\1/p" /tmp/pia-validate-up.log | tail -1)"
  [[ -z $real_iface ]] && real_iface="$iface"

  # -4: the tunnel is IPv4-only, so an IPv6 request would bypass it and never
  # trigger WireGuard's lazy handshake.
  curl -4 -s -m 8 -o /dev/null https://www.privateinternetaccess.com || true
  for _ in 1 2 3 4 5; do
    ts="$(wg show "$real_iface" latest-handshakes | awk '{print $2}')"
    [[ -n $ts && $ts -gt 0 ]] && break
    sleep 1
  done
  wg show "$real_iface" latest-handshakes
else
  # Surface the real failure reason on stdout/stderr (what the caller sees) —
  # it would otherwise be stranded in this log file, leaving only the
  # unrelated "wg-quick down" teardown warning below visible to the caller.
  cat /tmp/pia-validate-up.log >&2
fi

wg-quick down "$conf_path" > /tmp/pia-validate-down.log 2>&1
down_rc=$?
if [[ $down_rc -ne 0 ]]; then
  echo "WARNING: wg-quick down failed (exit $down_rc) - tunnel may still be active, run: sudo wg-quick down $iface"
fi

exit $up_rc
