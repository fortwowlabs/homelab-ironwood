#!/usr/bin/env bash
# leak-canary.sh — continuous verification of the jail invariants.
# Run by leak-canary.timer every 15 min. Converts "we leak-tested it once in
# July" into an enforced property.
#
# Checks, in order of severity:
#   1. Every dl-* container is actually inside /run/netns/vpn (SandboxKey).
#   2. The jail's default route is still wg0 and no foreign routes appeared.
#   3. Egress IP observed from inside the jail matches the provider.
# A stalled curl (tunnel down) is NOT a failure — that's fail-closed working.
# A *wrong* IP or a container outside the jail is a leak: stop the stack, alert.
set -uo pipefail


fail() {
    logger -p auth.crit -t leak-canary "LEAK: $1 — stopping download stack"
    systemctl stop 'dl-*.service' 2>/dev/null
    # Hook your notification of choice here (ntfy/mail/webhook via wg0 only).
    exit 1
}

# 1. Containers must live in the jail. SandboxKey is the netns path Podman
#    joined; anything else means a unit drifted (e.g. someone removed
#    PodmanArgs and it silently fell back to the default bridge).
want_inode=$(stat -Lc %i /run/netns/vpn) || fail "netns /run/netns/vpn missing"
for c in $(podman ps --filter name='^(sabnzbd|nzbhydra2|prowlarr|sonarr|radarr)$' -q); do
    key=$(podman inspect -f '{{.NetworkSettings.SandboxKey}}' "$c")
    [[ -n "$key" ]] || fail "container $c has no sandbox netns"
    [[ "$(stat -Lc %i "$key" 2>/dev/null)" == "$want_inode" ]] \
        || fail "container $c is NOT in the vpn netns (sandbox: $key)"
done

# 2. Topology drift.
ip -n vpn route show default | grep -q 'dev wg0' \
    || fail "default route in netns is no longer wg0"
ip -n vpn route show | grep -Ev 'dev (lo|wg0|veth-vpn)' | grep -q . \
    && fail "unexpected route present in netns"

# 3. Egress identity. Timeout/stall = tunnel down = fail-closed = OK.
egress=$(ip netns exec vpn curl -4 -s --max-time 10 https://am.i.mullvad.net/connected || true)
if [[ -n "$egress" && "$egress" != *"You are connected to Mullvad"* ]]; then
    fail "egress check says NOT Mullvad: $egress"
fi

logger -t leak-canary "OK (egress=${egress:-tunnel-down/fail-closed})"
exit 0
