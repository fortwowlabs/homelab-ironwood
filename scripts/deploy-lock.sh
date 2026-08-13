#!/usr/bin/env bash
# Advisory deploy lock, so two control nodes cannot deploy to one estate at
# once. Held on thurgadin: it hosts every VM, so it is up whenever deploying
# is meaningful, and the lock stays outside the machines being deployed to.
#
# Usage: deploy-lock.sh acquire|release|status <lockfile> <holder>
#
# Acquisition uses `set -o noclobber`, which makes the shell open the file
# with O_EXCL — the test-and-create is one syscall, so two deploys racing
# cannot both win. A plain `[ -f ]` test would leave a window between the
# check and the write, which is exactly the case this exists for.
set -euo pipefail

action=${1:?usage: deploy-lock.sh acquire|release|status <lockfile> <holder>}
lock=${2:?missing lockfile path}
holder=${3:-unknown}

case "$action" in
  acquire)
    if (set -o noclobber; printf '%s\n%s\n' "$holder" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
          > "$lock") 2>/dev/null; then
      echo "deploy lock acquired by ${holder}"
      exit 0
    fi
    # Held. Say who and since when, so the operator can act rather than guess
    # — at 2am an unexplained refusal gets the file deleted.
    existing_holder=$(sed -n '1p' "$lock" 2>/dev/null || echo unknown)
    existing_since=$(sed -n '2p' "$lock" 2>/dev/null || echo unknown)
    echo "deploy lock is HELD by ${existing_holder} since ${existing_since}" >&2
    echo "If that deploy is genuinely gone, clear it with: make deploy-unlock" >&2
    exit 1
    ;;
  release)
    rm -f "$lock"
    echo "deploy lock released by ${holder}"
    exit 0
    ;;
  status)
    if [ -f "$lock" ]; then
      echo "held by $(sed -n '1p' "$lock") since $(sed -n '2p' "$lock")"
    else
      echo "free"
    fi
    exit 0
    ;;
  *)
    echo "unknown action: ${action}" >&2
    exit 2
    ;;
esac
