# Operations and restore

Run routine commands from the repository root on the trusted control node.
The Ansible recap and failed task are authoritative; ntfy is an additional
signal and may itself be unavailable during an incident.

## Routine workflow

```bash
make validate          # offline; no vault or homelab access
make preflight         # authenticated inventory and connectivity
make check             # check mode, no diff
make check-diff        # opt-in sanitized diff
make verify            # non-disruptive gates, no tags required
make deploy             # converge only after the above are understood
```

Use `make media`, `make dl`, or `make access` to narrow a planned change. Use
`make reconcile` only when deliberately applying the inventory CPU, memory,
startup, or on-boot shape to an existing VM. Provisioning will not move or
shrink storage.

`make verify` must not install packages, rewrite files, restart services, or
leave NFS probe files behind. `make verify-disruptive` is different: it records
the active state of every catalogued download service, stops the jail to prove
fail-closed propagation, and restores exactly the prior state even when the
drill fails. Run it after deployments and quarterly, not during active work.

Useful host checks include:

```bash
# svc-download
systemctl status vpn-netns.service leak-canary.timer
systemctl list-units 'dl-*.service' '*-proxy.socket'
findmnt -t nfs4 /srv/media /srv/backups

# svc-media (run as root; -M reaches the homelab user's systemd manager)
systemctl --user -M homelab@ list-units --type=service
findmnt -t nfs4 /srv/media /srv/backups

# both VMs
systemctl list-timers --all 'backup-*' 'homelab-diskalert.timer'
journalctl -p warning --since today
```

## Nightly verification

Nightly verification is supported only on a Linux control node with systemd,
the repository at `/opt/homelab-iac`, its project `.venv`, SSH credentials, and
a mode-`0600` `.vault_pass`. It runs the safe `verify.yml`, suppresses success
notifications, and never invokes the disruptive drill.

```bash
sudo install -m 0644 contrib/systemd/homelab-verify@.service /etc/systemd/system/
sudo install -m 0644 contrib/systemd/homelab-verify@.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-verify@<user>.timer
sudo systemctl start homelab-verify@<user>.service
journalctl -u homelab-verify@<user>.service -e
```

The instance name is the unprivileged account that owns the checkout, SSH key,
and vault password file. The service calls
`/opt/homelab-iac/.venv/bin/ansible-playbook` explicitly; it does not depend on
a login shell or system Ansible. Confirm the next run with
`systemctl list-timers homelab-verify@<user>.timer` and investigate any
non-zero unit result even if ntfy did not arrive.

## Download maintenance and leak-canary recovery

For planned maintenance, stop the timer on `svc-download`, perform the bounded
change from the control node, then restart the timer on the guest:

```bash
# on svc-download
sudo systemctl stop leak-canary.timer

# on the control node
make verify

# on svc-download, after verification passes
sudo systemctl start leak-canary.timer
```

If the canary reports `DOWN`, repair the missing unit or proxy. If it reports
`LEAK`, leave the stack stopped until namespace membership, nftables policy,
and Mullvad identity are understood. After a passing safe verification from the
control node, start only the intended catalog services—including
LazyLibrarian—on `svc-download`, verify again from the control node, and clear
the trip marker on the guest:

```bash
# on svc-download
sudo systemctl start dl-{sabnzbd,prowlarr,sonarr,radarr,lazylibrarian}.service

# on the control node
make verify

# on svc-download, only after verification passes
sudo rm /var/lib/leak-canary/tripped
sudo systemctl start leak-canary.timer
```

Do not clear the marker merely to silence the alert. For an SSH lockout or
other emergency, use [Incidents](incidents.md).

## Monitoring and alerting

Homepage, Cockpit, and ntfy are deployed with the service VMs. Subscribe the
operator devices to the configured ntfy topic and test both authenticated and
anonymous behavior as applicable. VM filesystem alarms are managed by
`homelab-diskalert.timer`; the Proxmox host itself needs the manual ZFS/root
capacity guard from `contrib/`. Transfer those four files to a temporary
directory on PVE through an authenticated administrative path, then run:

```bash
# on the Proxmox host
sudo install -m 0750 contrib/bin/pve-diskguard.sh /usr/local/sbin/pve-diskguard.sh
sudo install -m 0644 contrib/systemd/homelab-diskguard.service /etc/systemd/system/
sudo install -m 0644 contrib/systemd/homelab-diskguard.timer /etc/systemd/system/
sudo install -m 0600 contrib/systemd/homelab-diskguard.env.example /etc/homelab-diskguard.env
sudo ${EDITOR:-vi} /etc/homelab-diskguard.env
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-diskguard.timer
sudo systemctl start homelab-diskguard.service
```

Set the direct LAN ntfy URL, topic, threshold, and optional token in the root-
owned environment file; derive the media address from inventory. Confirm the
test in `journalctl -u homelab-diskguard.service -e` and on the subscribed
device. A failed ntfy push is not permission to ignore a failed unit or disk
threshold.

## Backups

Backups land on the TrueNAS NFS backup export and retain the configured number
of days (14 by default).

| Workload | Artifact | Consistency |
|---|---|---|
| SABnzbd, Prowlarr, Sonarr, Radarr, LazyLibrarian | `/srv/backups/svc-download/<app>-YYYY-MM-DD.tar.gz` | Live appdata tar; arr application backups inside the archive are preferred when available |
| Jellyfin | `/srv/backups/svc-media/appdata/jellyfin-config-YYYY-MM-DD.tar.gz` | Live config tar |
| Seerr | `/srv/backups/svc-media/appdata/seerr-YYYY-MM-DD.tar.gz` | Live config tar |
| RomM files | `/srv/backups/svc-media/appdata/romm-appdata-YYYY-MM-DD.tar.gz` | Live appdata tar |
| RomM database | `/srv/backups/svc-media/romm-db/romm-YYYY-MM-DD.sql.gz` | `mariadb-dump --single-transaction` |
| Audiobookshelf | `/srv/backups/audiobookshelf/` | Built-in application backup; schedule it once in the UI |

Check timers and the newest artifacts daily through monitoring, and test the
archives before relying on them:

```bash
systemctl status backup-dl-appdata.timer backup-dl-appdata.service
systemctl status backup-media.timer backup-media.service
tar -tzf /srv/backups/svc-download/<app>-YYYY-MM-DD.tar.gz >/dev/null
gzip -t /srv/backups/svc-media/romm-db/romm-YYYY-MM-DD.sql.gz
```

Back up the backup dataset independently; a mounted NFS destination is not
protection from NAS loss. Treat ntfy backup failures as incidents.

## Restore drills

Run a restore drill quarterly and before any upgrade likely to migrate an
application schema. Prefer an isolated VM or disposable application instance.
Record the artifact date, checksum, elapsed time, result, and any manual step.

For every restore:

1. Confirm the NFS mount is really NFSv4 and the chosen artifact can be read.
2. Stop only the affected application; preserve its current appdata under a
   timestamped name so the operation is reversible.
3. Restore into the original parent directory, retain the archive's relative
   paths, set ownership to the configured service UID/GID, and run
   `restorecon -RF` on the restored tree.
4. Start the application, inspect its journal, exercise its UI/API, then run
   `make verify`. Keep the preserved pre-restore data until acceptance.

### Download application

Stop `dl-<app>.service`, preserve `/srv/appdata/<app>`, and extract the selected
archive under `/srv/appdata` (the archive contains the application directory).
For Sonarr, Radarr, Prowlarr, or LazyLibrarian, prefer the application's own
scheduled backup inside the tar when SQLite consistency is in doubt. Start the
unit, check its corresponding proxy, and confirm the container remains in the
VPN namespace.

### Jellyfin or Seerr

Stop the rootless unit with
`systemctl --user -M homelab@ stop <app>.service`. Jellyfin restores beneath
`/opt/homelab/appdata/jellyfin/config`; Seerr restores beneath
`/opt/homelab/appdata/seerr`. Restore as the service account, start the unit,
then test its named Caddy endpoint. A successful Seerr restore must retain its
Jellyfin, Sonarr, and Radarr connections.

### RomM

Stop `romm.service` while leaving the database isolated from user traffic.
Preserve current RomM appdata and take a fresh database dump. Restore the file
archive first, then import the selected SQL through the running `romm-db`
container using the root password already present in that container's
environment. Start RomM and verify login, library metadata, and a known ROM.
Never assume changing vault DB variables changes an initialized MariaDB
database; rotate the database account itself first.

### Audiobookshelf

Use Audiobookshelf's built-in restore workflow against an artifact from
`/config/backups`. Confirm libraries still point to the NFS media paths and
that playback succeeds before accepting the drill.

See [Deployment rollback](deployment.md#rollback) when a restore is part of a
larger release rollback.
