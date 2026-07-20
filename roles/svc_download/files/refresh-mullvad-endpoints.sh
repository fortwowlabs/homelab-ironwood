#!/usr/bin/env bash
# refresh-mullvad-endpoints.sh — regenerate /etc/nftables/mullvad-endpoints.nft
# from the Endpoint(s) in the WireGuard config, then atomically reload BOTH
# sets (endpoint IPs and endpoint PORTS).
#
# Why: host-backstop.nft permits the WG handshake only to known Mullvad
# IP:port pairs. If you swap Mullvad servers or the generated config uses a
# non-51820 port (they can), this keeps the backstop in sync so the new
# handshake isn't dropped. Endpoints are numeric IPs by design — this is a
# parse, not a DNS lookup; safe to run even when egress is locked down.
set -euo pipefail

WG_CONF=${1:-/etc/wireguard/peer.conf}
OUT=/etc/nftables/mullvad-endpoints.nft
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

# Extract IPv4 Endpoint hosts and ports. Supports multiple [Peer] blocks.
mapfile -t ips   < <(grep -iE '^\s*Endpoint\s*=' "$WG_CONF" \
    | sed -E 's/.*=\s*([0-9.]+):[0-9]+.*/\1/' | sort -u)
mapfile -t ports < <(grep -iE '^\s*Endpoint\s*=' "$WG_CONF" \
    | sed -E 's/.*=\s*[0-9.]+:([0-9]+).*/\1/' | sort -u)

[[ ${#ips[@]} -gt 0 && ${#ports[@]} -gt 0 ]] \
    || { echo "FATAL: no numeric IP:port Endpoint found in $WG_CONF" >&2; exit 1; }

{
    echo "# generated from $WG_CONF — do not edit by hand"
    echo "add element inet host_backstop mullvad_wg { $(IFS=,; echo "${ips[*]}") }"
    echo "add element inet host_backstop mullvad_ports { $(IFS=,; echo "${ports[*]}") }"
} > "$TMP"

# Load the table first if absent. Avoid rewriting the generated file when its
# semantic content is unchanged so steady-state convergence remains clean —
# but only declare convergence if the kernel set actually still holds the
# endpoints. If the runtime set was flushed out-of-band, fall through and
# reload so this periodic refresh keeps its self-healing property.
nft list table inet host_backstop &>/dev/null || nft -f /etc/nftables/host-backstop.nft
if [[ -f "$OUT" ]] && cmp -s "$TMP" "$OUT" \
   && nft list set inet host_backstop mullvad_wg 2>/dev/null | grep -qF "${ips[0]}"; then
    echo "unchanged: ips=${ips[*]} ports=${ports[*]}"
    exit 0
fi

nft flush set inet host_backstop mullvad_wg
nft flush set inet host_backstop mullvad_ports
install -m 644 "$TMP" "$OUT"
nft -f "$OUT"
logger -t mullvad-endpoints "refreshed: ips=${ips[*]} ports=${ports[*]}"
echo "changed: ips=${ips[*]} ports=${ports[*]}"
