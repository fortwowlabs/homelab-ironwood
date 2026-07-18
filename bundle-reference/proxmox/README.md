# Proxmox VM provisioning

Creates the two Rocky 10 service VMs from the official GenericCloud image with
cloud-init bootstrap. Run everything on the PVE host as root.

```
./fetch-image.sh                    # download + CHECKSUM-verify the image
./create-vm.sh vms/svc-media.env
./create-vm.sh vms/svc-download.env
```

## One-time host prereqs

```bash
# 1. Snippets content on the snippet storage (check current content first):
pvesm set local --content vztmpl,iso,backup,snippets

# 2. VLAN-aware bridge for svc-download (tag=40). /etc/network/interfaces:
#      auto vmbr0
#      iface vmbr0 inet static
#          ...
#          bridge-vlan-aware yes
#          bridge-vids 2-4094
ifreload -a          # non-disruptive on PVE; existing untagged VMs unaffected

# 3. TrueNAS VM boot ordering (the NFS dependency the service VMs have):
qm set <TRUENAS_VMID> --onboot 1 --startup order=1,up=120
# svc-media is order=2,up=90 and svc-download order=3,up=30 via the env files.
```

## Validation

```bash
qm agent 130 ping && qm agent 131 ping          # guest agent answering
ssh straderb@192.168.1.30  'cloud-init status --wait --long && id homelab'
ssh straderb@192.168.1.31 'cloud-init status --wait --long && id homelab'
#   homelab must be uid=10001 gid=10001 on both (TrueNAS export alignment)
ssh straderb@192.168.1.30  'df -h / ; getenforce'  # root fs grown; Enforcing
# svc-download reachability check is expected to be limited: pfSense rule 5
# blocks its general egress. `dnf` works only during a patch window (or via
# the standing 443 rule) — that is by design, not a provisioning failure.
```

## Rollback

```bash
qm stop 131; qm destroy 131 --purge     # removes disks, cloudinit, EFI vars
rm -f /var/lib/vz/snippets/svc-download-user.yaml
```
Fresh VMs are cattle — destroy and re-run `create-vm.sh` rather than hand-fix
a bad first boot.

## Gotchas

1. **CPU type is the one that bricks people on EL10.** Rocky/RHEL 10 userspace
   is compiled for x86-64-v3; Proxmox's default guest CPU (x86-64-v2-AES)
   makes the image fail to boot. The script forces `--cpu host`, which is
   correct on the 3700X (Zen 2 is v3-capable). If you ever move these VMs to
   a mixed cluster, use `x86-64-v3` instead of `host` for live-migration
   compatibility.
2. **`--cicustom user=` replaces PVE's generated user-data wholesale.**
   `qm set --ciuser/--sshkeys/--cipassword` are silently ignored once the
   snippet is attached. All users/keys live in `user-data.tmpl.yaml`.
   Network config (`--ipconfig0`) is a separate NoCloud file and still
   applies. After editing a snippet: `qm cloudinit update <vmid>` — and note
   cloud-init only consumes most modules on *first* boot; config changes to
   an already-bootstrapped VM belong in your normal config management, or
   destroy/recreate.
3. **Serial console.** GenericCloud images log to ttyS0; the script sets
   `--serial0 socket --vga serial0` so `qm terminal <vmid>` shows first boot.
   Don't switch vga back to std and then wonder where the console went.
4. **`.latest` is a moving symlink upstream.** fetch-image.sh verifies against
   the upstream CHECKSUM at download time and records a local sha256;
   create-vm.sh re-verifies against that record, so a later re-download can't
   silently change what you deploy. Air-gap habit applies: keep the verified
   qcow2 + .sha256 pair together.
5. **EPEL-in-same-boot race.** `epel-release` and EPEL packages
   (wireguard-tools) are requested in the same cloud-init `packages` run;
   most cloud-init versions order this fine, but if wireguard-tools is
   missing after first boot it's this — the bundle README's explicit
   `dnf install` covers it. Cloud-init here is bootstrap, not the system of
   record.
6. **Secure Boot is off** (`pre-enrolled-keys=0`) to avoid first-boot
   friction. Rocky's shim/kernels are signed, so you can enroll and enforce
   later if the STIG reflex demands it — do it before, not after, you add
   any out-of-tree modules.
7. **Ballooning is disabled** (`--balloon 0`): predictable RAM for ZFS-adjacent
   and download workloads beats overcommit on a 64 GB host with TrueNAS
   already resident.
8. **svc-download's host DNS** points at the VLAN40 gateway by default, which
   pfSense rule 3 blocks. Deliberate: the *host* barely needs DNS outside
   patch windows, and the *jail* has its own resolver via wg0. If that
   annoys you during initial setup, do the VM provisioning and bundle deploy
   before enabling pfSense rules 3/5 — the acceptance tests in the main
   README gate real traffic anyway.
9. **First-boot network on VLAN 40 requires the pfSense VLAN interface to
   exist first**, or cloud-init will bootstrap fine but package installs will
   hang. Order: pfSense VLAN + (permissive) rules → create-vm.sh → bundle
   deploy → tighten rules → acceptance tests.
