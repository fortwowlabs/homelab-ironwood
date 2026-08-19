#!/bin/bash
# homelab-iac managed — roles/svc_infra/files/chat-egress-apply.sh
#
# Loads, and keeps loaded, the chat egress nftables policy.
#
# It exists because `socket cgroupv2` matches an INODE, not a path. nft
# resolves the path once at load time; systemd gives open-webui a new cgroup
# inode on every restart. So a rule loaded before the last restart matches
# nothing, while the unit stays active, the table stays listed and the drop
# counter reads a perfectly healthy 0. See the header of chat-egress.nft.j2.
#
# Two modes:
#   --wait     boot path. nft REFUSES to load a ruleset naming a cgroup that
#              does not exist yet ("cgroupv2 path fails: No such file or
#              directory", exit 1), and multi-user.target is reached ~1.5-2s
#              before the rootless user manager has created open-webui's
#              cgroup. Without the wait the loader fails at every single boot
#              and open-webui comes up with unrestricted egress.
#   --refresh  timer path. Re-applies only when the rule has actually gone
#              stale, so the drop counter is not reset on every tick — Task 5's
#              probe reads that counter.
#
# Configuration is sourced here rather than taken from the unit's
# EnvironmentFile, following the zed/smartd lesson in CLAUDE.md: a hook that
# assumes its caller's environment gets an unset variable and dies on `set -u`.
set -euo pipefail

CONFIG=/etc/nftables/chat-egress.env
if [ ! -r "$CONFIG" ]; then
    echo "chat-egress: cannot read $CONFIG" >&2
    exit 1
fi
# shellcheck source=/dev/null
. "$CONFIG"

: "${CHAT_EGRESS_POLICY:?not set in $CONFIG}"
: "${CHAT_EGRESS_CGROUP:?not set in $CONFIG}"
: "${CHAT_EGRESS_WAIT_SECONDS:?not set in $CONFIG}"

NFT=/usr/sbin/nft
CGROUP_DIR="/sys/fs/cgroup/${CHAT_EGRESS_CGROUP}"

# nft prints the quoted path back while the inode still resolves to it, and a
# bare integer once it does not. That is the whole staleness test, and it costs
# one list call.
rule_is_fresh() {
    "$NFT" list table inet chat_egress 2>/dev/null \
        | grep -qF "\"${CHAT_EGRESS_CGROUP}\""
}

case "${1:---refresh}" in
--wait)
    waited=0
    while [ ! -d "$CGROUP_DIR" ] && [ "$waited" -lt "$CHAT_EGRESS_WAIT_SECONDS" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if [ ! -d "$CGROUP_DIR" ]; then
        echo "chat-egress: $CGROUP_DIR did not appear within ${CHAT_EGRESS_WAIT_SECONDS}s" >&2
        echo "chat-egress: refusing to load a policy that would match nothing" >&2
        exit 1
    fi
    "$NFT" -f "$CHAT_EGRESS_POLICY"
    echo "applied after ${waited}s"
    ;;
--refresh)
    # An admin who stopped the loader means it. Without this the timer would
    # silently re-arm the policy a minute later and `systemctl stop
    # chat-egress` would not be a working off switch.
    if ! systemctl is-active --quiet chat-egress.service; then
        echo "loader is not active; not re-applying"
        exit 0
    fi
    if [ ! -d "$CGROUP_DIR" ]; then
        # open-webui is down, so there is no egress to constrain. Withdraw a
        # stale rule rather than leaving it: it is bound to a freed inode, and
        # a recycled inode would drop some other cgroup's traffic. Withdrawing
        # is not a fail-open — a stale rule matches nothing either way.
        if rule_is_fresh; then
            echo "cgroup absent but rule still resolves; left alone"
        else
            "$NFT" delete table inet chat_egress 2>/dev/null || true
            echo "withdrawn: open-webui is not running and the rule was stale"
        fi
        exit 0
    fi
    if rule_is_fresh; then
        echo "fresh"
        exit 0
    fi
    "$NFT" -f "$CHAT_EGRESS_POLICY"
    echo "reapplied: open-webui's cgroup was recreated"
    ;;
*)
    echo "usage: ${0##*/} --wait|--refresh" >&2
    exit 2
    ;;
esac
