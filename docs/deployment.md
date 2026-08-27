# Deployment

This runbook covers a fresh installation and the controlled rollout of a new
repository revision. Run Ansible from the repository root on a trusted control
node. Linux and macOS are supported for interactive runs; the nightly systemd
verification unit is Linux-only.

## Prerequisites

### TrueNAS

Create the media and backup datasets manually, with NFSv4 exports restricted
to the three service VM addresses from `inventory/hosts.yml`. The service UID and
GID in `inventory/group_vars/all/main.yml` must own the writable dataset paths.
Confirm encryption unlock behavior and NFS recovery after a NAS reboot before
placing applications into service. Ansible mounts and probes these exports but
does not create or administer TrueNAS storage.

### Proxmox

Prepare the configured bridge, VM storage, snippet/image storage, and resource
pool. Create a privilege-separated API token with VM rights on the pool,
datastore rights on the two configured storages, and only the small additional
bridge/serial privileges described in
`inventory/group_vars/all_vault.yml.example`. Grant the ACLs to both the user
and token identities.

Set these values explicitly; there is no default PVE SSH target:

- `pve_api_host` and `pve_node` in `inventory/hosts.yml`;
- `pve_ssh_host`, storage names, bridge, and VM pool in inventory group vars;
- VM IDs, CPU, memory, disk size, and startup order in
  `inventory/host_vars/`.

The API token may not see an existing VM outside its pool. Provisioning also
checks `qm status` over the explicit PVE SSH connection and stops rather than
creating a duplicate. Adopt an existing VM into the intended pool only after
checking its identity and configuration.

### Trust the Proxmox API certificate

`pve_validate_certs` defaults to `true`. Use a publicly trusted PVE certificate
or trust the PVE cluster CA on the control node; do not disable validation as a
routine workaround.

For the cluster CA approach, copy `/etc/pve/pve-root-ca.pem` from the PVE host
to a protected location outside this repository, set `pve_api_host` to a DNS
name present in the API certificate, and export the absolute CA path before
running Ansible:

```bash
export REQUESTS_CA_BUNDLE=/absolute/path/to/pve-root-ca.pem
curl --cacert "$REQUESTS_CA_BUNDLE" "https://pve.example.internal:8006/api2/json/version"
```

If hostname verification fails, fix the certificate/SAN or the inventory DNS
name. A successful unauthenticated version response proves TLS trust; API token
authentication is checked by preflight.

### Credentials

Rotate every populated vault credential if decrypted variables have ever been
printed or serialized. This repository had such an exposure during review, so
rotation is a deployment prerequisite, not an optional hardening task. Follow
the ordered procedure in [Security](security.md#credential-exposure-response).

### Cloudflare and Let's Encrypt

The `fortwow.dev` Cloudflare zone must be active and remain empty of public
service records. Leave zone DNSSEC and registrar DS records disabled for the
split-horizon deployment. Create a Cloudflare API token with Zone/DNS/Edit
scoped only to `fortwow.dev`, then store it in the encrypted vault as
`vault_cloudflare_api_token`. Certbot uses it only to create and remove ACME
DNS-01 TXT records; no inbound Internet port is required.

## Control-node setup

`make deps` creates `.venv` and installs the committed versions of Ansible
14.2.0 (ansible-core 2.21.2), proxmoxer 2.3.0, `community.proxmox` 2.0.0, and
`ansible.posix` 2.2.2. `make deps-dev` includes that runtime plus the pinned
Ansible/YAML validation tools; install ShellCheck through the control node's OS
package manager. Upgrade these pins deliberately and validate the full stack
before merging.

```bash
make deps-dev
make validate
```

`make validate` is offline: it uses fixture inventory and must not decrypt the
vault or contact the homelab. It covers syntax, Ansible/YAML/shell lint, links,
catalog consistency, and secret scanning.

### Deploying from TERRA, via WSL

TERRA is the Windows GPU host and also a workstation. **Ansible cannot run on
native Windows** — it needs POSIX (`fcntl`, `pwd`) — so deploys go through WSL,
which has its own clone and its own `.venv`:

```bash
wsl -d Ubuntu-24.04
cd ~/dev/homelab-ironwood
USE_VAULT_FILE=1 make validate
USE_VAULT_FILE=1 make infra
```

`USE_VAULT_FILE=1` selects `--vault-password-file .vault_pass` instead of
prompting, which a non-interactive session cannot answer.

**That clone is separate from the Windows one at `C:\Users\tv\dev`, and the two
drift.** Check `git log -1` inside WSL before deploying — it is not necessarily
on the branch you just committed to on the Windows side.

**Deploy from the WSL clone, never from `/mnt/c` — being inside WSL is not
enough.** It is tempting to skip the second clone by running WSL's `ansible`
against the Windows checkout, since WSL can see it at
`/mnt/c/Users/tv/dev/homelab-ironwood`. That runs, and it is wrong. DrvFs
mounts the drive `drwxrwxrwx`, and Ansible refuses to read `ansible.cfg` from a
world-writable directory — it says so in a `[WARNING]`, then continues. Measured
2026-08-27, from the two clones on this host:

| Run from | `ansible --version` reports |
|---|---|
| `/home/tv/dev/homelab-ironwood` | `config file = …/ansible.cfg`, collections from `collections/` |
| `/mnt/c/Users/tv/dev/homelab-ironwood` | **`config file = None`**, collections from `~/.ansible/collections` |

So a deploy from `/mnt/c` silently discards every setting this repo pins in
`ansible.cfg` and resolves collections somewhere else entirely. It does not
fail; it does something different. The warning is one line in a stream of
Ansible output and is easy to miss.

The Windows clone is for editing and for `make validate`, which is unaffected
by this and passes there since `.gitattributes` pinned the working tree to LF.
Deploys belong to the WSL clone, which also holds `vault.yml` and
`.vault_pass` — the Windows clone need not have them at all.

**The trap that makes this worse than it sounds:** driving WSL from Git Bash
with `wsl.exe -- bash -c '...'` lets Git Bash expand `$(...)` and `$VAR`
*before* the string reaches WSL, even inside single quotes. So
`echo "branch: $(git branch --show-current)"` reports the **Windows** checkout
while appearing to report WSL. On 2026-08-22 that produced a confident,
completely wrong reading, and a `make infra` run against `main` believing it was
a feature branch. It also hung a command for half an hour: `$f` expanded to
empty, so `grep pattern` ran with no filename and blocked reading stdin.

Put the commands in a script file and run `wsl.exe -d Ubuntu-24.04 -- bash
/mnt/c/path/to/script.sh` instead. Add `MSYS_NO_PATHCONV=1` or Git Bash rewrites
the `/mnt/c/...` argument into a Windows path.

### `gh` on TERRA

Installed at `~/.local/bin/gh` inside WSL rather than through apt, because
passwordless sudo is not available there. It authenticates from
`vault_github_token`:

```bash
export GH_TOKEN=$(.venv/bin/ansible-vault view --vault-password-file .vault_pass \
    inventory/group_vars/all/vault.yml | grep '^vault_github_token:' | cut -d' ' -f2-)
~/.local/bin/gh run list --limit 5
```

CI runs on push to `main`, after the merge rather than before it, so it is an
alarm and not a gate — but nothing surfaces a red run on its own. Check it after
merging.

## Inventory and vault

1. Set shared non-secret values in `inventory/group_vars/all/main.yml`.
2. Set the canonical service VM addresses in `inventory/hosts.yml`; do not
   duplicate them in group or host vars.
3. Set VM shape in `inventory/host_vars/svc-download.yml` and
   `inventory/host_vars/svc-media.yml`.
4. Create and encrypt the vault:

   ```bash
   cp inventory/group_vars/all_vault.yml.example inventory/group_vars/all/vault.yml
   ${EDITOR:-vi} inventory/group_vars/all/vault.yml
   .venv/bin/ansible-vault encrypt inventory/group_vars/all/vault.yml
   ```

5. Run `make preflight`. It uses an authenticated inventory graph and treats
   an unreachable required host as a failure. It never renders
   `ansible-inventory --list`, which can expand decrypted host variables.

On a true first install the service VMs do not exist yet, so their connectivity
cannot pass until `make deploy` has provisioned them. In that case validate the
control node, PVE API/SSH, inventory, storage, and external prerequisites first;
run the full preflight immediately after initial provisioning and before any
day-two rollout.

## Fresh install

```bash
make validate
make deploy
make preflight
make verify
make verify-disruptive
make check
```

`make deploy` provisions missing VMs, waits for strict cloud-init completion,
converges download and media roles, runs verification, and sends ntfy status.
An API/authentication error, hidden VMID, ambiguous detached disk, storage
mismatch, or failed host verification aborts the run. A safe partial
EFI/import/boot/resize sequence resumes one step at a time; existing disks are
never moved, recreated, or shrunk automatically.

After the first converge, complete the pfSense/Tailscale split-DNS and
application UI steps in [Services](services.md). Publicly trusted certificates
need no client trust-store changes. `make check` must show no unexpected
changes.

## Refactor rollout

Treat the refactor as an operational change even though VM recreation is out
of scope.

1. Rotate exposed credentials and run a full-history secret scan.
2. Capture current Proxmox VM backups/snapshots according to local policy and
   verify the NFS backup artifacts in [Operations and restore](operations.md).
3. Record the current Git commit, immutable image digests, VM configuration,
   active services, timers, and mount state.
4. Run `make validate`, `make preflight`, `make check`, and `make verify`.
5. Deploy media first with `make media`. Confirm Caddy against every configured
   backend and verify Jellyfin, Seerr, RomM, backups, DNS, and ntfy.
6. During a download maintenance window, pause the leak-canary timer, run
   `make dl`, verify every catalog proxy and Mullvad identity, then re-enable
   the timer.
7. Run `make verify`, `make verify-disruptive`, and a second converge. The
   second converge must have no unexplained changes or restarts.

Do not begin the download rollout if media verification is red. Do not send a
green completion notification unless every targeted host recorded a passing
verification fact.

## Rollback

Rollback is explicit and favors restoring known-good state over attempting to
reverse individual migrations.

1. Stop the affected rollout and preserve logs, the Ansible recap, current
   appdata, image digests, and Proxmox configuration.
2. For a configuration-only regression, select the recorded known-good Git
   commit, keep its reviewed image digests, run `make check`, and converge only
   the affected VM.
3. If an application performed an incompatible data migration, stop it and use
   the tested artifact-specific restore in [Operations and restore](operations.md#restore-drills).
4. Restore a Proxmox backup only during an approved outage. Confirm the
   restored VM's name, pool, bridge, boot/cloud-init disks, and storage match
   inventory before running Ansible again.
5. Run safe verification, then the disruptive drill, before reopening the
   maintenance window or clearing an incident.

Never “fix” rollback by moving/shrinking a disk automatically, disabling PVE
TLS validation, flushing the download backstop without console access, or
reusing credentials that were exposed.
