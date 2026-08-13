#!/usr/bin/env bash
# Run a deploy command while holding the estate-wide lock.
#
# The lock lives on thurgadin: it hosts every VM, so it is up whenever
# deploying is meaningful, and holding it there keeps it off the machines
# being deployed to.
#
# This wraps the Make targets rather than living inside site.yml, because
# every scoped target passes --limit and a play that matches no hosts is
# silently skipped — the lock would then be missing from exactly the commands
# used day to day.
#
# scripts/deploy-lock.sh is piped to the remote shell rather than installed,
# so the logic under test locally is byte-identical to the logic that runs.
set -euo pipefail

lock_host=${DEPLOY_LOCK_HOST:-192.168.1.10}
lock_user=${DEPLOY_LOCK_USER:-root}
lock_path=${DEPLOY_LOCK_PATH:-/var/lock/homelab-deploy.lock}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
holder="$(hostname -s):$$"

remote() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 "${lock_user}@${lock_host}" \
    bash -s -- "$1" "$lock_path" "$holder" < "${here}/deploy-lock.sh"
}

remote acquire

# Release on success, failure, and interrupt alike. Without the trap, a
# Ctrl-C during a deploy strands the lock and the next run is refused by a
# holder that no longer exists.
trap 'remote release >/dev/null 2>&1 || true' EXIT

"$@"
