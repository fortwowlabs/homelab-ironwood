# DEPLOY — step-by-step runbook (v4, grounded to YOUR environment)

This version is rewritten against the actual audits of **thurgadin**
(Proxmox, 2026-05-31) and **convoker** (TrueNAS, 2026-05-31). Every value
below is real, not a placeholder — pool names, storage IDs, usernames,
domain. Where the previous DEPLOY.md said "adjust to your LAN," this one
already has.

**Conventions**
- Every command block starts with `## run on: <host>` — that is the box you
  type it on. `thurgadin` = PVE shell as root. `convoker` = TrueNAS
  (UI unless stated). `svc-download`/`svc-media` = SSH as `straderb`,
  then `sudo -i`.
- Each phase ends with a **GATE**. Do not continue until it passes.
- The rule that overrides everything: **no real download touches SABnzbd
  until Phase 4's Mullvad leak test passes.**

## Inventory (audited)

| host         | what                          | IP             | notes |
|--------------|-------------------------------|----------------|-------|
| router       | gateway + DNS                 | 192.168.1.1    | pfSense not deployed; flat LAN |
| thurgadin    | PVE 8.2.4, 3700X, 62 GiB      | 192.168.1.10   | domain `fort.wow`; bridge `vmbr0` (not VLAN-aware — fine, this variant doesn't need it) |
| convoker     | TrueNAS SCALE, **VM 100**     | 192.168.1.20   | **address is DHCP today** (fix in Phase 1); pool `sata_wd_14tb` (mirror, 7.25 TiB free, encrypted) |
| svc-media    | Rocky 10 VM, **VMID 130**     | 192.168.1.30   | created in Phase 5 |
| svc-download | Rocky 10 VM, **VMID 131**     | 192.168.1.31   | created in Phase 3 |
| kunark       | VM 111, old Tailscale exit    | —              | retired in Phase 8 |
| deployarr    | VM 112, old arr attempt       | —              | retired in Phase 2 (RAM) |

**Fixed decisions** (already baked into `proxmox/provision.env`):
VM disks on `nvme0pool` (1.2 TiB free — plenty for 160G+100G) · snippets on
`local` · admin user `straderb` · service uid/gid `10001` (`homelab`) ·
search domain `fort.wow` · DNS `192.168.1.1` · media dataset
`sata_wd_14tb/media` · backups dataset `sata_wd_14tb/backups`.

---

## PHASE 0 — Where am I? (state check + prerequisites)

Deployment may be partially done from a previous session. Determine your
resume point first:

```bash
## run on: thurgadin
qm list | grep -E '13[01]'            # 131 exists => Phase 3 done; 130 => Phase 5 done
ls /var/lib/vz/template/cache/Rocky-10-GenericCloud-Base.latest.x86_64.qcow2* 2>/dev/null
                                      # image + .sha256 present => Phase 2 image step done
showmount -e 192.168.1.20             # /mnt/sata_wd_14tb/media listed => Phase 1 done
```

| you see…                                   | resume at |
|--------------------------------------------|-----------|
| no media export on convoker                | Phase 1   |
| export ok, no image / no `provision.env`   | Phase 2   |
| VM 131 exists but `ip netns list` empty on it | Phase 4 |
| jail up, containers in it, canary green    | Phase 5   |

Off-box prerequisites (get these before Phase 4, order anytime):

1. **Mullvad**: account → WireGuard config generator → generate **2–3 server
   configs**. From each `.conf` note: numeric `Endpoint` IP, your assigned
   `Address` (`10.6x.x.x/32`), DNS `10.64.0.1`. You will NOT use the file
   as-is (Phase 4b strips it).
2. **Usenet**: Eweka (primary) + Usenet.Farm (block) credentials; NZBFinder
   and nzb.su API keys. Needed at Phase 7.
3. **SSH key** on the MBP (`ssh-keygen -t ed25519` if needed). Its `.pub`
   content must be readable on thurgadin at the path in `provision.env`
   (`ADMIN_SSH_PUBKEY_FILE`) — scp it there.
4. Copy this bundle to thurgadin:
   `scp homelab-vpn-bundle-v4.zip root@192.168.1.10:` and unzip.

**GATE:** at least one Mullvad config in hand (Endpoint IP + Address).

---

## PHASE 1 — TrueNAS (convoker): storage the whole stack depends on

Convoker is up and NFS is already running, but **none of what the stack
needs exists yet**: no `homelab` user, no media/backups datasets, and the
one existing NFS export (`/mnt/sata_wd_14tb/rust`) is unrestricted. Also
two infrastructure problems to fix while you're here.

### 1a. Pin the IP (currently DHCP)
`ip addr` on convoker shows a **dynamic** lease for 192.168.1.20. Everything
downstream (fstab on both VMs, host-backstop.nft) pins that IP. Either:
- convoker UI → Network → Interfaces → ens18 → uncheck DHCP, add alias
  `192.168.1.20/24` (+ set gateway 192.168.1.1 and DNS under Global
  Configuration), **Test Changes → Save**; or
- a DHCP reservation for convoker's MAC (`bc:24:11:8b:19:ac`) at .20 on the
  router. Either works; the reservation is lower-risk.

### 1b. Service account (uid/gid 10001)
UI → Credentials → Local Users → Add:
- Username `homelab`, uid **10001** (override the suggested uid)
- Create new primary group `homelab` — after saving, check Credentials →
  Groups and confirm gid is **10001**; if TrueNAS auto-picked another gid,
  edit the group and set 10001 explicitly
- Disable password, shell `nologin`, no home directory (`/var/empty`), SMB
  user **off**

### 1c. Datasets
UI → Datasets → `sata_wd_14tb` → Add Dataset:
- `media` — **one dataset, no children**. This is load-bearing: arr imports
  are hardlinks, hardlinks can't cross datasets, and a child dataset under
  `media` silently downgrades imports to full copies. Preset Generic /
  POSIX ACL. (It inherits the pool's encryption — expected; see 1e.)
- `backups` — same settings, separate dataset (no hardlink requirement).

Then the directory tree and ownership:

```bash
## run on: convoker (SSH as root)
mkdir -p /mnt/sata_wd_14tb/media/{downloads/complete/{tv,movies},tv,movies,audiobooks,ebooks,romm/roms}
chown -R 10001:10001 /mnt/sata_wd_14tb/media /mnt/sata_wd_14tb/backups
```

(No `downloads/incomplete` here — unpack churn stays on svc-download's
local disk; only completed jobs land on NFS. Phase 4/7 wire that up.)

### 1d. NFS exports
UI → Shares → UNIX (NFS) Shares → Add, one share each for:
- Path `/mnt/sata_wd_14tb/media`
- Path `/mnt/sata_wd_14tb/backups`

Both with: Hosts = `192.168.1.30` and `192.168.1.31` (nothing else),
Maproot User = `homelab`, Maproot Group = `homelab`. NFS service is already
enabled/running — no service change needed.

Side note while you're in there: the existing `/mnt/sata_wd_14tb/rust`
export has **no host restriction and no maproot**. Not this project's
problem, but worth tightening the same way.

### 1e. Encryption / reboot survival — do not skip
`sata_wd_14tb` is encrypted (aes-256-gcm, hex key, currently unlocked);
`rust-1tb` is sitting there **locked**, which proves not everything
auto-unlocks on this box. If `media` fails to unlock at boot, the NFS
exports serve nothing, the service VMs come up against empty mounts, and
the arrs see empty root folders — the whole "reboot converges unattended"
property dies right here. Verify once, deliberately:

```bash
## run on: convoker
zfs get -o name,value keystatus sata_wd_14tb        # must be: available
midclt call datastore.query storage.encrypteddataset | grep -c sata_wd_14tb
# >=1 means the key is stored in the TrueNAS config (auto-unlock at boot)
```

Then actually reboot convoker (`qm reboot 100` from thurgadin) and re-check
`keystatus` + `showmount -e` afterward. If it comes back `unavailable`,
UI → Datasets → sata_wd_14tb → Encryption → confirm the key is stored (not
passphrase-only) before proceeding.

**GATE:**
```bash
## run on: thurgadin (or the MBP)
showmount -e 192.168.1.20        # lists /mnt/sata_wd_14tb/media and /backups
mkdir -p /tmp/nfstest && mount -t nfs4 192.168.1.20:/mnt/sata_wd_14tb/media /tmp/nfstest
touch /tmp/nfstest/.probe && stat -c '%u:%g' /tmp/nfstest/.probe    # 10001:10001 (via maproot)
rm /tmp/nfstest/.probe && umount /tmp/nfstest
```
…and the reboot test in 1e passed.

---

## PHASE 2 — Proxmox host prep (thurgadin)

### 2a. RAM budget — you do not currently have room
Running VMs have ~50 GiB allocated of 62 GiB; `free` shows ~15 GiB
available. svc-media (10 GiB) + svc-download (6 GiB) won't fit on top.
`deployarr` (VM 112, 8 GiB) is the previous arr attempt this stack
replaces — take it out of the pool now, destroy it after cutover:

```bash
## run on: thurgadin
qm stop 112 && qm set 112 --onboot 0     # keep disks until Phase 7 works
free -g                                   # expect ~23 GiB available now
cat /sys/module/zfs/parameters/zfs_arc_max   # 0 = ARC may grow to 50% RAM;
# if 0, cap it (rpool+nvme0pool don't need 31 GiB of ARC on this box):
echo "options zfs zfs_arc_max=8589934592" > /etc/modprobe.d/zfs.conf && update-initramfs -u
# takes effect next host reboot; not urgent, just don't let it surprise you
```

kunark (VM 111, another 8 GiB) is retired in Phase 8 — leave it for now,
it's your only remote access path.

### 2b. Boot ordering — convoker has none set
VM 100 is `onboot: 1` but has **no startup order**, so nothing guarantees
TrueNAS (and its ZFS import + unlock) is up before the service VMs try to
mount NFS:

```bash
## run on: thurgadin
qm set 100 --startup order=1,up=120
# svc-media gets order=2,up=90 and svc-download order=3,up=30 from their env files
```

### 2c. Snippets + provision.env + image

```bash
## run on: thurgadin
pvesm set local --content vztmpl,iso,backup,snippets   # adds snippets to 'local'

cd homelab-vpn-bundle/proxmox
# provision.env ships pre-filled for this environment (nvme0pool, straderb,
# fort.wow, 192.168.1.1). Only edit if something changed:
grep -vE '^#|^$' provision.env
test -r "$(grep ADMIN_SSH_PUBKEY_FILE provision.env | cut -d= -f2 | awk '{print $1}')" \
  && echo "pubkey OK" || echo "FIX: put your MBP pubkey at that path"

./fetch-image.sh
```

**GATE:** fetch-image.sh prints `OK: …Rocky-10-GenericCloud…qcow2` and the
verified image + `.sha256` sit in `/var/lib/vz/template/cache/`.

---

## PHASE 3 — Create svc-download VM (131)

```bash
## run on: thurgadin
cd homelab-vpn-bundle/proxmox
./create-vm.sh vms/svc-download.env      # VMID 131, 192.168.1.31
qm terminal 131                          # watch first boot; Ctrl-] to exit
```

Validate:
```bash
## run on: thurgadin
qm agent 131 ping
ssh straderb@192.168.1.31 'cloud-init status --wait --long; id homelab; getenforce'
```

**GATE:** cloud-init `status: done` (a `degraded done` means read the
listed error — the known uid-quoting bug is already fixed in this bundle),
`homelab uid=10001 gid=10001`, SELinux `Enforcing`.

---

## PHASE 4 — Build the jail  ← the critical phase

### 4a. Ship + install files (one script now)
```bash
## run on: thurgadin (or MBP)
scp -r homelab-vpn-bundle/svc-download straderb@192.168.1.31:

## run on: svc-download (sudo -i first)
cd ~straderb/svc-download && ./deploy-files.sh
```
`deploy-files.sh` installs packages (EPEL + wireguard-tools + podman +
nfs-utils), places every script/unit/quadlet/nft file with correct modes,
seeds the two secret files **only if absent**, creates
`/etc/netns/vpn/resolv.conf`, creates appdata dirs owned 10001:10001, sets
`virt_use_nfs`, and runs the Quadlet dry-run. It prints exactly what still
needs your hand: the two files in 4b.

### 4b. Mullvad tunnel material (the only manual edits)
```bash
## run on: svc-download (root)
$EDITOR /etc/wireguard/peer.conf   # PrivateKey, Peer PublicKey, NUMERIC Endpoint
$EDITOR /etc/vpn-netns.env         # WG_ADDR=<Mullvad 'Address', e.g. 10.65.x.x/32>
```
peer.conf is `wg(8)` syntax, NOT wg-quick: **delete** `Address=`/`DNS=`
lines from what Mullvad gave you or `wg setconf` fails. Address goes in
WG_ADDR; DNS (10.64.0.1) is already handled.

### 4c. NFS mount (was missing from the old runbook entirely)
The dl-* quadlets bind `/srv/media` — mount it before containers start:
```bash
## run on: svc-download (root)
cat >> /etc/fstab << 'EOF'
192.168.1.20:/mnt/sata_wd_14tb/media  /srv/media  nfs4  hard,nofail,x-systemd.automount,x-systemd.mount-timeout=30  0 0
EOF
systemctl daemon-reload && mount /srv/media
sudo -u '#10001' touch /srv/media/.probe-dl && rm /srv/media/.probe-dl
```

### 4d. Host backstop — READ THIS BEFORE ENABLING
`host-backstop.nft` has a **default-drop input chain**. If the IP you SSH
from isn't in `LAN_ADMIN`, enabling it **locks you out** (this wedged the
build sandbox once already). Recovery exists (`qm terminal 131` →
`nft flush ruleset`) but don't need it:

```bash
## run on: svc-download (root)
grep LAN_ADMIN /etc/nftables/host-backstop.nft   # default 192.168.1.0/24 — covers the LAN
systemctl disable --now firewalld                # this file owns BOTH directions now
/usr/local/sbin/refresh-mullvad-endpoints.sh     # loads table + fills Mullvad set from peer.conf
grep -q host-backstop /etc/sysconfig/nftables.conf || \
  echo 'include "/etc/nftables/host-backstop.nft"' >> /etc/sysconfig/nftables.conf
systemctl enable --now nftables
# VERIFY SSH from a SECOND session before closing this one:
nft list chain inet host_backstop input | grep -A1 'dport 22'
```
Need dnf later? `nft -f /etc/nftables/patch-window.nft`, do the work,
`nft -f /etc/nftables/host-backstop.nft` to re-close.

### 4e. Bring up the jail
```bash
## run on: svc-download (root)
systemctl enable --now vpn-netns.service
ip netns exec vpn curl -4 --max-time 10 https://am.i.mullvad.net/connected
#   -> "You are connected to Mullvad"
curl -4 --max-time 5 https://ifconfig.me
#   -> must TIME OUT (host fenced by the backstop)
```

### 4f. Start the containers
```bash
## run on: svc-download (root)
systemctl start dl-sabnzbd dl-nzbhydra2 dl-prowlarr dl-sonarr dl-radarr
systemctl enable --now sab-proxy.socket hydra-proxy.socket \
  prowlarr-proxy.socket sonarr-proxy.socket radarr-proxy.socket
```

**GATE — the one that matters:**
```bash
## run on: svc-download (root)
for c in sabnzbd nzbhydra2 prowlarr sonarr radarr; do
  podman inspect -f "$c {{.NetworkSettings.SandboxKey}}" $c; done
#   every line: /run/netns/vpn
podman exec sabnzbd curl -4 -s https://am.i.mullvad.net/connected
#   "You are connected to Mullvad"
systemctl stop vpn-netns && systemctl is-active dl-sabnzbd   # -> inactive (fail-closed)
systemctl start vpn-netns dl-sabnzbd dl-nzbhydra2 dl-prowlarr dl-sonarr dl-radarr
systemctl enable --now leak-canary.timer
/usr/local/sbin/leak-canary.sh && echo PASS
```
All five in the jail; Mullvad confirmed from inside a container; kill test
drops the stack; canary PASS; host `ifconfig.me` times out. Only then does
real traffic ever flow.

---

## PHASE 5 — Create svc-media VM (130)

```bash
## run on: thurgadin
cd homelab-vpn-bundle/proxmox
./create-vm.sh vms/svc-media.env         # VMID 130, 192.168.1.30
ssh straderb@192.168.1.30 'cloud-init status --wait --long; id homelab'
```

NFS on svc-media:
```bash
## run on: svc-media (root)
setsebool -P virt_use_nfs 1
cat >> /etc/fstab << 'EOF'
192.168.1.20:/mnt/sata_wd_14tb/media    /srv/media    nfs4  hard,nofail,x-systemd.automount,x-systemd.mount-timeout=30  0 0
192.168.1.20:/mnt/sata_wd_14tb/backups  /srv/backups  nfs4  hard,nofail,x-systemd.automount,x-systemd.mount-timeout=30  0 0
EOF
mkdir -p /srv/media /srv/backups
systemctl daemon-reload && mount -a
sudo -u '#10001' touch /srv/media/.probe && rm /srv/media/.probe
```

**GATE:** both paths mount; the probe file writes as 10001.

---

## PHASE 6 — Media stack (rootless on svc-media)

```bash
## run on: svc-media (root)
loginctl enable-linger homelab          # cloud-init tries this; confirm anyway
install -d -o homelab -g homelab /opt/homelab/.config/containers/systemd
install -d -o homelab -g homelab /opt/homelab/appdata/{jellyfin/{config,cache},audiobookshelf/{config,metadata},romm/{config,assets,mysql},jellyseerr}
cp svc-media/quadlets/*.container svc-media/quadlets/romm.network \
   /opt/homelab/.config/containers/systemd/
chown homelab:homelab /opt/homelab/.config/containers/systemd/*
systemctl --user -M homelab@ daemon-reload
systemctl --user -M homelab@ start jellyfin audiobookshelf romm-db romm jellyseerr
firewall-cmd --permanent --add-port=8096/tcp && firewall-cmd --reload   # Jellyfin LAN direct-play
```

Access layer — **pick ONE** (they collide on port 8443):
- **Caddy** (what Phases 7–8 assume): all four apps get tailnet HTTPS
  vhosts. Install Tailscale first, then follow `caddy/README-caddy.md`:
  ```bash
  ## run on: svc-media (root)
  curl -fsSL https://tailscale.com/install.sh | sh
  tailscale up      # authenticate; note svc-media.<tailnet>.ts.net
  ```
  then the README's Caddy steps (TS_PERMIT_CERT_UID drop-in,
  /etc/sysconfig/caddy, deploy Caddyfile, `caddy validate`, enable).
- **tailscale-serve.sh**: simpler, no Caddy, but covers ABS + RomM only.
  Fine for a first pass; you'll want Caddy by Phase 7 for Jellyseerr.

**GATE:** `https://svc-media.<tailnet>.ts.net:8444` serves Jellyfin over
the tailnet; `http://192.168.1.30:8096` direct-plays on the LAN.

---

## PHASE 7 — Application wiring

Order matters (indexers → arrs → download client → request UI). UIs:
SAB `http://192.168.1.31:8080`, Hydra `:5076`, Prowlarr `:9696`,
Sonarr `:8989`, Radarr `:7878` (all via the veth proxies).

1. **SABnzbd**: Servers — Eweka priority 0, Usenet.Farm priority 1, both
   SSL/563, ~15–20 connections. Folders — Temporary `/incomplete-downloads`
   (VM-local disk, by design), Completed `/data/downloads/complete`.
   Categories `tv`, `movies`. Add to `host_whitelist` any hostname you'll
   ever front it with (IP access works without it).
2. **Prowlarr**: indexers (NZBFinder, nzb.su); Settings → Apps → Sonarr
   `http://127.0.0.1:8989`, Radarr `http://127.0.0.1:7878` (same netns,
   so localhost). Prowlarr pushes indexer config to both.
3. **Sonarr/Radarr**: root folders `/data/tv`, `/data/movies`; download
   client SABnzbd `127.0.0.1:8080`, matching category. Both see completed
   jobs at `/data/downloads/complete/...` — same `/data` mount as SAB, so
   imports hardlink.
4. **Jellyfin**: libraries `/media/tv`, `/media/movies`; create the
   girlfriend's user.
5. **Jellyseerr** (`:8447` via Caddy): connect Jellyfin, then Sonarr/Radarr
   at `192.168.1.31:8989/7878`. This is the URL you hand her.
6. **ABS / RomM**: audiobooks + ebooks libraries; RomM scans
   `/romm/library` → `roms/<platform>/`.

**GATE:** request a show in Jellyseerr → appears in Sonarr → SAB grabs it
→ import is a **hardlink**:
```bash
## run on: svc-download (root)
stat -c '%i %n' /srv/media/downloads/complete/tv/<file> /srv/media/tv/<show>/<file>
# same inode number = hardlink; different = you're copying, stop and check
# that SAB's completed dir and the arr root folders share the ONE dataset
```
→ plays in Jellyfin. Once this passes, `qm destroy 112 --purge` (deployarr)
whenever you're comfortable.

---

## PHASE 8 — Remote access (replaces kunark)

svc-media is already on the tailnet by name. For the rest of the LAN
(Proxmox UI, TrueNAS, svc-download admin), a small dedicated subnet-router
VM keeps the PVE host stock:

```bash
## run on: a tiny new Debian 12 VM (1 core / 512 MB / 8 G, flat LAN)
curl -fsSL https://tailscale.com/install.sh | sh
printf 'net.ipv4.ip_forward=1\nnet.ipv6.conf.all.forwarding=1\n' > /etc/sysctl.d/99-tailscale.conf
sysctl -p /etc/sysctl.d/99-tailscale.conf
tailscale up --advertise-routes=192.168.1.0/24 --advertise-exit-node
```
Approve the route (and exit node) in the admin console; disable key expiry
on this node; on the MBP `tailscale set --accept-routes`. Then retire
kunark — its containerized exit node has the known state-persistence bug
anyway: `qm stop 111 && qm set 111 --onboot 0`, destroy after a week of
the new path working.

**GATE:** from the MBP off-LAN: `https://192.168.1.10:8006` and Jellyfin
via Caddy both reachable, kunark powered off.

---

## PHASE 9 — Ops (none of this exists yet — create, don't verify)

The audits show **zero** vzdump jobs on thurgadin and **zero** snapshot /
replication tasks on convoker. Minimum viable:

- **PVE**: Datacenter → Backup → Add: VMs 100,130,131 (+ the subnet
  router), weekly, to `local` for now (PBS later if you stand one up).
  `qemu-guest-agent` is installed in both service VMs → fs-freeze
  consistent.
- **TrueNAS**: Data Protection → Periodic Snapshot Tasks:
  `sata_wd_14tb/media` (daily, keep 2 weeks) and `sata_wd_14tb/backups`
  (daily, keep 1 month). Scrubs already run (last one completed clean).
- **App-native**: Audiobookshelf scheduled backup → `/srv/backups`;
  arr configs via stop→rsync→start or `sqlite3 .backup`; `mariadb-dump`
  for RomM — all landing in `/srv/backups`.
- **ntfy on svc-media** (recommended): then point the canary `fail()` hook
  in `/usr/local/sbin/leak-canary.sh` at `http://192.168.1.30:<port>/homelab`
  — LAN-internal, so it works despite svc-download's locked egress.
- **Monthly-ish**: check `journalctl -t leak-canary` is green; after any
  Mullvad server swap, `refresh-mullvad-endpoints.sh`.
- **Updates**: `podman auto-update` labels are set; RomM's image should be
  pinned to a tag (its compose layout churns); snapshot the VM before
  manual pulls; host dnf via the patch window.

---

## Global rollback

- Jail/stack: `systemctl stop vpn-netns` (ExecStop deletes the netns;
  `Requires=` stops the containers). `nft delete table inet host_backstop`
  removes the backstop; re-enable firewalld if you do.
- Per-service: remove its `.container` file, `daemon-reload`, stop.
- VMs are cattle: `qm stop <id>; qm destroy <id> --purge` and re-run
  create-vm.sh rather than hand-repairing a bad boot.
- Nothing in this deploy touches the router, convoker's existing shares,
  or the PVE host beyond snippets content + two `qm set` lines.
