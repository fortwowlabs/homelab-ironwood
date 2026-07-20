# homelab-iac

Ansible-driven, git-versioned deployment of the netns-VPN-jail homelab
(thurgadin / convoker / svc-download / svc-media). This is the automation
layer over the `homelab-vpn-bundle` design — same architecture, same gates,
now runnable as one command with a check-back-later ntfy ping.

Run everything from the MBP. It talks to the PVE **API** (token) for VM
lifecycle and **SSH** for in-guest config. cloud-init stays minimal (identity
+ packages); Ansible owns everything after first boot, so day-two changes and
drift checks use the same code.

## What is and isn't automated

| Phase (DEPLOY.md)            | Here?  | Why |
|------------------------------|--------|-----|
| 1 TrueNAS storage            | **No** | One-time; the encryption reboot test you *want* to witness. Do it by hand. |
| 2 PVE host prep (RAM, order) | Partly | Snippet content + boot order are one-liners in DEPLOY; do those once. |
| 3/5 VM creation              | Yes    | `pve_vm` role via the API |
| 4 Jail + stack               | Yes    | `svc_download` role, gates as assertions |
| 6 Media stack                | Yes    | `svc_media` role. Media apps + LazyLibrarian (books/audiobooks) + RomM (ROM presentation + metadata). |
| 7 App wiring                 | No     | Clicking through SAB/Prowlarr/arr/LazyLibrarian/Jellyseerr UIs — see [docs/DEPLOY-CHECKLIST.md](docs/DEPLOY-CHECKLIST.md) |
| 9 Backups                    | Yes    | Nightly timers on both VMs: RomM DB dump + appdata tars (media, 03:05), arr/SAB/LazyLibrarian appdata tars (download, 03:10), 14-day retention. ABS uses its built-in backup to the NFS-mounted `/config/backups` — schedule it once in the UI. **Restore is manual; test it quarterly.** |
| 9 ntfy server                | Yes (v6) | Rootless container on svc-media (`ntfy.fort.wow`), bound to the LAN IP so the canary/deploy/disk alarms deliver. |
| **Service DNS names (v6)**   | Yes    | dnsmasq + Caddy hostname routing → `*.fort.wow`. 3 one-time glue steps ([docs/dns-and-names.md](docs/dns-and-names.md)) |
| **Monitoring (v6)**          | Yes    | Cockpit (both VMs) + Homepage dashboard + disk-space ntfy alarms. PVE zpool guard installs by hand ([docs/monitoring.md](docs/monitoring.md)) |

Mullvad key generation is manual by nature. Tailscale is used for remote access
via the subnet router; `vault_tailscale_authkey` still enables unattended
`tailscale up` on svc-media (skipped with a visible note otherwise).

**New here? Start with [docs/DEPLOY-CHECKLIST.md](docs/DEPLOY-CHECKLIST.md)** — the
ordered runbook (base install + v6), which links the per-feature docs in `docs/`.

## One-time setup

```bash
# 0. control node deps (MBP) — a project venv, because Homebrew's python
#    refuses global pip installs (PEP 668). The Makefile auto-detects .venv.
python3 -m venv .venv
.venv/bin/pip install ansible "proxmoxer>=2.0" requests
.venv/bin/ansible-galaxy collection install -r requirements.yml

# 1. PVE API token (on thurgadin, once) — SCOPED in v5: pool + two storages,
#    privilege separation stays ON. Full command block with rationale lives in
#    inventory/group_vars/all_vault.yml.example; short version:
pveum pool add homelab-svc --comment "svc-download + svc-media"
pveum user add automation@pve
pveum user token add automation@pve ansible                # copy the secret
set +H   # MUST be its own line first: interactive bash history expansion
         # mangles the !ansible token id ("event not found") otherwise
for who in "-user automation@pve" "-token automation@pve!ansible"; do
  pveum aclmod /pool/homelab-svc     $who -role PVEVMAdmin
  pveum aclmod /storage/nvme0pool    $who -role PVEDatastoreUser
  pveum aclmod /storage/local        $who -role PVEDatastoreUser
done
# VM serial ports are permission-gated at / (Sys.Modify), not at the pool —
# without this the create 403s with "Permission check failed (/, Sys.Modify)".
# One-privilege custom role keeps the widening deliberate and auditable:
pveum role add homelab-sysmodify -privs Sys.Modify
for who in "-user automation@pve" "-token automation@pve!ansible"; do
  pveum aclmod / $who -role homelab-sysmodify
done
# PVE 8 gates NIC attachment behind SDN.Use on the bridge (403 "SDN.Use"
# on /sdn/zones/localnetwork/vmbr0 otherwise) — same custom-role pattern:
pveum role add homelab-sdnuse -privs SDN.Use
for who in "-user automation@pve" "-token automation@pve!ansible"; do
  pveum aclmod /sdn/zones/localnetwork/vmbr0 $who -role homelab-sdnuse
done

# 2. secrets — MUST live inside group_vars/all/ (a file named all_vault.yml
#    maps to a group called "all_vault", which doesn't exist, and is silently
#    never loaded). v6 adds the vault_romm_* block (DB creds + metadata keys).
cp inventory/group_vars/all_vault.yml.example inventory/group_vars/all/vault.yml
$EDITOR inventory/group_vars/all/vault.yml      # token secret + Mullvad + vault_romm_*
.venv/bin/ansible-vault encrypt inventory/group_vars/all/vault.yml

# 3. your public SSH key
$EDITOR inventory/group_vars/all/main.yml       # set admin_ssh_pubkey

# 4. confirm you can reach PVE root over SSH (image fetch/snippet render use it)
ssh root@192.168.1.10 true
```

After the deploy, three one-time glue steps make the `*.fort.wow` names resolve
(pfSense domain override, Tailscale split-DNS, install the Caddy root CA) — all
in [docs/dns-and-names.md](docs/dns-and-names.md). The full ordered runbook,
including the v6 features and app wiring, is
[docs/DEPLOY-CHECKLIST.md](docs/DEPLOY-CHECKLIST.md).

## Deploy

```bash
ansible-playbook site.yml --ask-vault-pass
```

Or via the Makefile (see `make help` for all targets):

```bash
make deps          # one-time: collections + python libs
make preflight     # syntax + inventory + ping, before a real run
make deploy        # full unattended deploy + ntfy
make check         # dry-run with diff (drift detection)
make verify        # gate assertions only + ntfy
make dl            # just svc-download   (ARGS="--tags jail" etc.)
make media         # just svc-media
make access        # (re)run tailscale + caddy on svc-media, e.g. after adding the authkey
make reconcile     # push cores/memory/onboot/startup onto EXISTING VMs (opt-in drift repair)
make vault-edit    # edit the encrypted secrets
```

Every target prompts for the vault password once. To run non-interactively
(required for the nightly timer): `echo 'pass' > .vault_pass && chmod 600
.vault_pass`, then add `USE_VAULT_FILE=1` to any target. `.vault_pass` is
gitignored. Pass extra ansible flags with `ARGS=`, e.g.
`make deploy ARGS="--limit svc-download --tags jail,verify"`.

Provisions both VMs, waits for cloud-init, builds the jail (gates block real
traffic until leak tests pass), starts the media stack, and pushes a green or
red ntfy at the end. Walk away; the phone tells you.

## Nightly health check (systemd timer)

`contrib/` ships a timer that runs the gate assertions nightly and only pings
you on failure — the green "all OK" ntfy is suppressed so a passing night is
silent. It also skips the fail-closed KILL test (`--skip-tags killtest`) so it
never interrupts in-flight downloads; run that one manually with
`make verify` when you want the full check.

Install on an always-on box that has the repo + vault password (your MBP or a
small LAN host — **not** the PVE host):

```bash
sudo cp -r homelab-iac /opt/homelab-iac        # or clone there
cd /opt/homelab-iac
echo 'YOUR_VAULT_PASS' > .vault_pass && chmod 600 .vault_pass
sudo cp contrib/systemd/homelab-verify.* /etc/systemd/system/
# edit User= and paths in the .service to match your box
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-verify.timer
systemctl list-timers homelab-verify.timer     # confirm next run
```

A failing gate fires an urgent ntfy (from the playbook's rescue block) and
marks the unit failed, so `systemctl --failed` catches it too. Test the wiring
any time with `sudo systemctl start homelab-verify.service` then
`journalctl -u homelab-verify.service -e`.

## Slices & drift

```bash
# resume just the jail on svc-download
ansible-playbook site.yml --limit svc-download --tags jail,verify --ask-vault-pass

# what would change? (day-two drift — IN-GUEST config only; VM shape is
# create-once and gets reconciled explicitly with `make reconcile`)
ansible-playbook site.yml --check --diff --ask-vault-pass

# gates only, e.g. from cron on the MBP
ansible-playbook verify.yml --tags verify --ask-vault-pass

# gates only, skipping the disruptive fail-closed kill test
ansible-playbook verify.yml --tags verify --skip-tags killtest --ask-vault-pass
```

Tags on the svc_download role: `files secrets nfs jail containers backup
verify`, plus `killtest` on the one fail-closed test that stops/starts the
jail. svc_media adds `media access backup`. Tag-sliced runs work in v5 (the
includes are `always`-tagged); failures still alert because rescue blocks are
`always`-tagged too.

## Updates & image policy (v5)

- **Containers:** the inert auto-update labels are GONE (there was no
  `podman-auto-update.timer`, so they advertised an update story that never
  ran). RomM is pinned to the release verified against this layout
  (`4.9.2`); everything else runs `:latest` at deploy time — after burn-in,
  pin what you have:  `podman inspect --format '{{ '{{' }}.ImageDigest{{ '}}' }}' jellyfin`
  then set `Image=docker.io/jellyfin/jellyfin@sha256:...` in the quadlet.
  Updates are a deliberate act: bump, restart, check.
- **Rocky image:** pinned to a dated build with exact-filename checksum
  verification (bump procedure in `group_vars/all.yml`). `.latest.` pins are
  refused by an assertion.

## Leak canary operations (v5)

A LEAK stops the download stack and writes `/var/lib/leak-canary/tripped`;
every 15-min run keeps alerting until you investigate and clear it:

```bash
rm /var/lib/leak-canary/tripped
systemctl start dl-{sabnzbd,prowlarr,sonarr,radarr}
```

Planned maintenance on the stack? `systemctl stop leak-canary.timer` first,
`systemctl start leak-canary.timer` after, or it will ping you about the
stopped services (that's it doing its job).

## Gotchas carried over from the bundle

- **Adopting a bundle-era VM?** Add it to the pool first:
  `pveum pool modify homelab-svc -vms 131`. The token's visibility is
  pool-scoped, so an existing VM outside the pool is invisible to the
  existence check — the play then tries to create it and dies on
  "VM 131 already exists". (`pveum pool list` doesn't render members;
  verify with `pvesh get /pools/homelab-svc`.)

- **host-backstop.nft input chain is default-drop.** It is now TEMPLATED
  from `lan_cidr` (`roles/svc_download/templates/host-backstop.nft.j2`), and
  the role asserts the rendered file matches `lan_cidr` before enabling
  nftables, so Ansible's own SSH survives. Tightening = change `lan_cidr`
  in group_vars (kept at the full /24 deliberately). Recovery is still
  `qm terminal 131` → `nft flush ruleset`.
- **peer.conf is wg(8) syntax** — the template emits only Interface/Peer;
  Address is WG_ADDR in vpn-netns.env, DNS in /etc/netns/vpn/resolv.conf.
- **RomM needs TWO env files** before it starts — `romm.env` (app secrets)
  AND `romm-db.env` (MariaDB bootstrap only; v5 splits them so provider API
  creds never enter the DB container). Required keys are in the quadlet
  headers; the role fails loudly listing them if either file is missing.
- **First run, no ntfy yet?** Pushes fail with a visible WARNING (not
  silently as before). Point `ntfy_url` at `https://ntfy.sh/<long-random>`
  in group_vars if you want pings before Phase 9.
- **PVE self-signed cert**: if you haven't put a trusted cert on :8006, set
  `pve_validate_certs: false` (default in group_vars/pve.yml).

## What this doesn't give you that Terraform would

Declarative VM *state* with a plan/apply/destroy lifecycle and a state file.
For two VMs on one node that's not worth the ceremony — `proxmox_kvm` is
idempotent enough (won't recreate an existing VMID), and `--check` covers
drift on the config side. If you later want true declarative infra, the
`bpg/proxmox` provider is the maintained choice; this repo's roles would
still do the in-guest half.
