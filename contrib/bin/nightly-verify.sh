#!/usr/bin/env bash
# nightly-verify.sh — run the read-only gate checks on a schedule.
#
# Behavior:
#   - Skips the fail-closed KILL test (--skip-tags killtest) so it never
#     interrupts in-flight downloads.
#   - Suppresses the green "all OK" ntfy (-e notify_on_success=false); the
#     playbook's rescue blocks still fire a red/urgent ntfy on any failure.
#   - Non-zero exit on failure so systemd marks the unit failed (and you get
#     `systemctl --failed` visibility on top of the ntfy).
#
# Run by homelab-verify.service. Assumes a .vault_pass file in the repo root
# (chmod 600, gitignored). Adjust REPO if you clone elsewhere.
set -euo pipefail

REPO="${HOMELAB_IAC_REPO:-/opt/homelab-iac}"
cd "$REPO"

exec ansible-playbook verify.yml \
    --vault-password-file .vault_pass \
    --tags verify \
    --skip-tags killtest \
    -e notify_on_success=false
