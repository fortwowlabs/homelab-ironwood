#!/usr/bin/env bash
# pve-diskguard.sh — ntfy alarm when a ZFS pool or the PVE root/vz filesystem
# crosses a capacity threshold. This is the guard that would have caught the
# 2026-07-20 rpool-full outage BEFORE svc-download froze. Install on the PVE
# host (thurgadin) via the systemd timer in contrib/systemd/. Never exits
# nonzero — a failed push must not mark the unit failed.
set -uo pipefail

# Config file: NTFY_URL, NTFY_TOPIC, THRESH, NTFY_TOKEN (optional).
# shellcheck source=/dev/null
[ -f /etc/homelab-diskguard.env ] && . /etc/homelab-diskguard.env
THRESH="${THRESH:-85}"
NTFY_URL="${NTFY_URL:-http://192.168.1.30:8080}"
NTFY_TOPIC="${NTFY_TOPIC:-homelab-deploy}"
host=$(hostname -s)

over=""
# ZFS pools (the thing that actually filled: thin zvols on a full pool freeze VMs).
while read -r name cap; do
    c=${cap%\%}
    [[ "$c" =~ ^[0-9]+$ ]] || continue
    [ "$c" -ge "$THRESH" ] && over+=$'\n'"zpool ${name} ${c}%"
done < <(zpool list -H -o name,capacity 2>/dev/null)

# PVE host filesystems.
while read -r pct mount; do
    p=${pct%\%}
    [[ "$p" =~ ^[0-9]+$ ]] || continue
    [ "$p" -ge "$THRESH" ] && over+=$'\n'"${mount} ${p}%"
done < <(df -P / /var/lib/vz 2>/dev/null | tail -n +2 | awk '{print $5, $6}')

[ -z "$over" ] && exit 0

auth=()
[ -n "${NTFY_TOKEN:-}" ] && auth=(-H "Authorization: Bearer ${NTFY_TOKEN}")
curl -fsS --max-time 10 "${auth[@]}" \
    -H "Title: PVE disk alarm on ${host}" \
    -H "Priority: urgent" \
    -H "Tags: rotating_light,floppy_disk" \
    -d "At/over ${THRESH}%:${over}" \
    "${NTFY_URL}/${NTFY_TOPIC}" >/dev/null 2>&1 || \
    logger -t pve-diskguard "ntfy push failed; over ${THRESH}%:${over}"
exit 0
