# Cold-start resilience

Findings from a full-estate cold start executed 2026-08-04, and the fixes for
them. All six VMs on thurgadin were shut down simultaneously, confirmed down,
then started simultaneously.

The test is worth repeating after any change to boot ordering, NFS options or
the container catalogs. `scratchpad/coldstart/healthcheck.sh` in the session
that produced this file was a before/after diff; the reusable form is
`make verify` plus the new `cold-start` gate described in §5.

## What actually happened

| VM | Shutdown | Start | Result |
|----|----------|-------|--------|
| convoker (TrueNAS, 100) | < 3 min | ~90 s to serve NFS | ✅ |
| kunark (111) | < 3 min | — | ✅ |
| svc-download (131) | < 3 min | 8 s to sshd | ❌ 5 of 9 `dl-*` dead |
| svc-infra (132) | **6 m 20 s** | 8 s to sshd | ✅ all 28 services |
| svc-media (130) | **7 m 10 s** | 8 s to sshd | ✅ all 11 services |
| w10-edgar (108) | **never** (forced) | — | ⚠️ out of scope |

Two things went right and are worth recording, because they are the reason
this was a test and not an outage:

- **The leak canary caught it.** It publishes `download stack degraded —
  expected container sonarr does not exist` at priority 4, and its own
  `OnFailure=` published a second alert at priority 5. It checks an *explicit
  expected list* rather than `podman ps` output, which is exactly the v5 fix
  described in its own header comment. That design is what made a silent
  failure loud.
- **Postgres survived being SIGKILLed.** `immich-db` logged `database system
  was not properly shut down; automatic recovery in progress`, replayed its
  WAL and came up clean. So did netbox-db and nextcloud-db. This is crash
  safety doing its job, not evidence that hard-killing databases is fine.

## F1 (critical, root cause) — the NFS server has no boot order

`convoker` (VM 100) is TrueNAS and serves `/srv/media` and `/srv/backups` to
svc-media, svc-download and svc-infra. Its config is:

    onboot: 1
    (no startup: line)

Proxmox starts VMs with no `startup` order **after** every ordered VM, and
shuts them down **first** — shutdown is the reverse of start order. So the
storage server boots last and dies first, which is precisely backwards.

The comment at `inventory/host_vars/svc-media.yml` reading
`# TrueNAS is order=1 (set once, out of band)` is **false**, and nothing ever
checked it. That is the whole failure in one line: a claim in a comment
standing in for a verified property.

One root cause, two expensive symptoms:

**Boot.** The three Rocky VMs reach sshd about 8 seconds after power-on.
TrueNAS needs roughly another 90 to serve NFS. Measured on svc-download:

    09:55:29  srv-media.mount  Mounting...
    09:55:59  srv-media.mount  Mounting timed out (30 s) — FAILED
    09:55:59  dl-sonarr        Dependency failed → inactive/dead
    09:56:25  srv-media.mount  Mounted.

The mount succeeded 26 seconds after the timeout. Nothing retried the units.

**Shutdown.** TrueNAS stops first, leaving clients holding `hard` NFS mounts.
Processes wedge in uninterruptible I/O, which `SIGKILL` cannot clear:

    nfs: server 192.168.1.20 not responding, timed out
    user@10001.service: State 'stop-sigterm' timed out. Killing.
    user@10001.service: Killing process 1799853 (postgres) with signal SIGKILL
    user@10001.service: Processes still around after final SIGKILL. Entering failed mode.

That is what stretched shutdown past PVE's 180 s `qm shutdown` timeout, and
what hard-killed nine postgres processes across Immich, NetBox and Nextcloud.

**Fix.** Give TrueNAS `order=1` and a shutdown allowance. Because shutdown is
reverse order, one setting fixes both directions:

    qm set 100 -startup order=1,up=120,down=180

`up=120` holds the ordered start groups until TrueNAS has had two minutes to
begin serving. The service VMs keep order 2/3/4 and gain `down=` values.

Because a comment already lied about this once, §5 adds a gate that asserts
it against the live PVE API rather than trusting the file.

## F2 (critical) — a transient mount failure permanently abandons a unit

`download.container.j2` emits, for every catalog entry with `media_mount`:

    RequiresMountsFor=/srv/media

which the generator expands to a hard `Requires=srv-media.mount`. When the
mount failed at second 30, systemd marked the dependency failed and left the
unit `inactive/dead`. `Restart=on-failure` does not help: the unit never
started, so there is no failure to restart from.

**Which units die is a race, not a property.** Seven `dl-*` units require the
mount. Two survived only because their start jobs were evaluated after the
successful remount:

    dl-sabnzbd    started 09:56:25  (mount succeeded 09:56:25)  survived
    dl-shelfmark  started 09:56:49                              survived
    dl-sonarr / radarr / bazarr / lazylibrarian / jdownloader
                  evaluated 09:55:59 (mount failed)             died

A different boot produces a different set. That is worse than a deterministic
failure, because it cannot be reasoned about from the previous boot.

### The obvious fix does not work, and this is the useful part

The intended fix was to drop `RequiresMountsFor=` and order against the
**automount** unit instead, letting the existing `ExecStartPre` guard —

    ExecStartPre=/bin/sh -c 'stat /srv/media/. >/dev/null && findmnt -t nfs4 -M /srv/media >/dev/null'

— do the gating with a retry budget. That guard is the right positive control;
the problem is only that the hard dependency kills the unit before it runs.

**It does not work.** Removing the directive from the template changes
nothing, verified on the host rather than assumed:

    $ systemctl show dl-sonarr.service -p Requires
    Requires=... vpn-netns.service srv-media.mount ...

Quadlet *derives* `RequiresMountsFor=` from the host path in `Volume=`, so the
generated unit hard-requires `srv-media.mount` regardless. A drop-in cannot
undo it either — resetting `Requires=` was tried and the mount dependency came
back, because `RequiresMountsFor` is applied as an implicit dependency at
unit-load time, after drop-ins are merged.

The only way to remove it would be to stop declaring `/srv/media` as a volume,
which is the entire point of these containers.

**So the real defence is F1 and the mount timeout, not this.** Boot ordering
(TrueNAS `order=1`, now asserted) plus a 120 s mount timeout mean the mount is
never attempted against a NAS that is still starting. That is a prevention
rather than a recovery, which is weaker in principle — if the ordering is ever
lost, the race returns — and that is exactly why the ordering is now a gate
instead of a comment.

**What was still worth changing** is the other failure path: the mount unit
succeeds but the export is not really usable, so `ExecStartPre` fails. That
path *does* restart, and the defaults were spent instantly — `Restart=on-failure`
with no `RestartSec` retries every 100 ms, five times, inside half a second.
So the budget is now explicit:

    StartLimitIntervalSec=600
    StartLimitBurst=60

Sixty attempts at 5 s covers ten minutes and still ends in `failed`, which —
unlike `inactive/dead` — is visible to `systemctl --failed` and does fire
`OnFailure=`. The same budget was added to the four svc-media user units and
immich-server, which had the identical 100 ms/5-attempt defaults behind the
same NFS guard and survived this run only on timing luck.

## F3 (high) — dependency-failed units are invisible to `systemctl --failed`

A unit abandoned for a failed dependency is `inactive/dead`, not `failed`. So:

- `systemctl --failed` reports clean
- `OnFailure=` never fires
- `homelab-failedunits.service` sees nothing

On svc-download the leak canary covers this within 15 minutes by design — it
enumerates an expected list rather than reading `podman ps`.

**`make verify` already covers all three hosts**, and this was checked rather
than assumed before adding anything:

- `roles/svc_download/tasks/verify.yml` — "Verify every catalog container is in
  the VPN namespace" runs `podman inspect` per catalog entry under `set -e`, so
  a missing container fails the task.
- `roles/svc_media/tasks/verify.yml` — "Verify all rootless media services are
  active" asserts `is-active` per catalog entry.
- `roles/svc_infra/tasks/verify.yml` — the same, over `infra_service_names`.

And `homelab-verify@.service` runs that nightly on svc-infra.

**So no new gate was added here**, because one already existed and adding a
second would be noise. What is worth recording is the shape of the blind spot,
since it will recur elsewhere: `systemctl --failed`, and therefore
`homelab-failedunits.service` and every `OnFailure=` drop-in, cannot see a unit
that was abandoned for a failed dependency. Anything relying on those alone is
blind to this class of failure. The three checks above work because they
enumerate what *should* be running instead of asking what *is* broken.

The residual exposure is the window between nightly verifies on svc-media and
svc-infra — up to 24 hours, against 15 minutes on svc-download. Accepted:
the F2 fix means a unit now retries through the NAS boot and ends in `failed`
if it genuinely cannot start, which the existing alerting *does* see.

## F4 (medium) — stop timeouts are shorter than the work

Two layers both defaulted too low:

- podman's 10 s container stop timeout SIGKILLed `calibre-web-automated` and
  `immich-server`.
- `user@10001.service` inherits systemd's 90 s `TimeoutStopSec`, which is not
  enough to stop ~28 containers, so it SIGKILLed the whole session including
  postgres.

**Fix.** A drop-in raising `TimeoutStopSec` for the rootless user manager, so
the containers get a chance to stop cleanly before the session is killed. This
matters most once F1 is fixed, because the NFS hang will no longer be the
dominant cost.

## F5 (low) — TrueNAS VM has no EFI disk

`qm start 100` warns:

    WARN: no efidisk configured! Using temporary efivars disk.

EFI variables are not persisted across boots. Not the cause of anything seen
here, and adding an efidisk to a running TrueNAS VM is an out-of-band change
with its own risk. **Recorded, not fixed.**

## F6 (low, out of scope) — w10-edgar has no guest agent

VM 108 has `agent: 1` configured but the QEMU guest agent is not running in
the guest, so `qm shutdown` fell back to ACPI, which Windows ignored. It never
shut down and had to be `qm stop`-ed. Not managed by this repo.
**Recorded, not fixed.**

## §5 — the gates

Only one new gate was needed; the unit-liveness one already existed (see F3).

**TrueNAS boot order**, asserted in `roles/pve_mon/tasks/verify.yml` by reading
`qm config 100` on the hypervisor over SSH. It checks two conditions
separately, so "could not look" cannot pass as "order is satisfied":

    - pve_truenas_config.stdout | length > 0
    - pve_truenas_config.stdout is search('(?m)^startup:.*\border=1\b')

It reads the hypervisor rather than the repo deliberately. The repo does not
own this VM, and the previous mechanism — a comment asserting `order=1` — was
false for months with nothing to catch it. A restored or rebuilt PVE loses the
setting silently, which is exactly the case a gate exists for.

**The stop timeout** (F4) is asserted the same way, in
`roles/service_vm/tasks/shutdown.yml`: it reads back
`systemctl show user@10001.service -p TimeoutStopUSec` rather than trusting
that the drop-in file exists. A drop-in with a mistyped section header writes
successfully and is silently ignored — the file proves nothing about what
systemd will do at shutdown. Same failure shape as the boot-order comment.

Per the standing rule in CLAUDE.md: a gate that cannot fail is not a gate, and
"none found" must be distinguishable from "could not look".

## What was not fixed

- **F5, TrueNAS EFI disk.** Adding one to a running TrueNAS VM is an
  out-of-band change with its own risk, and it caused nothing observed here.
- **F6, w10-edgar's guest agent.** Not managed by this repo.
- **The test itself is not automated.** Re-running it means real downtime, so
  it stays a deliberate act. The findings above are what the gates now defend.
