# v3 → v4 — grounded to the audited environment (2026-05-31 audits)

## Mismatches between the old runbook and reality (all fixed)

1. **`STORAGE=local-nvme` didn't exist.** Actual PVE storage is `nvme0pool`
   (this already bit you once). provision.env now ships pre-filled.
2. **`tank/media` didn't exist — there is no `tank` pool.** All paths are
   now `sata_wd_14tb/media` and `sata_wd_14tb/backups`, including both
   fstab blocks.
3. **`SEARCHDOMAIN=home.arpa` vs actual domain `fort.wow`.** Fixed.
4. **`ADMIN_USER=brandon` / `ssh brandon@` vs actual `straderb`.** Fixed.
5. **Phase 1 said "if TrueNAS isn't up, that's a separate project."**
   Convoker is up but has none of the required objects: no `homelab`
   uid-10001 user, no media/backups datasets, no restricted NFS exports.
   Phase 1 is now concrete click-path + shell steps on convoker.
6. **Convoker's IP is a DHCP lease**, while fstab and host-backstop.nft pin
   192.168.1.20. Phase 1a pins it.
7. **VM 100 (convoker) has no startup order**, so the TrueNAS-before-NFS
   boot dependency the design assumes wasn't actually enforced. Phase 2b:
   `qm set 100 --startup order=1,up=120`.
8. **RAM doesn't fit**: ~50 GiB of 62 allocated to running VMs; the two new
   VMs need 16 GiB. Phase 2a retires deployarr (VM 112) first and caps ZFS
   ARC on the host; kunark (VM 111) goes in Phase 8.
9. **No vzdump jobs on PVE, no snapshot/replication tasks on TrueNAS** —
   Phase 9 rewritten as "create," not "verify."
10. **Encrypted-pool boot risk**: `sata_wd_14tb` is encrypted and
    `rust-1tb` sits locked today, proving auto-unlock isn't universal on
    this box. A locked media dataset = empty NFS exports = the whole
    unattended-reboot story fails. Phase 1e adds a keystatus check and a
    deliberate reboot test.

## Internal bundle inconsistencies (fixed)

11. **SAB ↔ arr path mismatch broke hardlinks AND imports.** SAB mounted
    `/srv/downloads/complete:/downloads` while Sonarr/Radarr expected
    completed jobs at `/data/downloads/complete` on `/srv/media`. Different
    host paths (different filesystem ⇒ copy, not hardlink) and different
    container paths (arr can't find SAB's reported path). dl-sabnzbd now
    mounts the same `/srv/media:/data`; incomplete stays on a local dir
    (`/var/lib/sabnzbd-incomplete`) for unpack speed.
12. **svc-download never mounted NFS in the old runbook** — the quadlets
    bind `/srv/media`, but no fstab step existed for that VM. Phase 4c.
13. **Appdata directories were never created** on either VM. deploy-files.sh
    (svc-download) and Phase 6 (svc-media) create them owned 10001:10001.
14. **README claimed tailscale-serve.sh "replaces Caddy entirely" while
    DEPLOY required Caddy.** Resolved: Caddy is the access layer; the serve
    script is the documented no-Caddy alternative; don't run both (:8443).

## New

- `svc-download/deploy-files.sh` — Phase 4a in one idempotent script
  (packages, files, modes, secrets-if-absent, netns resolv.conf, appdata
  dirs, SELinux boolean, quadlet dry-run). Never overwrites peer.conf or
  vpn-netns.env.
- `proxmox/provision.env` — pre-filled (the .example remains for reference).
- DEPLOY.md — every block labeled with the host it runs on; Phase 0 is now
  a state-check with a resume table (deployment was already partially
  started); all gates preserved, lockout warning preserved.

## Verified against current sources at rewrite time

- Rocky 10 GenericCloud image + CHECKSUM URLs at dl.rockylinux.org still
  valid, cloud-init pre-installed per Rocky 10 docs.
- Still verify `dnf repoquery wireguard-tools` on the first Rocky 10 boot
  (EPEL packaging on EL10 is the standing caveat) — deploy-files.sh fails
  loudly if the install doesn't land.
