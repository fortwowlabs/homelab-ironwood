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

# Release on normal exit (success or failure) but NOT on a signal: the
# wrapper dying does not prove the wrapped deploy died. A targeted `kill`
# hits only this process — ansible-playbook handles SIGINT/SIGTERM itself
# and keeps running for several more seconds — so releasing here would hand
# the lock to the other control node while the first deploy is still
# applying. A stale lock merely refuses the next run loudly; an early
# release is silent and worse. Ctrl-C at a terminal is unaffected: the whole
# process group receives the signal together, wrapper and child alike.
signalled=0
trap 'signalled=1; exit 130' INT TERM
trap 'if [ "$signalled" -eq 0 ]; then
        remote release >/dev/null 2>&1 || true
      else
        echo "deploy-lock: signalled — lock deliberately NOT released, because" >&2
        echo "  the deploy may still be running. Confirm it has stopped, then:" >&2
        echo "      make deploy-unlock" >&2
      fi' EXIT

"$@"
