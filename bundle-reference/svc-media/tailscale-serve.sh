#!/usr/bin/env bash
# svc-media access layer: Tailscale Serve, no Caddy, no firewalld opens.
# Serve binds to the tailnet only and provisions *.ts.net certs itself.
# Run once; --bg persists across restarts (state in tailscaled).
set -euo pipefail

tailscale serve --bg --https=8443 localhost:13378   # Audiobookshelf
tailscale serve --bg --https=8446 localhost:8081    # RomM
tailscale serve status

# Girlfriend access: SHARE this node into her tailnet
# (admin console > Machines > svc-media > Share) instead of inviting her
# into yours. Shared machines are quarantined by default (can answer,
# cannot initiate) and she sees nothing else. Zero grants engineering.
#
# Rollback: tailscale serve --https=8443 off  (etc.)
