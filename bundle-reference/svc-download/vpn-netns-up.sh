#!/usr/bin/env bash
# vpn-netns-up.sh — construct the "vpn" network namespace jail.
#
# Design: wg0 is CREATED in the init namespace (so its encrypted UDP socket
# egresses via normal host routing / eth0), then MOVED into the netns. The
# cleartext side of the tunnel exists only inside the jail. The jail's only
# egress is wg0; a veth /30 provides host->jail access for admin UIs.
# Leaks are unroutable by construction — no firewall rules required for
# fail-closed behavior.
#
# Idempotent: safe to re-run; tears down partial state first.
set -euo pipefail

NS=vpn
WG_IF=wg0
WG_CONF=/etc/wireguard/peer.conf        # wg(8) syntax ONLY — see peer.conf.example
WG_ADDR=""                              # tunnel address, e.g. 10.13.128.7/32 (from provider)
WG_MTU=1420
VETH_HOST=veth-host
VETH_NS=veth-vpn
VETH_HOST_IP=10.77.0.1/30
VETH_NS_IP=10.77.0.2/30

# Load overrides (WG_ADDR is mandatory)
[[ -f /etc/vpn-netns.env ]] && source /etc/vpn-netns.env
[[ -n "${WG_ADDR}" ]] || { echo "FATAL: set WG_ADDR in /etc/vpn-netns.env" >&2; exit 1; }
[[ -f "${WG_CONF}" ]]  || { echo "FATAL: ${WG_CONF} missing" >&2; exit 1; }

# --- cleanup of any partial prior state (idempotency) ---
ip netns del "${NS}" 2>/dev/null || true          # deletes wg0/veth peer inside it
ip link del "${WG_IF}" 2>/dev/null || true        # in case wg0 was left in init ns
ip link del "${VETH_HOST}" 2>/dev/null || true

# --- namespace ---
ip netns add "${NS}"
ip -n "${NS}" link set lo up

# --- WireGuard: create in init ns (socket birthplace), configure, move ---
ip link add "${WG_IF}" type wireguard
wg setconf "${WG_IF}" "${WG_CONF}"
ip link set "${WG_IF}" netns "${NS}"
ip -n "${NS}" addr add "${WG_ADDR}" dev "${WG_IF}"
ip -n "${NS}" link set "${WG_IF}" mtu "${WG_MTU}" up
ip -n "${NS}" route add default dev "${WG_IF}"

# --- veth for admin-UI access; jail never learns a route to the LAN ---
ip link add "${VETH_HOST}" type veth peer name "${VETH_NS}"
ip link set "${VETH_NS}" netns "${NS}"
ip addr add "${VETH_HOST_IP}" dev "${VETH_HOST}"
ip link set "${VETH_HOST}" up
ip -n "${NS}" addr add "${VETH_NS_IP}" dev "${VETH_NS}"
ip -n "${NS}" link set "${VETH_NS}" up

# --- belt-and-suspenders inside the jail (cheap, static, never reloaded) ---
# Not the enforcement mechanism; just refuses forwarding/martians if a future
# change ever adds an interface to this ns.
ip netns exec "${NS}" sysctl -qw net.ipv4.ip_forward=0 \
    net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1

# --- assertions: fail the unit loudly if the topology is wrong ---
ip -n "${NS}" route show default | grep -q "dev ${WG_IF}" \
    || { echo "FATAL: default route in ${NS} is not ${WG_IF}" >&2; exit 1; }
if ip -n "${NS}" route show | grep -Ev "dev (lo|${WG_IF}|${VETH_NS})" | grep -q .; then
    echo "FATAL: unexpected route in ${NS}" >&2; exit 1
fi

logger -t vpn-netns "namespace '${NS}' up: default via ${WG_IF}, admin veth ${VETH_NS_IP}"
