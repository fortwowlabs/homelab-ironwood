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
LazyLibrarian and Shelfmark—on `svc-download`, verify again from the control
node, and clear the trip marker on the guest:

```bash
# on svc-download
sudo systemctl start dl-{sabnzbd,prowlarr,sonarr,radarr,lazylibrarian,shelfmark,bazarr,jdownloader}.service

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
operator devices to **both** ntfy topics and mute the informational one:

| Topic | Contents | Phone setting |
|---|---|---|
| `homelab-deploy` | deploy/verify succeeded — routine confirmations | muted |
| `homelab-alerts` | anything actionable: failed units, disk, certificate, ZFS, SMART | loud; urgent bypasses Do Not Disturb |

The split exists because a channel that pings for every green deploy gets
muted, and a muted channel is the same as no alerting at all.

### What watches what

| Watcher | Where | Cadence | Raises |
|---|---|---|---|
| `notify-failure@.service` | all VMs + PVE | on failure | any unit in `onfailure_units_*` failing |
| `homelab-failedunits.timer` | all VMs | 15 min | units already failed, system and rootless |
| `homelab-diskalert.timer` | all VMs | 15 min | local FS ≥85%, NFS ≥90% |
| `homelab-certwatch.timer` | svc-media | daily | wildcard certificate under 21 days |
| `homelab-heartbeat.timer` | svc-media | daily | pings healthchecks.io; fails if ntfy is down |
| `homelab-backups-fresh.timer` | svc-infra | daily 04:40 | any VM's newest backup older than 26h |
| `homelab-diskguard.timer` | PVE | 15 min | pool or root/vz ≥80% |
| `homelab-pve-health.timer` | PVE | daily | pool not healthy, scrub older than 45d, failed units |
| zed + smartd hooks | PVE | on event | ZFS pool events and SMART warnings |

Alerts deduplicate: a condition that persists re-alerts every 6 hours rather
than every run, and clearing one sends a single all-clear.

### The Proxmox host

It is now managed — `make pve` installs the capacity guard, the ZFS event
hook, the SMART hook, and the daily health check over SSH as root. The old
manual `contrib/` installation is gone; `contrib/bin/pve-diskguard.sh` and
`contrib/systemd/homelab-diskguard.*` were promoted into `roles/pve_mon/`.

Its scope is deliberately narrow. PVE's storage, networking, and updates stay
hand-managed; only the watchers are converged, because a failed play against a
hypervisor takes every guest with it.

zed and smartd were both already running and already detecting these faults —
they delivered to local root mail, on a box where nothing reads mail. What
changed is delivery, not detection.

### The dead-man's switch

Every alert above travels through ntfy on svc-media, which cannot report that
svc-media, the network, or the power is gone — silence and health look
identical from a phone. Four healthchecks.io checks close that: jobs ping out
on success and healthchecks.io alerts on the ping that never arrives.

Setup is one-time and manual (see the `vault_hc_ping_*` block in
`inventory/group_vars/all_vault.yml.example`): create the account, **configure
its notification channels first**, create the four checks, then paste the ping
UUIDs with `make vault-edit`. Until then the UUIDs are empty, every ping is
skipped with a journal line, and nothing fails — but there is no external
safety net either.

A failed ntfy push is not permission to ignore a failed unit or disk
threshold.

## Backups

Backups land on the TrueNAS NFS backup export and retain the configured number
of days (14 by default).

| Workload | Artifact | Consistency |
|---|---|---|
| Catalogued download apps, including Shelfmark | `/srv/backups/svc-download/<app>-YYYY-MM-DD.tar.gz` | Live appdata tar; application-consistent backups inside the archive are preferred when available |
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
VPN namespace. After restoring Shelfmark, also confirm `/api/health`, its
Prowlarr/SABnzbd connections, ebook and audiobook destinations, and one
authorized search.

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

## Changing a service VM's IP

`inventory/hosts.yml`'s `ansible_host` under `service_vms` is the single
source of truth for each VM's address — cloud-init's static-IP config, Caddy's
reverse-proxy backends, dnsmasq's DNS records, the svc-download nftables
backstop's allowed peers, and `ntfy_url` all derive from it via
`hostvars[...].ansible_host`. `preflight.yml` asserts every service VM defines
one and that they're distinct.

This is a **fresh-provision-time** knob, not a live-migration tool: editing
the value and re-running against an **already-provisioned** VM does not move
it — Proxmox doesn't renumber a running guest's address because the inventory
changed, and `pve_vm`'s existing-VM identity checks assume the VM at that
node/VMID still answers at its original address. To actually move a live VM
to a new IP: update the value here, then perform the network-side change
(guest OS static IP + any DHCP reservation) out of band, confirm SSH reaches
the new address, and only then re-run the playbooks so Caddy/dnsmasq/backstop
configuration catches up to match reality. Coordinate with whatever DNS/HTTPS
clients have cached the old address.

## Renaming a service VM

Each service VM's `inventory_hostname` (the key under
`inventory/hosts.yml`'s `download_vms` / `media_vms` child groups) is the
single source of truth for its name — play targeting (`hosts: download_vms` /
`media_vms`), the Makefile's `dl`/`media`/`access` targets, `download_host` /
`media_host` (`group_vars/all/main.yml`, used everywhere a template or task
needs "the other VM's" address), backup directory paths
(`/srv/backups/{{ inventory_hostname }}`), and the DNS host-record for the
physical box itself all derive from it instead of a literal
`svc-download`/`svc-media` string.

Like the IP above, this is a **fresh-provision-time** knob. To rename an
already-provisioned VM: rename the inventory key (and the matching
`inventory/host_vars/<name>.yml` file — Ansible requires the filename to
match), re-run a fresh provision/configure cycle so the guest's own hostname,
Caddy vhost bindings, and dnsmasq host-record pick up the new name, and expect
existing backup directories under the old name to need a manual move (the
role only creates/reads `/srv/backups/<new-name>`, it won't migrate the old
one). Any bookmarks or DNS caches pointed at `<old-name>.{{ service_domain }}`
break until they're updated too.

## Changing the service domain

`service_domain` (`inventory/group_vars/all/main.yml`, `fortwow.dev` today) is
the single source for the split-horizon domain every service uses —
Caddy's vhosts, dnsmasq's zone file, cloud-init's guest search domain
(`search_domain` follows it by default), and every `<name>.{{ service_domain
}}` reference in a template derive from this one value.

This is more disruptive than an IP or hostname change because two **manual,
external** integrations are keyed to the domain string and are NOT
Ansible-managed (see docs/deployment.md and docs/services.md):

- pfSense's DNS Resolver **Domain Override**, which forwards the old TLD to
  the media VM — it must be repointed at the new domain.
- Tailscale's split-DNS domain must be replaced while retaining the media VM
  nameserver and approved subnet route.
- The public DNS provider, ACME token scope, Certbot lineage, deploy hook, and
  Caddy certificate paths must all cover the new registered domain.

To change it, first prepare the public zone, DNSSEC posture, scoped API token,
and certificate-lineage migration. Then update `service_domain` (and
`search_domain` only if it should diverge), run the media deployment so
dnsmasq, Certbot, and Caddy converge, deploy all var-driven consumers, and
replace the pfSense and Tailscale entries. A hard cutover makes old names stop
working; coordinate DNS changes with the Ansible run.

`search_domain` is applied only when a VM is created. Existing VMs keep their
old guest FQDN/search domain until recreation; never use `qm set
--searchdomain` to correct this cosmetic drift because it changes cloud-init
instance identity.
