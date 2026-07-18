#!/usr/bin/env bash
# vpn-netns-down.sh — full rollback of the jail.
# Deleting the netns destroys wg0 and the veth peer inside it; the host-side
# veth dies with its peer. Returns the VM to a stock box in one step.
set -uo pipefail
ip netns del vpn 2>/dev/null || true
ip link del wg0 2>/dev/null || true
ip link del veth-host 2>/dev/null || true
logger -t vpn-netns "namespace 'vpn' torn down"
exit 0
