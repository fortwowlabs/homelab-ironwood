# homelab-iac

Ansible provisions and operates two Rocky Linux service VMs on Proxmox: a
fail-closed Mullvad download jail and a rootless media/application host. Caddy
and dnsmasq provide private `*.fort.wow` service names; TrueNAS supplies the NFS
media and backup datasets.

## Architecture

| Component | Responsibility |
|---|---|
| Control node | Runs the pinned Ansible toolchain, Proxmox API calls, and guest SSH configuration |
| `svc-download` | Rootful Podman download apps inside a WireGuard network namespace, with LAN socket proxies and an nftables backstop |
| `svc-media` | Rootless media, request, dashboard, ntfy, DNS, and Caddy services |
| TrueNAS | Manually managed NFS media and backup storage |

The inventory's `ansible_host` values are the canonical VM addresses.
`download_apps` describes the jailed applications, while `caddy_services`
describes media and infrastructure endpoints. See
[Architecture](docs/architecture.md) for ownership and data flow.

## Quick start

1. If any decrypted variables have appeared in a terminal, transcript, ticket,
   or chat, rotate every populated vault credential before continuing. Follow
   [Security](docs/security.md#credential-exposure-response).
2. Complete the manual TrueNAS and Proxmox prerequisites in
   [Deployment](docs/deployment.md), including trust for the Proxmox API CA.
3. Install the pinned runtime and validation dependencies (ShellCheck is an OS
   package and must also be present):

   ```bash
   make deps-dev
   ```

4. Set non-secret environment values in
   `inventory/group_vars/all/main.yml`, host-specific VM shape in
   `inventory/host_vars/`, and the public SSH key. Create the encrypted vault:

   ```bash
   cp inventory/group_vars/all_vault.yml.example inventory/group_vars/all/vault.yml
   ${EDITOR:-vi} inventory/group_vars/all/vault.yml
   .venv/bin/ansible-vault encrypt inventory/group_vars/all/vault.yml
   ```

5. Validate locally, authenticate, and deploy:

   ```bash
   make validate
   make preflight
   make deploy
   make verify
   ```

   On a true first install, the service-VM connectivity part of preflight
   cannot pass until provisioning creates those guests; use the fresh-install
   sequence in the deployment guide.

Targets prompt for the vault password by default. For an unattended Linux
control node, create a mode-`0600` `.vault_pass` and set
`USE_VAULT_FILE=1`; see [Nightly verification](docs/operations.md#nightly-verification).

## Commands

| Command | Purpose |
|---|---|
| `make deps` / `make deps-dev` | Install pinned runtime / runtime plus validation tools |
| `make validate` | Offline syntax, lint, shell, link, catalog, and secret checks |
| `make preflight` | Authenticated inventory graph and required-host connectivity |
| `make deploy` | Provision, converge, verify, and notify |
| `make dl` | Converge `svc-download` only |
| `make media` | Converge `svc-media` only |
| `make access` | Reconcile the Caddy/DNS access layer |
| `make check` | Check mode without diff |
| `make check-diff` | Check mode with secret-bearing diffs suppressed |
| `make verify` | Non-disruptive health and policy gates; temporary NFS probes are always removed |
| `make verify-disruptive` | Fail-closed drill with guaranteed service-state restoration |
| `make reconcile` | Opt-in Proxmox VM shape reconciliation; never moves or shrinks disks |
| `make vault-edit` | Edit the encrypted vault |

Pass additional Ansible flags with `ARGS`, for example
`make media ARGS="--limit svc-media"`. Verification playbooks do not require
tags.

## Operator guides

- [Architecture](docs/architecture.md) — components, trust boundaries, and sources of truth
- [Deployment](docs/deployment.md) — prerequisites, first install, rollout, and rollback
- [Operations and restore](docs/operations.md) — routine checks, maintenance, backups, and restore drills
- [Services](docs/services.md) — URLs, application wiring, DNS, and catalog changes
- [Security](docs/security.md) — secrets, Proxmox trust, image policy, and the VPN backstop
- [Incidents](docs/incidents.md) — symptom-led recovery runbooks

TrueNAS creation, Proxmox host preparation, application UI setup, LAN/tailnet
DNS forwarding, client trust of Caddy's internal CA, and destructive restore
decisions remain deliberately manual.
