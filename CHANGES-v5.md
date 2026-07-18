# v4 → v5

Fixes from the independent review (items numbered as triaged) plus three
issues found during verification of that review. Behavior-relevant only;
comment-level corrections not listed.

## Broken automation, fixed
1.  **Tag routing works.** `site.yml`/`verify.yml` includes are `always`-tagged;
    every documented `--tags` slice now runs instead of silently no-opping.
    New: `--tags provision` slice, `make reconcile`, `make access`.
2.  **Tailscale out of cloud-init.** It isn't in Rocky/EPEL repos; the failed
    dnf transaction silently dropped podman/nfs-utils too. svc_media installs
    from Tailscale's official EL10 repo, authenticates unattended when
    `vault_tailscale_authkey` is set, and deploys Caddy (ts.net vhosts) once
    Running. Cloud-init gates are now strict on BOTH VMs: `degraded done` is
    a failure, not a pass.
3.  **NZBHydra2 removed** (quadlet, proxy pair, port 5076, gates). A
    retire-leftovers task cleans a v4 host on upgrade.
A.  **verify.yml is actually read-only.** v4's `apply:{tags:[verify]}` added
    the tag to every task — nightly "verify" was a full converge.
B.  **Failed verifies can't be silent.** Rescue/alert tasks are `always`-tagged;
    v4 tag-skipped them under `--tags verify`, so the rescue "succeeded" and
    the play exited 0 with no ntfy.

## Jail / canary
10. **Canary rewritten** (now templated): explicit expected-container list,
    unit-active checks, persistent tripped flag (`/var/lib/leak-canary/
    tripped`) that keeps alerting until cleared, real ntfy delivery, LEAK
    (stop stack) vs DOWN (alert only) severity split.
C.  **Backstop opens canary alert egress** to LAN ntfy (svc-media:port) —
    conditional on `ntfy_url` pointing at svc-media; v4's own default-drop
    output chain would have eaten the alert curl.
12. **Endpoint ports derived from peer.conf.** `mullvad_ports` set joins
    `mullvad_wg`; refresh script populates both. No 51820 assumption.
11. **Proxy sockets:** `FreeBind=yes` + network-online ordering (boot race
    binding 192.168.1.31 before NM configured it).

## Media stack
7.  **Jellyfin publishes on the LAN IP only** + firewalld rich rule scoped to
    `lan_cidr` (v4's 0.0.0.0 publish + unscoped open exposed it to the whole
    tailnet the moment tailscaled came up). v4's unscoped rule is removed on
    upgrade.
6.  **Transcode tmpfs actually used:** `/cache/transcodes` (image default
    path), 6g. v4 shielded `/transcodes`, which nothing wrote to.
8.  **RomM:** matches the upstream 4.9.x two-service layout (embedded Valkey
    — deliberately NOT a third container); image pinned `4.9.2`; secrets
    split into `romm.env` (app) + `romm-db.env` (DB bootstrap) so provider
    creds stay out of the DB container; `Notify=healthy` on romm-db so romm
    waits for a READY database.
5.  **NFS mount guards on every quadlet touching /srv/*:** rootful units get
    `RequiresMountsFor`, all get an ExecStartPre stat+findmnt (stat trips the
    automount, findmnt proves nfs4 — no more writing into an empty
    mountpoint dir). User units can't use RequiresMountsFor on system mounts;
    the ExecStartPre covers them.
4.  **Container-level NFS probes** (podman exec read/write tests per
    container) + subuid/subgid assertions, in deploy and nightly verify.

## Lifecycle / supply chain
9.  **Auto-update labels removed** (no podman-auto-update.timer existed; the
    labels advertised updates that never ran). Pin-after-burn-in procedure in
    README.
15. **Rocky image pinned to a dated build** with exact-filename BSD-checksum
    verification (v4 matched the hash against ANY line and pinned the mutable
    `.latest`). `.latest.` pins now refused by assertion. Bump procedure in
    group_vars.
13. **Opt-in VM reconcile** (`make reconcile`): cores/memory/onboot/startup
    pushed to existing VMs deliberately; create-once remains the default.
14. **API token scoped:** pool `homelab-svc` + the two storages, privsep ON,
    ACLs on user AND token. Commands in `all_vault.yml.example`. (Honest
    limit: qm import/EFI steps are root-over-SSH regardless.)
3b. **ntfy failures are loud** (WARNING in play output) instead of silent;
    first-run bootstrap options documented.

## New
-   **Backups as code:** nightly timers — svc-media 03:05 (RomM
    `mariadb-dump --single-transaction` + jellyfin/jellyseerr/romm appdata
    tars, runs as homelab), svc-download 03:10 (SAB/arr appdata tars via
    runuser), 14-day retention, ntfy on failure. ABS: built-in backup now
    lands on NFS via the new `/config/backups` bind; schedule once in the UI.
    Restore drills remain manual — quarterly.

## Operator deltas on upgrade from v4
-   Re-create the PVE token scoped (or keep the old one and skip — new
    commands in the vault example). Create pool `homelab-svc`.
-   Add `vault_tailscale_authkey` (may be empty) to the vault.
-   Create `/opt/homelab/appdata/romm/romm-db.env` on svc-media (keys in the
    romm-db quadlet header) before the media play.
-   Strict cloud-init gates: a `degraded done` guest that used to pass now
    fails — that is the point.
-   After a LEAK alert: `rm /var/lib/leak-canary/tripped` is now part of the
    recovery, or the canary keeps paging you (also the point).
