# Unattended-6-months alerting: ntfy + healthchecks.io dead-man's switch

## Context

The environment will run unattended for six months. Today, alerting is partial and inconsistent: everything posts to one unauthenticated ntfy topic (`homelab-deploy`), **no systemd unit anywhere has an `OnFailure=` handler**, certbot renewal failure is silent (the single most likely 6-month break: wildcard cert expiry), failed units are only noticed when `make verify` runs from the workstation, disk alerts spam every 15 min with no dedupe and ignore NFS, the Proxmox host is unmanaged (manual contrib diskguard, no SMART/zed/scrub), and — worst — **silence is ambiguous**: if ntfy, svc-media, or the whole homelab dies, the phone goes quiet exactly as if everything were healthy.

User decisions: healthchecks.io free tier as the external dead-man's switch; ntfy stays unauthenticated; new minimal `pve_mon` role over SSH (root@192.168.1.10); nightly `make verify` moves onto svc-infra via a timer.

Verified constraints:
- `verify.yml` targets only `service_vms` + localhost — the svc-infra runner needs SSH to the 3 VMs, no PVE API.
- All backup/canary/diskalert timers are **system** units → system-scope `OnFailure=` drop-ins cover them all.
- svc-download's nftables backstop only opens egress to LAN ntfy — **no healthchecks pings from svc-download**; its backup freshness is checked from svc-infra over the shared backups mount.
- `/etc/homelab-notify.env` (from `roles/service_vm/templates/notify.env.j2`) is the shared env pattern — extend it, don't duplicate.
- `tests/validate_systemd_units.py` pins the contrib `homelab-verify@` unit content — promoting those units means updating that test in the same branch.

## Alert taxonomy (shapes everything)

Two topics: keep `homelab-deploy` for info (deploy/verify OK — phone: muted), add `ntfy_alert_topic: "homelab-alerts"` for anything actionable (phone: sound, urgent bypasses DND). Add `NTFY_ALERT_TOPIC` to `notify.env.j2`; alert-class scripts use `${NTFY_ALERT_TOPIC:-$NTFY_TOPIC}`; `roles/notify/tasks/main.yml` gains optional `ntfy_topic_override` used by the site.yml/verify.yml rescue calls.

## Branch 1 — `feat/onfailure-core`

1. **Universal notifier** (service_vm role): `roles/service_vm/templates/notify-failure.sh.j2` → `/usr/local/sbin/notify-failure.sh` (posts unit name + last 20 journal lines, urgent, to alerts topic; `curl --retry 2`, falls back to `logger`, never exits nonzero) + `roles/service_vm/files/notify-failure@.service`. New `roles/service_vm/tasks/onfailure.yml` loops drop-ins `/etc/systemd/system/<unit>.d/10-onfailure.conf` (`OnFailure=notify-failure@%n.service`) over per-group `onfailure_units` lists in group_vars: base `homelab-diskalert.service`; media adds `backup-media.service`, `certbot-renew.service`, `homelab-certwatch.service`; download adds `backup-dl-appdata.service`, `leak-canary.service`, `vpn-netns.service`; infra adds `backup-infra-appdata.service`, `homelab-verify@.service` (template-name drop-in dir), `homelab-backups-fresh.service`.
2. **Failed-unit watcher** (roles/mon, all 3 VMs): `failed-units-watch.sh.j2` + `homelab-failedunits.{service,timer}` (15 min). Collects system + rootless `--user` failed sets (same invocation as `roles/service_vm/tasks/failed-units.yml`); state file `/var/lib/homelab/failed-units.state`; alerts on set change, re-alerts every 6h, sends all-clear on recovery; always exits 0.
3. **Cert-expiry watcher** (svc-media): `homelab-certwatch.{service,timer}` daily — `openssl x509 -checkend` at 21 days (`cert_expiry_days`) against the live wildcard fullchain; exits 1 on near-expiry → OnFailure notifier carries it. certbot-renew gets an OnFailure drop-in via the list above.
4. **disk-alert rework** (`roles/mon/templates/disk-alert.sh.j2`): include NFS mounts (separate `disk_alert_nfs_threshold: 90`), state-file hysteresis (re-alert every 6h, recovery message), keep exit-0 contract.
5. **New gate** `tests/validate_onfailure.py`: every `*.timer`-triggered service in the repo must appear in an `onfailure_units` list or an explicit allowlist. Wire into `make validate`.

Verify by using it: `systemd-run --unit=testfail -p OnFailure=notify-failure@%n.service /bin/false` → phone alert with journal lines; watcher run twice → one alert, no dup; `reset-failed` → all-clear; certwatch with `-e cert_expiry_days=3650` → alert; disk-alert with `-e disk_alert_threshold=1` → one alert then silence.

## Branch 2 — `feat/healthchecks`

Five checks (free tier holds 20), generous grace:

| Check | Pinged by | Cadence/grace |
|---|---|---|
| homelab-verify | verify wrapper on svc-infra (branch 4); `/fail` on nonzero | daily / 6h |
| homelab-backups | new `homelab-backups-fresh` on svc-infra (04:30, checks all 3 VMs' newest backup mtime <26h over the backups mount; stale → HC `/fail` + exit 1, OnFailure carries the ntfy) | daily / 6h |
| homelab-pve | daily `homelab-pve-health` on PVE (branch 3) | daily / 6h |
| homelab-media-heartbeat | daily timer on svc-media (proves the ntfy host itself lives) | daily / 3h |
| homelab-scrub | monthly zpool scrub service on PVE | 32d / 3d |

UUIDs as `vault_hc_ping_{verify,backups,pve,media_heartbeat,scrub}` (placeholders in `all_vault.yml.example`), rendered into `/etc/homelab-healthchecks.env` 0600 root `no_log: true` (auto-enforced by validate_secret_tasks.py). Never rendered on svc-download. One-time manual step: create the 5 checks in the healthchecks.io UI, configure its email+push notification target, paste UUIDs via `make vault-edit`.

Verify: run backups-fresh manually → ping visible on HC dashboard; force failure via `-e` threshold → `/fail` event + phone alert; pause a check and confirm healthchecks' own external notification arrives.

## Branch 3 — `feat/pve-mon`

Inventory: new SSH host group `pve_mon_hosts` → `thurgadin-ssh` (`ansible_host: 192.168.1.10`, `ansible_user: root`); the API-driven `thurgadin` host is untouched. Check preflight.yml's IP-uniqueness assert.

New `roles/pve_mon/`:
- diskguard promoted from `contrib/bin/pve-diskguard.sh` + `contrib/systemd/homelab-diskguard.*` into role files (delete contrib copies, fix doc links → validate_links).
- notify env + `notify-failure@.service` (reuse service_vm template via relative `src`; unit duplicated, gate asserts copies identical), failed-units watcher (system scope).
- **zed**: `zed.rc` sets `ZED_EMAIL_PROG=/usr/local/sbin/zed-ntfy.sh` (+interval 3600); hook is arg-agnostic (posts args + stdin) because the calling convention varies by zfs version. Restart zed handler.
- **smartd**: `DEVICESCAN -a -o on -S on -n standby,q -m root -M exec /usr/local/sbin/smartd-ntfy.sh -M daily`; hook reads `SMARTD_*` env → urgent ntfy.
- **`homelab-scrub.{service,timer}`** monthly (`zpool scrub -w` each pool; success → HC scrub ping) and **`homelab-pve-health.{service,timer}`** daily (`zpool status -x` healthy + no failed units → HC pve ping, else exit 1 → OnFailure).
- `tasks/verify.yml`: timers active, config content asserts, `zpool status -x` healthy.

site.yml gets a `pve_mon_hosts` play with the standard rescue→notify block; Makefile gets `make pve`. **PVE stays out of nightly verify** (runner uses `--limit 'service_vms:localhost'`); its daily HC ping covers liveness. Workstation `make verify` runs everything.

Verify: testfail transient unit on PVE → phone; smartd test-mode one-shot alert; pve-health run → HC ping; scrub the smallest pool.

## Branch 4 — `feat/verify-runner`

Nightly verify runs on svc-infra from a **push-from-workstation** checkout (no GitHub credentials on the VM; the code that ran the last deploy is what verifies nightly):

- `svcops` user on svc-infra; checkout at `/opt/homelab-iac` synced at deploy time via `git archive HEAD` on localhost → unarchive (tracked files only; `.vault_pass`/`.venv` survive alongside; HEAD sha recorded in `.deployed-rev`).
- venv built on-host (`requirements.txt` checksum stamp for idempotency; svc-infra egress is open).
- svcops gets an ed25519 keypair; pubkey installed into straderb's authorized_keys on all VMs (`authorized_key`, `from="<svc-infra-ip>"` restricted); `StrictHostKeyChecking=accept-new` in svcops ssh config. Implementation-time check: straderb NOPASSWD sudo (cloud-init) — add sudoers drop-in if absent.
- **Vault password is a manual one-time step** (`install -m 0600` over SSH, documented); svc-infra's verify asserts the file exists with mode 0600 so a forgotten step fails loudly before departure.
- Wrapper `/usr/local/sbin/homelab-verify-run.sh`: runs `ansible-playbook verify.yml --vault-password-file .vault_pass --limit 'service_vms:localhost' -e notify_on_success=false`; success → HC ping, failure → HC `/fail` + exit 1 (OnFailure + playbook-rescue ntfy; if ntfy is dead, healthchecks escalates externally).
- Promote `contrib/systemd/homelab-verify@.{service,timer}` into `roles/svc_infra/files/` with ExecStart = wrapper; update `tests/validate_systemd_units.py` accordingly; enable `homelab-verify@svcops.timer` (03:30).

Verify: `systemctl start homelab-verify@svcops.service` → HC ping; force a failure (bogus limit) → HC `/fail` + phone alert.

## Branch 5 — `chore/departure-runbook`

`docs/operations.md`: "6-month unattended" runbook — healthchecks account/notification setup, phone ntfy topics (subscribe both, mute deploy), vault-pass install step, what each alert means + first response, the 5 HC checks table, remote recovery path (Tailscale → cockpit). Touch README/CLAUDE.md for `make pve` and the new gate.

## Sequencing

1 → 2 → 3 → 4 → 5, each via the full CLAUDE.md workflow (branch → validate → iterate deploy → functional phone-verified test → commit → clean tree → final deploy `changed=0` → `make verify` → merge/push → delete branch). Branch 2 needs 1 (topic + OnFailure list); branch 4 needs 2 (HC env) and goes last.

## Risks / implementation-time checks

- straderb NOPASSWD sudo on VMs (branch 4).
- zed hook arg convention on PVE's zfs version — hook stays arg-agnostic.
- preflight.yml uniqueness assert vs. new 192.168.1.10 ansible_host.
- Alert-storm note: a persistently failing 15-min timer alerts each run — actionable by design; the watcher's 6h dedupe is the backstop.
- healthchecks.io UUIDs are secrets: vault + no_log, never echoed.

---

## Appendix: detailed file-by-file design

All assumptions below were verified against the repo (verify.yml, Makefile, hosts.yml,
roles/notify, roles/mon, contrib units, host-backstop.nft.j2, maintenance-egress.sh.j2,
backup scripts, validate_* tests).

Key verified facts that shape the design:
- verify.yml targets only `service_vms` + a localhost summary play — the nightly runner
  needs SSH to the 3 VMs only, NOT the PVE API (PROXMOX_CA_PATH is irrelevant to verify).
- All backup/canary/diskalert timers are SYSTEM units (backup-media.service has
  User=homelab but is system-scope) → system-level OnFailure= drop-ins cover every timer.
  Rootless quadlet failures are already surfaced by `systemctl --user --failed` — but only
  during verify; the new watcher covers between-runs.
- svc-download backstop only opens egress to LAN ntfy (host-backstop.nft.j2:58-66);
  hc-ping.com from svc-download would need a DNS-dependent 443 hole → avoid: do all
  healthchecks pings from svc-media/svc-infra/PVE, and check download-backup freshness
  from svc-infra over the shared NFS backups mount.
- notify.env pattern: /etc/homelab-notify.env (NTFY_URL/NTFY_TOPIC/NTFY_TOKEN) from
  roles/service_vm/templates/notify.env.j2 — extend, don't duplicate.
- contrib/systemd/homelab-verify@.{service,timer} content is pinned by
  tests/validate_systemd_units.py — moving/changing them requires updating that test.

---

## Decision 8 first (it shapes everything): alert taxonomy

Two topics, one env file:
- `homelab-deploy` (existing, keep): informational — deploy/verify OK, scrub OK. Phone: muted/min priority.
- `homelab-alerts` (new): anything actionable — all OnFailure, watcher, canary, disk,
  cert-expiry, diskguard, zed, smartd. Phone: default+sound, urgent overrides DND.

Changes:
- `inventory/group_vars/all/main.yml`: add `ntfy_alert_topic: "homelab-alerts"` next to
  ntfy_topic (~line 137).
- `roles/service_vm/templates/notify.env.j2`: add `NTFY_ALERT_TOPIC={{ ntfy_alert_topic | quote }}`.
- Existing alert-class scripts (leak-canary.sh.j2, disk-alert.sh.j2, backup ERR traps,
  pve-diskguard) switch to `${NTFY_ALERT_TOPIC:-$NTFY_TOPIC}` (fallback keeps them working
  mid-rollout). roles/notify rescue calls pass `ntfy_topic_override: "{{ ntfy_alert_topic }}"`
  — add optional `{{ ntfy_topic_override | default(ntfy_topic) }}` in roles/notify/tasks/main.yml.
- Phone runbook note in docs: subscribe to both topics, mute homelab-deploy.

---

## Branch 1: `feat/onfailure-core` — universal notifier, watcher, cert watcher, disk-alert rework

### 1a. Universal OnFailure notifier (service_vm role, reused by pve_mon)
New files:
- `roles/service_vm/templates/notify-failure.sh.j2` → /usr/local/sbin/notify-failure.sh (0755)
  - args: `$1` = failed unit name. Sources /etc/homelab-notify.env.
  - Posts to `${NTFY_URL}/${NTFY_ALERT_TOPIC}`: Title "UNIT FAILED on $(hostname -s): $1",
    Priority urgent, Tags rotating_light, body = `journalctl -u "$1" -n 20 --no-pager -o cat`
    (truncate to ~3800 bytes). `curl -fsS --max-time 10 --retry 2`; on curl failure `logger`
    and exit 0 (never cascades).
- `roles/service_vm/files/notify-failure@.service` → /etc/systemd/system/
  - `[Service] Type=oneshot ExecStart=/usr/local/sbin/notify-failure.sh %i`
    `EnvironmentFile=/etc/homelab-notify.env` (env in unit, not script, matching diskguard pattern).
- `roles/service_vm/tasks/onfailure.yml` (included from main.yml): installs script+unit,
  then loops a drop-in `/etc/systemd/system/<unit>.d/10-onfailure.conf` containing
  `[Unit]\nOnFailure=notify-failure@%n.service` over a per-host var `onfailure_units`:
  - group_vars/all: base list `[homelab-diskalert.service]`
  - svc-media host adds: backup-media.service, certbot-renew.service, homelab-certwatch.service
  - svc-download adds: backup-dl-appdata.service, leak-canary.service, vpn-netns.service
  - svc-infra adds: backup-infra-appdata.service, homelab-verify@svcops.service (branch 4),
    homelab-backups-fresh.service (branch 2)
  - daemon_reload handler.
  Define `onfailure_units` per group in `inventory/group_vars/all/main.yml` +
  new small `inventory/host_vars/` additions if the repo prefers host_vars (check convention;
  cockpit_origin lives in host_vars). NOTE: drop-in dir for a template unit instance is
  `homelab-verify@.service.d/` (applies to all instances) — use the template name.

### 1b. Failed-unit watcher (roles/mon — it already owns the 15-min timer pattern)
- `roles/mon/templates/failed-units-watch.sh.j2` → /usr/local/sbin/failed-units-watch.sh
  - Collects `systemctl --failed --plain --no-legend` plus, when homelab user exists
    (media/infra; guard with `id homelab`), `runuser -u homelab -- env XDG_RUNTIME_DIR=/run/user/{{ svc_uid }} systemctl --user --failed ...`
    (same invocation as roles/service_vm/tasks/failed-units.yml). On svc-download also
    nothing extra (rootful).
  - Dedupe: state file `/var/lib/homelab/failed-units.state` holding `sha256(sorted set)` +
    last-alert epoch. Alert when set changes (including recovery → send an "all clear",
    default priority, alerts topic) or when nonempty and last alert >6h old. Exit 0 always.
- `roles/mon/files/homelab-failedunits.{service,timer}`: timer OnBootSec=5min,
  OnUnitActiveSec=15min, Persistent=true. Service gets its own OnFailure drop-in? No —
  the watcher never exits nonzero; skip (and exclude it in the validate gate allowlist).
- Tasks appended to roles/mon/tasks/main.yml (tags [failedunits]) + verify.yml assertion
  (timer active, script present). mon runs on all three VMs already (verify.yml asserts
  mon_verified for all 3 — confirm mon is in each host's main play; it is per verify.yml).
- PVE gets the same script via pve_mon (branch 3), system scope only.

### 1c. Cert-expiry watcher (svc-media)
- `roles/svc_media/templates/certwatch.sh.j2` → /usr/local/sbin/homelab-certwatch.sh
  - `openssl x509 -checkend $(( {{ cert_expiry_days | default(21) }} * 86400 )) -noout -in
    /etc/letsencrypt/live/{{ service_domain }}/fullchain.pem` (confirm live path name from
    roles/svc_media/tasks/access.yml certonly invocation — use the same cert-name var).
  - On failure: exit 1 (unit fails → OnFailure notifier fires with journal context; script
    prints days-remaining first). Missing file also exit 1.
- `roles/svc_media/files/homelab-certwatch.{service,timer}` — daily 09:00, Persistent=true.
- certbot-renew.service OnFailure drop-in via 1a list.
- Installed from roles/svc_media/tasks/access.yml (certbot section), verify assertion added
  to roles/svc_media/tasks/verify.yml near line 236 (certbot-renew.timer check).

### 1d. disk-alert dedupe + NFS coverage
Rework `roles/mon/templates/disk-alert.sh.j2`:
- Include NFS: drop `-x nfs -x nfs4`, but dedupe df rows by device and use a separate
  threshold `disk_alert_nfs_threshold | default(90)` for nfs mounts (NAS is big; 85% of
  20TB is not urgent) — detect via `df -PT` fstype column.
- State/hysteresis: `/var/lib/homelab/disk-alert.state` — same shape as watcher: alert on
  new mount crossing or every `disk_alert_realert_hours | default(6)`h while any remain
  over; send recovery message when the set clears.
- Keep exit-0-always contract.

### 1e. Validate gate (branch 1)
- `tests/validate_onfailure.py`: statically walk roles/*/files/*.timer +
  roles/*/templates/*.timer.j2, derive triggered service names, and assert each appears in
  an `onfailure_units` list in group_vars OR in an explicit allowlist
  (homelab-failedunits, homelab-diskalert already notifies internally — decide: diskalert
  SHOULD have OnFailure too since a broken script is silent; include it).
  Wire into Makefile `validate-systemd` target + validate list.
- shellcheck coverage of new .j2 scripts is automatic (validate_shell_templates.py).

### Branch 1 functional verification
- Deploy; `systemctl start notify-failure@fake.service` manually? Better: create a
  transient failing unit: `systemd-run --unit=testfail -p OnFailure=notify-failure@%n.service /bin/false`
  → phone gets urgent alert with journal lines.
- `systemctl start homelab-failedunits.service` with testfail failed → alert; run again →
  no duplicate; reset-failed → all-clear message.
- Temporarily set cert_expiry_days=3650 via -e → certwatch fails → OnFailure alert.
- Fill a tmp file? For disk-alert, temporarily set threshold to 1 via -e and run the
  service → one alert; run again → silence; check state file.

---

## Branch 2: `feat/healthchecks` — dead-man's switch

Checks (5 of 20 free-tier slots; grace generous — reliability over latency):
| Check (hc name)      | Pinged by                                   | Schedule/grace |
|----------------------|---------------------------------------------|----------------|
| homelab-verify       | verify wrapper on svc-infra (branch 4)      | daily / 6h     |
| homelab-backups      | new backups-fresh checker on svc-infra      | daily / 6h     |
| homelab-pve          | daily pve-health service on PVE (branch 3)  | daily / 6h     |
| homelab-media-heartbeat | daily timer on svc-media (proves the ntfy host itself is up) | daily / 3h |
| homelab-scrub        | monthly zpool scrub service on PVE          | 32d / 3d       |

Files:
- `inventory/group_vars/all_vault.yml.example`: placeholders `vault_hc_ping_verify`,
  `vault_hc_ping_backups`, `vault_hc_ping_pve`, `vault_hc_ping_media_heartbeat`,
  `vault_hc_ping_scrub` (UUIDs only, not full URLs). Real values into vault.yml via
  `make vault-edit`. tests/validate_secrets.py may require example parity — follow its rules.
- `roles/service_vm/templates/healthchecks.env.j2` → /etc/homelab-healthchecks.env (0600 root):
  `HC_BASE=https://hc-ping.com`, `HC_PING_VERIFY=...uuid`, etc. Rendered with `no_log: true`
  (validate_secret_tasks.py enforces this because the template references vault_*).
  Render per-host only the UUIDs that host uses (template guards) — or all; simpler: all,
  EXCEPT never render it on svc-download (egress-fenced, no consumer).
- Shared ping helper inline in each consumer (keep minimal):
  `curl -fsS -m 10 --retry 3 "${HC_BASE}/${HC_PING_X}${suffix}"` where suffix ""/"/fail".
- `roles/svc_infra/templates/backups-fresh.sh.j2` + `roles/svc_infra/files/homelab-backups-fresh.{service,timer}`
  (04:30 daily, after all backups): checks newest file mtime <26h in
  /srv/backups/{svc-media,svc-download,svc-infra}/... (confirm exact dest dirs from the three
  backup-*.sh.j2 templates — media uses /srv/backups/{{ inventory_hostname }}/…). Fresh →
  ping HC_PING_BACKUPS; stale → ntfy alert (alerts topic, listing stale dirs) + HC /fail +
  exit 1 (OnFailure also fires — acceptable duplicate, or exit 1 without its own ntfy and
  let OnFailure carry it; choose: script does HC /fail + exit 1, OnFailure does the ntfy).
  This covers svc-download's backup without opening its egress. NOTE verify already checks
  NFS mounts; this runs on svc-infra which mounts the backups dataset — confirm mount path
  in roles/svc_infra tasks/nfs (service_vm/tasks/nfs.yml).
- `roles/svc_media/files/homelab-heartbeat.{service,timer}` + tiny
  `roles/svc_media/templates/heartbeat.sh.j2`: daily ping HC_PING_MEDIA_HEARTBEAT.
- Backup scripts: media/infra backup-*.sh.j2 get success-ping? No — HC_PING_BACKUPS is
  centralized in backups-fresh; per-backup pings would burn 3 checks. Their ERR traps
  switch to alerts topic only (branch 1 change). Keep as-is otherwise.

Verification: run backups-fresh manually → hc dashboard shows ping; `touch -d '2 days ago'`
is a mutation — instead temporarily set freshness threshold to 1s via -e and run → /fail +
phone alert + healthchecks shows "fail" event; hc sends its own notification (configure
hc email+push to phone once, manually, documented in docs/operations.md).

---

## Branch 3: `feat/pve-mon` — bring PVE under management

Inventory: add a real SSH host (cleaner than delegating a whole role via pve_ssh_host):
```yaml
# inventory/hosts.yml
pve_mon_hosts:
  hosts:
    thurgadin-ssh:
      ansible_host: 192.168.1.10
      ansible_user: root
```
(preflight.yml asserts unique ansible_host — 192.168.1.10 appears only as pve_api_host
today, so OK; confirm preflight scope.) `thurgadin` stays API/local; no change to pve_vm.

New role `roles/pve_mon/`:
- files/: `pve-diskguard.sh` (moved from contrib/bin — leave a pointer README in contrib or
  delete; update any docs links → tests/validate_links.py), `homelab-diskguard.{service,timer}`
  (from contrib/systemd), `notify-failure@.service` (reuse service_vm file via
  role file copy or symlinkless duplicate — simplest: tasks copy from
  roles/service_vm/files/ using `src: ../../service_vm/files/notify-failure@.service`? Ansible
  role file lookup won't do that cleanly; duplicate the tiny unit in pve_mon/files and let
  validate_onfailure assert both copies identical),
  `homelab-scrub.{service,timer}` (monthly, `zpool scrub -w` each pool from
  `zpool list -H -o name`; success → HC scrub ping; OnFailure drop-in),
  `homelab-pve-health.{service,timer}` (daily: `zpool status -x` == "all pools are healthy"
  && systemctl --failed empty → HC pve ping, else exit 1 → OnFailure).
- templates/: `notify.env.j2`? Reuse service_vm's template via
  `template: src: roles/service_vm/templates/notify.env.j2` is not idiomatic — pve_mon
  gets its own minimal `homelab-notify.env` render (or import the service_vm template with
  a relative path `../../service_vm/templates/notify.env.j2`, which DOES work for
  template src). Prefer the relative-src reuse; one source of truth.
  Also `smartd-ntfy.sh.j2` and `zed-ntfy.sh.j2`.
- tasks/main.yml:
  - packages: smartmontools, zfs-zed present (Debian PVE: apt, they're preinstalled mostly;
    state: present).
  - /etc/homelab-notify.env + /etc/homelab-diskguard.env (THRESH + sources notify env or
    duplicates vars — keep the existing diskguard env contract: NTFY_URL/NTFY_TOPIC/THRESH;
    render from same vars, add NTFY_ALERT_TOPIC).
  - diskguard script+units enabled (15min timer), OnFailure drop-in.
  - zed: lineinfile/copy `/etc/zfs/zed.d/zed.rc`: `ZED_EMAIL_ADDR="root"`,
    `ZED_EMAIL_PROG="/usr/local/sbin/zed-ntfy.sh"`, `ZED_NOTIFY_INTERVAL_SECS=3600`,
    `ZED_NOTIFY_VERBOSE=0`; zed-ntfy.sh takes (addr, subject via ZED env/args) — zed calls
    `$ZED_EMAIL_PROG -s subject addr < body`; script posts subject+body to alerts topic.
    Restart zed on change (handler).
  - smartd: manage /etc/smartd.conf single DEVICESCAN line:
    `DEVICESCAN -a -o on -S on -n standby,q -m root -M exec /usr/local/sbin/smartd-ntfy.sh -M daily`
    smartd-ntfy.sh reads SMARTD_* env vars (SMARTD_MESSAGE/DEVICE/FAILTYPE) → urgent ntfy.
    Enable smartd, restart on change. (`-M test` once manually for verification.)
  - scrub + pve-health units enabled; failedunits watcher (system scope) installed here too
    (reuse via relative template src from roles/mon).
  - healthchecks env (HC_PING_PVE, HC_PING_SCRUB) 0600 no_log.
- tasks/verify.yml: timers active, scripts present, zed.rc + smartd.conf content asserts,
  `zpool status -x` healthy.

site.yml: new play `hosts: pve_mon_hosts, gather_facts: true (minimal), roles: pve_mon`
with the standard rescue→notify block, placed after provisioning. Makefile: add
`pve` target (`--limit pve_mon_hosts` tags). verify.yml: add a `hosts: pve_mon_hosts` play
calling pve_mon verify — BUT branch 4's svc-infra runner would then need root@PVE SSH.
Decision: keep PVE OUT of nightly verify (the runner limits to service_vms + localhost via
`--limit 'service_vms:localhost'` in the wrapper); PVE liveness is covered daily by
homelab-pve-health HC ping. Workstation `make verify` runs everything. The localhost
summary-play asserts in verify.yml must not require pve facts (they don't today; keep the
new pve verify play skippable via the limit).

Verification: deploy pve play; `systemd-run --unit=testfail -p OnFailure=... /bin/false` on
PVE → phone; `smartctl` -M test alert; run homelab-pve-health → hc ping visible; start a
scrub on the smallest pool.

Risks/open questions:
- root@PVE SSH from workstation must already work (pve_vm delegates to pve_ssh_host — yes).
- zed's email-prog calling convention differs across zfs versions (PVE 8: 2.2.x,
  `$ZED_EMAIL_PROG $ZED_EMAIL_OPTS` with subject in ZED_EMAIL_OPTS) — verify on host during
  implementation; keep the hook tolerant (post stdin + all args).

---

## Branch 4: `feat/verify-runner` — nightly `make verify` on svc-infra

Strategy: push-from-workstation (no GitHub deploy key, no repo credentials on the VM;
reliability = the code that ran the last successful deploy is what verifies nightly).

- Dedicated user `svcops` on svc-infra (roles/svc_infra/tasks/verify-runner.yml):
  system user, home /opt/homelab-iac parent? Give home /home/svcops; checkout at
  /opt/homelab-iac owned svcops:svcops 0750.
- Repo sync at deploy time (svc_infra role, tags [verifyrunner]):
  `git archive HEAD` on localhost (delegate_to: localhost, become: false) → fetch to
  /opt/homelab-iac via unarchive (copy tarball + unarchive remote). Excludes untracked
  files by construction; .vault_pass and .venv live outside the archive and survive
  (unarchive without `--delete`; drift acceptable — archive is authoritative for tracked
  paths). Record HEAD sha into /opt/homelab-iac/.deployed-rev for the wrapper to log.
  NOTE: this makes deploys require a clean-ish committed tree for the runner to be current
  — matches the CLAUDE.md "final deploy from clean tree" rule; document it.
- venv: `python3 -m venv /opt/homelab-iac/.venv` + pip -r requirements.txt + galaxy
  collections into /opt/homelab-iac/collections (ansible.cfg likely sets collections_path
  relative — verify ansible.cfg; svc-infra has unrestricted egress so pip/galaxy fine;
  make idempotent via creates= + a stamp file keyed on requirements.txt checksum).
- SSH: generate ed25519 keypair for svcops on svc-infra (community.crypto or command
  creates=). Its PUBLIC key is fetched as a fact and installed into straderb's
  authorized_keys on all service_vms by roles/service_vm (var
  `verify_runner_pubkey`, authorized_key module, restricted `from="192.168.1.32"`).
  known_hosts for svcops pre-populated from hostvars (ssh-keyscan at deploy, or better:
  `StrictHostKeyChecking=accept-new` in svcops ~/.ssh/config — simpler, acceptable on LAN).
  Sudo: ansible_become uses straderb's sudo — confirm straderb is NOPASSWD (cloud-init
  standard); if not, add sudoers drop-in (open question; check
  roles/pve_vm cloud-init user-data).
- Vault password: MANUAL one-time step (documented in docs/operations.md runbook):
  `install -o svcops -g svcops -m 0600 /dev/stdin /opt/homelab-iac/.vault_pass` over SSH.
  Never in git/Ansible. verify.yml (svc-infra host verify) asserts the file exists with
  mode 0600 so a forgotten step fails loudly BEFORE departure.
- Wrapper `roles/svc_infra/templates/homelab-verify-run.sh.j2` → /opt/homelab-iac/bin? →
  /usr/local/sbin/homelab-verify-run.sh: sources /etc/homelab-healthchecks.env; runs
  `/opt/homelab-iac/.venv/bin/ansible-playbook verify.yml --vault-password-file .vault_pass
  --limit 'service_vms:localhost' -e notify_on_success=false`; exit 0 → HC verify ping;
  nonzero → HC /fail + exit 1 (OnFailure notifier fires; playbook rescue already ntfy'd —
  and if ntfy is dead, healthchecks escalates externally. This answers "should verify
  failures also /fail": YES).
- Units: update `contrib/systemd/homelab-verify@.service` ExecStart to the wrapper?
  tests/validate_systemd_units.py pins current ExecStart — instead PROMOTE the units into
  `roles/svc_infra/files/homelab-verify@.{service,timer}` with
  `ExecStart=/usr/local/sbin/homelab-verify-run.sh` (+EnvironmentFile healthchecks env),
  update validate_systemd_units.py paths/fragments accordingly, and delete the contrib
  copies (update docs links). Enable `homelab-verify@svcops.timer`.
  Env vars in verify run: ANSIBLE_HOME etc. are Makefile-only — wrapper sets
  `ANSIBLE_LOCAL_TEMP/ANSIBLE_HOME/XDG_CACHE_HOME` under /opt/homelab-iac/.ansible and
  `ANSIBLE_CONFIG=/opt/homelab-iac/ansible.cfg`; NO PROXMOX_CA_PATH needed (verified:
  verify.yml has no pve play today; after branch 3, the --limit excludes it).
  Also verify.yml's `delegate_to: localhost` ntfy push runs on svc-infra — fine (LAN ntfy
  reachable).
- verify.yml/svc_infra verify additions: timer enabled+active assert, .vault_pass mode,
  .deployed-rev matches workstation HEAD? (skip — informational only).

Functional verification: `systemctl start homelab-verify@svcops.service`, watch journal,
confirm "verify OK"… wait, notify_on_success=false — confirm via HC dashboard ping +
`systemctl status`. Then break something benign read-only? Simplest: run wrapper with a
bogus --limit via manual invocation to force failure → HC /fail + phone alert. Also pause
the HC check manually and confirm healthchecks emails after grace (before departure).

---

## Branch 5: `chore/departure-runbook` (small, may fold into 4)
- docs/operations.md: "6-month unattended" runbook — hc account config (email+push
  targets), phone ntfy topic setup (mute deploy topic), one-time vault-pass install,
  smartd -M test procedure, what each alert means + first-response actions, list of the 5
  HC checks with expected cadence, recovery steps (Tailscale in, cockpit URLs).
- README/CLAUDE.md touch-ups: new make target `pve`, validate gate list mention.

## Sequencing & workflow (per CLAUDE.md)
Order 1→2→3→4→5; each: branch → make validate → iterate deploy → functional test above →
clean tree → final deploy changed=0 → make verify → merge/push → delete branch. Branch 2
requires 1 (topic split + OnFailure list entries). Branch 4 requires 2 (HC env) and is last
because it freezes verify semantics.

## Risks / open questions
1. straderb passwordless sudo on VMs (needed for svcops nightly become) — verify in
   cloud-init (roles/pve_vm); add sudoers drop-in if absent.
2. zed ZED_EMAIL_PROG arg convention on the PVE zfs version — make hook arg-agnostic.
3. preflight.yml uniqueness assert vs. 192.168.1.10 appearing as a new ansible_host —
   check preflight scope; adjust assert or host entry if it collides with pve_api_host.
4. validate_secrets.py / scan_history_secrets expectations for the new vault_hc_* vars —
   follow all_vault.yml.example placeholder conventions.
5. Free-tier hc grace tuning: verify timer has RandomizedDelaySec=900 + 10-min runtime —
   set hc schedule "daily" with 6h grace to avoid flap.
6. notify-failure@ storm risk: a flapping timer (15-min diskalert) failing every run would
   alert every 15 min — acceptable (it IS actionable), but note StartLimit defaults; the
   watcher's 6h dedupe is the backstop for sustained failures.

