# Incident runbooks

Start with the failed Ansible task, service journal, and current host state. Do
not repeatedly converge a red environment: a retry can erase useful evidence
or turn a bounded failure into a migration. ntfy is advisory; absence of an
alert is not evidence of health.

For every incident, record start time, affected services, last known-good Git
commit and image digests, recent changes, commands run, and recovery evidence.

## Deployment or verification failure

1. Stop the rollout. Save the Ansible recap and first failed task.
2. Run `make preflight` to separate authentication/connectivity failures from
   guest failures. Do not render the real inventory with `--list`.
3. Limit investigation to the failed host and inspect relevant journals,
   mounts, containers, firewall state, and available capacity.
4. Run `make verify` only after the cause is corrected. It does not restart
   services; its temporary NFS write probes are removed in cleanup paths.
5. Run `make verify-disruptive` only when the download stack can tolerate a
   drill. Confirm it restored the exact previously running services, including
   LazyLibrarian.
6. Accept recovery only when every targeted host reports a verification fact
   and the play exits zero. A green notification without those facts is a bug.

For a failed migration, preserve both legacy and new appdata and follow
[Deployment rollback](deployment.md#rollback).

## Download canary `DOWN` or `LEAK`

`DOWN` means an expected catalog service/unit is unavailable. `LEAK` means a
download workload violated namespace or VPN identity policy; the canary stops
the full catalog and writes `/var/lib/leak-canary/tripped`.

1. Stop `leak-canary.timer` to keep the investigation stable.
2. Leave services stopped after `LEAK`. Inspect `vpn-netns.service`, each
   `dl-*.service`, the WireGuard peer, namespace membership, nftables, and the
   canary journal.
3. Repair the generated catalog/configuration rather than starting a container
   with ad hoc network flags.
4. Run `make verify`, start only the intended catalog units, run
   `make verify-disruptive`, and confirm the proxy for every app.
5. Clear the trip marker and restart the timer only after both checks pass.

See [Download maintenance](operations.md#download-maintenance-and-leak-canary-recovery)
for the normal recovery commands.

## Download VM SSH lockout

The nftables input policy is default-drop. If a bad policy severs SSH, use the
Proxmox console; do not disable host-key checking or widen another network to
work around it.

1. On the PVE host, open `qm terminal <svc-download-vmid>` using the VMID from
   inventory.
2. Capture `nft list ruleset` if possible. As an emergency-only recovery,
   `nft flush ruleset` restores access but removes the security backstop.
3. Keep download services stopped. Correct inventory/template data from the
   control node and run `make dl` immediately to reinstall the strict policy.
4. Run safe and disruptive verification before starting the canary or declaring
   recovery.

If the maintenance-egress window failed to close, treat it the same way: stop
downloads, restore strict policy, and verify. A package update is never a reason
to leave unrestricted egress in place.

## Proxmox API, hidden VM, or partial provisioning

An API timeout, TLS failure, or authorization error is not “VM absent.” Fix CA
trust or token ACLs and retry preflight; never set `pve_validate_certs: false`
to pass an incident.

If the API returns no VM but the SSH guard reports that the VMID exists:

1. On PVE, inspect `qm status <vmid>` and `qm config <vmid>`.
2. Confirm the VM name, disks, storage, bridge, cloud-init drive, and ownership.
3. If it is the intended service VM, back it up and deliberately adopt it into
   the configured API-token pool. If it is not, choose a different inventory
   VMID; never overwrite it.
4. Rerun preflight and provisioning. The API must now see the same object as
   the SSH check.

For a partial EFI/import/boot/resize sequence, do not delete disks or create
replacement volumes. Record `qm config`, ensure each completed step matches
inventory, and let the individually resumable role continue. A storage mismatch
requires a separate maintenance plan and backup; automatic disk movement and
shrinking are out of scope.

## Disk-capacity alarm or frozen VM

PVE pool exhaustion can freeze thin-backed guests before their own filesystem
alarms fire. An urgent PVE disk-guard alert takes priority over application
symptoms.

1. Stop deployments, backups, image pulls, and other avoidable writes.
2. On PVE, capture `zpool list`, `zfs list`, `df -h / /var/lib/vz`, and VM
   status. On a guest, capture `df -h` and `journalctl -p err`.
3. Identify growth by dataset/volume and reclaim or expand capacity according
   to the storage runbook. Do not blindly remove VM disks, snapshots, or the
   Rocky image cache.
4. Confirm pools and filesystems are below their configured thresholds, resume
   one VM at a time, and check NFS mounts and databases.
5. Run `make preflight`, `make verify`, the affected backup unit, and then the
   disruptive drill.

The VM disk-alert timer is managed by Ansible. The PVE disk guard is installed
manually from `contrib/`; test both alert paths after recovery.

## NFS or backup failure

1. Check TrueNAS availability/export restrictions, then use `findmnt -t nfs4`
   on both guests. A local directory at the mountpoint is not proof of NFS.
2. Check write access as the configured service UID. Do not change a mounted
   NFS root's ownership from a root-squashed client.
3. Stop applications that could write into an unmounted local path.
4. After NFS recovers, confirm no stray local files were hidden beneath the
   mount, then start the affected service.
5. Run the failed backup service manually, validate the new tar/gzip artifact,
   and run `make verify`. Verification must remove all temporary probe files,
   including on failure.

Use [Restore drills](operations.md#restore-drills) when an artifact is corrupt
or application data is already lost.

## DNS, Caddy, or service-name failure

1. Query `svc-media`'s dnsmasq directly, then query the normal LAN/tailnet
   resolver. Direct success isolates the failure to pfSense/Tailscale forwarding.
2. Check Caddy's journal and test the backend from `svc-media`. For a download
   service also check its generated proxy socket and nftables input port.
3. If TLS alone fails, compare the served certificate and locally installed
   Caddy root. Do not confuse it with the Proxmox CA.
4. Regenerate the access layer with `make access`; never edit rendered DNS,
   Caddy, or Homepage files in place.
5. Smoke-test every configured backend, not just the one first reported.

Jellyfin must remain reachable through its LAN-bound backend, and a Seerr
migration must produce `seerr` backup artifacts rather than stale Jellyseerr
paths.

## Secret exposure

1. Restrict access to the output/transcript and stop copying it. Do not spend
   time proving which values a viewer noticed; consider all populated decrypted
   values exposed.
2. Follow the issuer-by-issuer rotation procedure in
   [Credential exposure response](security.md#credential-exposure-response).
3. Remove transcript copies from the active tree and ensure both
   `docs/sessions/` and `docs/transcripts/` are ignored.
4. Scan the complete Git history and CI artifacts. Rewrite shared history only
   for a verified committed secret, with coordinated force-push and a second
   rotation where necessary.
5. Exit the incident only after new credentials pass targeted deployment,
   revoked credentials fail, Git/CI scans are clean, and revocation evidence is
   recorded without secret material.

## Historical lessons and document disposition

The retired top-level `CHANGES-v5.md`, `DEPLOYMENT-NOTES.md`, bundle reference,
and session transcript were point-in-time implementation records, not operator
interfaces. Their useful lessons are represented in these runbooks:

- verify must import only verification tasks and must fail non-zero;
- pool-scoped API visibility needs an independent VMID guard;
- NFS probes run as the service UID because root squash is intentional;
- package/image egress is temporary and must restore the nftables backstop;
- migrations are conditional on real legacy units/files;
- Seerr, LazyLibrarian, and all catalog services participate in backup,
  verification, canary, and recovery behavior.

Git history is the archive for obsolete narratives. The six linked guides in
the repository README are the only canonical operator documentation.
