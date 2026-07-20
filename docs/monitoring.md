# Monitoring & alerting

Three layers, all reached by name (see [dns-and-names.md](dns-and-names.md)):

| What | Where | URL |
|------|-------|-----|
| **Homepage** dashboard (launcher for everything) | svc-media, rootless | `https://home.fort.wow` |
| **Cockpit** ops console (systemd, storage, logs, podman) | each VM | `https://cockpit-media.fort.wow`, `https://cockpit-dl.fort.wow` |
| **ntfy** alert server (deploy pings, leak canary, disk alarms) | svc-media, rootless | `https://ntfy.fort.wow` (and direct `http://192.168.1.30:8080`) |

## Cockpit

Installed on both VMs (`cockpit`, `cockpit-podman`, `cockpit-storaged`), reverse-
proxied by Caddy. Log in with your system account (`straderb`). It shows systemd
units, logs, storage/filesystems, updates, a terminal, and — via cockpit-podman —
**rootful** containers (svc-download's `dl-*`). Note: on svc-media the app
containers are **rootless** (`homelab` user); cockpit-podman only shows the
logged-in user's containers, so use Homepage for the media-service list and
Cockpit for host/systemd/storage. The `Origins` in `/etc/cockpit/cockpit.conf`
is set to the proxied hostname so the WebSocket isn't rejected cross-origin.

## Homepage

A dashboard/launcher rendered from the `caddy_services` dict — add a service
there and it appears here, grouped by its `group`, with an icon. Config lives at
`/opt/homelab/appdata/homepage/`. It's link tiles today; live status and
arr/Jellyfin API widgets are an easy follow-up (drop API keys into the widget
configs — keys go in the vault).

## Disk alarms (the 2026-07-20 incident-preventer)

- **Both VMs:** `homelab-diskalert.timer` runs every 15 min, ntfy's `high` if any
  real local filesystem is ≥ `disk_alert_threshold` (default 85%). Managed by the
  `mon` role — nothing to install.
- **PVE host (thurgadin)** — the box that actually filled — is not Ansible-managed,
  so install the guard by hand (mirrors `contrib/`'s nightly-verify):

  ```bash
  sudo cp contrib/bin/pve-diskguard.sh /usr/local/sbin/ && sudo chmod 750 /usr/local/sbin/pve-diskguard.sh
  sudo cp contrib/systemd/homelab-diskguard.{service,timer} /etc/systemd/system/
  sudo cp contrib/systemd/homelab-diskguard.env.example /etc/homelab-diskguard.env
  sudo $EDITOR /etc/homelab-diskguard.env && sudo chmod 600 /etc/homelab-diskguard.env
  sudo systemctl daemon-reload && sudo systemctl enable --now homelab-diskguard.timer
  # test: sudo systemctl start homelab-diskguard.service ; journalctl -u homelab-diskguard -e
  ```

  It alarms `urgent` when any zpool (rpool/nvme0pool) or the PVE root/vz
  filesystem is ≥ threshold. Point `NTFY_URL` at svc-media's ntfy.

## ntfy

The alert sink everything already POSTs to (`ntfy_url` in group_vars). This branch
finally stands it up as a rootless container on svc-media, bound to the LAN IP so
the leak canary and deploy pings reach it. Subscribe your phone to the
`homelab-deploy` topic in the ntfy app pointed at `http://192.168.1.30:8080` (or
`https://ntfy.fort.wow`).
