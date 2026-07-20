# Architecture

`homelab-iac` keeps the Proxmox/Ansible/Podman design deliberately small: one
control node, two service VMs, and external NFS storage. Ansible owns VM
provisioning and guest configuration; it does not own the Proxmox host,
TrueNAS, pfSense, Tailscale, or application UI state.

## System flow

```text
control node
  |-- HTTPS + scoped token --> Proxmox API
  |-- root SSH -------------> Proxmox image/snippet/disk operations
  `-- admin SSH + sudo ------> svc-download and svc-media

clients --> pfSense/Tailscale split DNS --> dnsmasq on svc-media
        --> Caddy on svc-media ----------> media/infra services
                                         `> svc-download LAN proxies

svc-download -- WireGuard netns --> Mullvad --> Internet
svc-download -- NFSv4 ---------------------> TrueNAS media/backups
svc-media ---- NFSv4 ---------------------> TrueNAS media/backups
```

## Components and ownership

### Control node

The control node runs the pinned Python and Ansible dependencies from `.venv`.
Provisioning talks to the Proxmox API with a scoped token. Operations that the
API module cannot perform safely—image placement, cloud-init snippets, and
resumable disk setup—use the explicit `pve_ssh_host`. Guest configuration uses
the inventory's admin account with `become`.

The scheduled verification unit is supported only on a Linux control node
with systemd. Interactive deployment may run from Linux or macOS.

### `svc-download`

Download applications are rootful Podman Quadlets attached to a pre-created
WireGuard network namespace. The host nftables policy permits only required
LAN, NFS, DNS, ntfy, and tunnel traffic. systemd socket proxies expose the app
UIs without moving the containers out of the jail. A canary checks expected
units, namespace membership, and external VPN identity; a leak stops every
catalogued download service and leaves a persistent trip marker.

### `svc-media`

Media and infrastructure containers run rootless as the service account.
Jellyfin is also bound to the VM's LAN address for local playback; Caddy
provides the normal named HTTPS endpoints. dnsmasq answers only the private
service domain. Firewalld scopes host ports to the configured LAN.

### Storage and access

TrueNAS exports the media and backup datasets over NFSv4. Both guests mount
them with systemd automounts and prove access as the service UID; server-side
ownership remains authoritative because root squash is expected. A dedicated
Tailscale subnet router, not either service VM, provides remote access to the
LAN subnet.

## Sources of truth

- `inventory/hosts.yml` owns host membership and each VM's canonical
  `ansible_host`. Roles derive service addresses from `hostvars`; do not add
  parallel `svc_*_ip` or `vm_ip` values.
- `inventory/group_vars/all/main.yml` owns shared non-secret network, identity,
  storage, image, and service settings. The encrypted
  `inventory/group_vars/all/vault.yml` owns secrets.
- `inventory/group_vars/all/apps.yml` contains `download_apps`, which owns each
  jailed application's immutable image, UI port, volumes, media-mount
  requirement, backup paths, and dashboard metadata. The role derives
  Quadlets, proxies, firewall ports, pulls, service state, backups, canary
  membership, probes, and recovery from that catalog. The same file centralizes
  reviewed media image digests.
- `caddy_services` in `inventory/group_vars/all/main.yml` owns media and
  infrastructure endpoints. The access layer derives Caddy vhosts, private DNS
  records, and Homepage entries from it and the download catalog.
- `inventory/host_vars/` owns the desired Proxmox VM shape. Existing disks are
  validated, never automatically recreated, moved, or shrunk.

When adding or removing a download application, change its catalog entry
instead of adding one-off task lists. When adding a media or infrastructure
endpoint, change `caddy_services`; see [Services](services.md).

## Deployment and verification flow

`site.yml` performs these stages in order:

1. Query and, when absent, provision both VMs through Proxmox.
2. Apply shared cloud-init, identity, package, SELinux, and NFS behavior.
3. Converge and verify `svc-download` before allowing real download traffic.
4. Converge host monitoring, then `svc-media`; this lets media verification
   probe the configured Cockpit backends through Caddy.
5. Assert that every targeted host set its verification result before sending
   the success notification.

`verify.yml` imports only each role's non-disruptive verification tasks. It
does not converge or restart services. `verify-disruptive.yml` records which
catalog services were running, proves the jail fails closed, and restores that
exact prior state in an `always` path even when an assertion is deliberately
failed.

## Safety invariants

- No download application may reach the Internet outside the Mullvad
  namespace.
- A missing, hidden, partial, or mismatched Proxmox VM fails closed; an API
  error is never interpreted as “VM absent.”
- NFS consumers start only after proving the expected NFSv4 mount.
- Secret-bearing tasks suppress logs and diffs; operator validation never
  expands decrypted host variables.
- Images are immutable by digest, and the Rocky cloud image is checked against
  the SHA-256 committed with the desired build.
- A deployment cannot report success unless all selected hosts completed
  verification.
