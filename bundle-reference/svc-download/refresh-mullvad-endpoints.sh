#!/usr/bin/env bash
# refresh-mullvad-endpoints.sh — regenerate /etc/nftables/mullvad-endpoints.nft
# from the Endpoint(s) in the WireGuard config, then atomically reload the set.
#
# Why: host-backstop.nft permits the WG handshake only to known Mullvad IPs.
# If you swap Mullvad servers (edit Endpoint + restart vpn-netns), this keeps
# the backstop in sync so the new handshake isn't dropped. Mullvad endpoints
# are numeric IPs already, so this is a parse, not a DNS lookup — no external
# dependency, safe to run from a timer even when egress is locked down.
set -euo pipefail

WG_CONF=${1:-/etc/wireguard/peer.conf}
OUT=/etc/nftables/mullvad-endpoints.nft
TMP=$(mktemp)

# Extract IPv4 Endpoint hosts (strip :port). Supports multiple [Peer] blocks.
mapfile -t ips < <(grep -iE '^\s*Endpoint\s*=' "$WG_CONF" \
    | sed -E 's/.*=\s*([0-9.]+):[0-9]+.*/\1/' | sort -u)

[[ ${#ips[@]} -gt 0 ]] || { echo "FATAL: no numeric Endpoint found in $WG_CONF" >&2; exit 1; }

{
    echo "# generated $(date -Is) from $WG_CONF — do not edit by hand"
    echo "add element inet host_backstop mullvad_wg { $(IFS=,; echo "${ips[*]}") }"
} > "$TMP"

# Load the table first if absent (idempotent), then flush+repopulate the set.
nft list table inet host_backstop &>/dev/null || nft -f /etc/nftables/host-backstop.nft
nft flush set inet host_backstop mullvad_wg
install -m 644 "$TMP" "$OUT"
nft -f "$OUT"
rm -f "$TMP"
logger -t mullvad-endpoints "refreshed: ${ips[*]}"
echo "OK: ${ips[*]}"
