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
  # On macOS, wg-quick maps the friendly config name to a real kernel
  # interface (e.g. "utun8") it picks at runtime — only wg-quick itself
  # understands that mapping. The raw `wg` binary needs the real name, or
  # every query below fails with "Unable to access interface" regardless of
  # whether the tunnel is actually up. wg-quick prints the mapping as part
  # of `up`, so pull it from there instead of guessing.
  real_iface="$(sed -n "s/.*Interface for .* is \(.*\)/\1/p" /tmp/pia-validate-up.log | tail -1)"
  [[ -z $real_iface ]] && real_iface="$iface"

  {
    echo "--- diag $(date) ---"
    # -4: PIA WireGuard is IPv4-only (AllowedIPs = 0.0.0.0/0 has no IPv6 route).
    # On a dual-stack network, plain curl can reach the internet over IPv6
    # directly, bypassing the tunnel entirely — passing "no handshake" even
    # though the tunnel itself is fine, because no traffic ever tests it.
    curl -4 -sv -m 8 -o /dev/null https://www.privateinternetaccess.com 2>&1
    echo "curl_rc=$?"
    # A handshake can take a moment on the first packet; poll instead of
    # checking once immediately after curl returns.
    for i in 1 2 3 4 5 6; do
      hs="$(wg show "$real_iface" latest-handshakes)"
      ts="$(echo "$hs" | awk '{print $2}')"
      echo "poll $i: $hs"
      [[ -n $ts && $ts -gt 0 ]] && break
      sleep 1
    done
    wg show "$real_iface" transfer
    wg show "$real_iface" endpoints
    netstat -rn -f inet | grep -E "^default|utun" || true
  } > /tmp/pia-validate-diag.log 2>&1
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
