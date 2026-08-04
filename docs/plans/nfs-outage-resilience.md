# NFS server outage — what a ten-minute TrueNAS stop actually does

Companion to `cold-start-resilience.md`. That document covers the estate
starting and stopping *together*. This one covers a different and, it turns
out, nastier case: **the storage server disappears while every client stays
up.**

Run on 2026-08-04. `convoker` (VM 100, TrueNAS, 192.168.1.20) was shut down
cleanly at 15:24:02 and started again at 15:37:31 — a 13m29s outage with
svc-media, svc-download and svc-infra running throughout.

## The headline

The estate survived it completely and **told nobody anything was wrong.**

Recovery was automatic and total: the post-outage health check was
byte-identical to the baseline taken before it. No unit failed, no container
died, no `dl-*` unit dropped, nothing needed a manual kick. That is `hard`
NFS mounts working exactly as designed, and it is the good news.

The bad news is everything in between. For roughly eight of those thirteen
minutes all three service VMs were **unadministrable and unmonitored**, and
every alerting path this repo owns was disarmed by the very event it exists
to report.

## F1 — PID 1 stops answering, so the host cannot be administered

Symptom, observed directly: all three VMs answered ICMP and accepted TCP on
port 22, but **SSH login never completed**. `systemctl`, `sudo` and
`machinectl` all hung too.

```
192.168.1.30: SSH LOGIN HUNG (>25s)     192.168.1.30:22 open
192.168.1.31: SSH LOGIN HUNG (>25s)     192.168.1.31:22 open
192.168.1.32: SSH LOGIN HUNG (>25s)     192.168.1.32:22 open
```

The mechanism is in svc-media's journal:

```
15:31:37 systemd-logind: Failed to start user service 'user-runtime-dir@0.service': Connection timed out
15:32:02 systemd-logind: Failed to start user service 'user@0.service': Connection timed out
15:32:02 sudo:  pam_systemd(sudo:session): Failed to create session: Connection timed out
```

Those are D-Bus calls *into PID 1* timing out. The system manager was blocked,
so it could not create sessions, start units, or answer queries. Any login has
to go through `pam_systemd`, so every login hung.

Early in the outage — before the manager stopped answering entirely — a probe
caught the processes stuck in uninterruptible sleep:

```
D-state:  1 systemd   1 python3   1 node_exporter
```

Note that even diagnosis is constrained: `ps -eo args` hangs during an outage,
because reading `/proc/PID/cmdline` needs a lock the wedged task holds.
`comm=` reads the task struct and stays safe. Probes written for this scenario
must never touch `/srv/media` or `/srv/backups` — a `hard` mount blocks in
D-state, which `SIGKILL` cannot clear, so the probe becomes another casualty.

**This is not fixable by configuration, and pretending otherwise would be
worse than saying so.** `hard` is the correct mount option: `soft` trades this
hang for silently corrupted writes, which is a far worse failure for a media
and backup store. The answer is to *detect* the condition from somewhere that
cannot be affected by it — see the fix below.

## F2 — the entire alerting architecture is disarmed by this exact event

This is the finding that matters, and it is the repo's recurring defect in a
new costume: **a check that reports nothing because it never ran.**

`homelab-diskalert.service` exists to notice storage trouble. Its unit file
already anticipates a hung NAS, with a comment that is worth quoting because
it is correct as far as it goes:

> systemd disables the start timeout for `Type=oneshot` by default, and this
> script calls `df` across NFS mounts. A hung NAS blocks `df` in
> uninterruptible sleep, the unit sits in `activating` forever […] The timeout
> converts that silent hang into a failure, which is what alerts.

`TimeoutStartSec=300` is set for precisely that reason. **It never engaged.**

The timer runs every 15 minutes. On all three hosts the run before the outage
was at 15:15, so the next was due at ~15:30 — squarely inside the outage.
Here is when it actually ran:

| host | last run before | next run due | next run actual |
|---|---|---|---|
| svc-media | 15:15:42 | ~15:30:42 | **15:38:31** |
| svc-infra | 15:18:12 | ~15:33:12 | **15:38:30** |
| svc-download | 15:19:21 | ~15:34:21 | **15:38:31** |

15:38:31 is the same second the kernel logged `nfs: server 192.168.1.20 OK`.

The unit was never *started*, so there was no start to time out. PID 1 could
not run it. The safety net was one level too shallow: it protects against the
script hanging, not against the manager being unable to launch the script.

The consequence generalises to everything: **every watcher in this repo is a
systemd timer on the affected host.** Disk alerts, failed-unit sweeps, the
leak canary, the nightly scan — during a storage outage none of them can fire,
and no `OnFailure=` can fire either, because firing an `OnFailure` also
requires PID 1 to start a unit. `notify-failure` logged nothing on any host.

Meanwhile the estate looked perfectly healthy from outside. Thirteen minutes
in, every HTTP endpoint returned its exact baseline status code:

```
home -> 200    jellyfin -> 302    auth -> 200    chat -> 200    vaultwarden -> 200
```

Jellyfin answers 302 whether or not its library exists — the redirect is
served before anything touches `/media`. ntfy was healthy (`/v1/health` = 200)
with **zero messages**. This is the CLAUDE.md rule about smoke tests proving
only that a process started, demonstrated at estate scale.

## F3 — node_exporter wedges, so metrics go blind too

`node_exporter`'s filesystem collector stats every mount point on each scrape.
The unit excluded pseudo-filesystems *by mount point*
(`--collector.filesystem.mount-points-exclude`) but nothing excluded NFS *by
type*, so every scrape walked `/srv/media` and `/srv/backups` and blocked.
That is the `node_exporter` in the D-state list above.

The cost is not just missing NFS capacity numbers: the whole scrape hangs, so
Prometheus loses CPU, memory, network and local-disk metrics for that host as
well. The observability layer fails at the same moment, and in the same
direction, as the alerting layer.

## F4 — recovery is automatic and complete (the good news, stated plainly)

TrueNAS started at 15:37:31, answered ICMP by 15:38:40, and all three hosts
were administrable again within ~65 seconds of that. The kernel logged
`nfs: server 192.168.1.20 OK` and in-flight I/O resumed.

The post-outage health check diffed **byte-identical** against the baseline:
same 9 endpoints, same container inventory on both rootless hosts, same 9/9
`dl-*` units, zero failed units, both NFS mounts present everywhere, leak
canary untripped, VPN netns intact.

No intervention was required, and none of the retry budgets added during the
cold-start work were even consumed — nothing restarted, because nothing
failed. Processes blocked and then continued, which is what `hard` promises.

## The fix

Two changes, one principle: **the detector must not live on a machine that
the failure can silence, and it must never touch the thing that hangs.**

### 1. `homelab-nfsguard` on the hypervisor

The PVE host is the correct home for this and is the only place in the estate
that qualifies:

- it mounts **nothing** from TrueNAS (`findmnt -t nfs,nfs4` is empty;
  `pvesm status` lists only `local`, `local-zfs`, `nvme0pool`), so it cannot
  be wedged by the outage;
- it is by definition up whenever the guests are, since it runs them;
- it already has `/etc/homelab-notify.env`, the `notify-failure@` alerter and
  an established timer pattern (`homelab-diskguard`).

That it stayed healthy is not an assumption — it was measured.
`homelab-diskguard.timer` fired at **15:32:16**, in the middle of the outage,
and completed normally, while all three guests' timers were frozen.

The guard probes **the server, never a mount**: `qm status` for the storage
VM, an ICMP reachability check, and a bounded TCP connect to port 2049. None
of those can block in D-state, which is what makes the guard safe to run on a
5-minute timer.

It reports a **tri-state verdict**, following the precedent set by the
credential canary in commit `057e1e4`: `ok`, `down`, or `inconclusive`.
"Could not look" — `qm` missing, VMID unreadable — is its own state and never
renders as an all-clear. It alerts on the transition into trouble and again
on recovery, with the same hash-and-realert dedupe `pve-diskguard.sh` uses so
a long outage cannot become a push every five minutes.

**It has a real positive control**, which the credential canary explicitly
lacks. `--check` runs the full probe path and prints the verdict without
alerting or writing state, and `make verify` requires it to print `ok`. A
typo'd port, a wrong VMID or a broken `qm` invocation makes verification fail
rather than making the guard quietly stop noticing outages. Because TrueNAS
is normally up, this is a genuine "something that must be true if it ran".

Detection latency: at most 5 minutes, against the current infinity.

### 2. node_exporter stops walking NFS

Extend the collector's default `fs-types-exclude` regex with `nfs` and `nfs4`.
node_exporter's own default list is reproduced in full and the two types
appended, rather than replaced, so the pseudo-filesystem exclusions are not
silently dropped.

This trades NFS capacity metrics for keeping every other metric alive during
an outage. That trade is only acceptable because NFS capacity is *already*
covered elsewhere, by `disk-alert.sh` with its own `disk_alert_nfs_threshold:
90` — a bounded one-shot with `TimeoutStartSec` and an `OnFailure` path,
which is a very different risk profile from a process scraped every 15
seconds. `disk-alert.sh` is deliberately left alone.

## What was NOT fixed, and why

- **The wedge itself.** `hard` NFS mounts block uninterruptibly when the
  server vanishes; that is the contract, and `soft` would trade a recoverable
  hang for unrecoverable write corruption. Not changed.
- **Watchers on the guests remain unable to fire during an outage.** Moving
  them all to the hypervisor would be a much larger change and would weaken
  them for every failure mode *except* this one. The hypervisor guard covers
  the gap; the guest timers keep their existing jobs.
- **Uptime Kuma has no monitor for the NAS.** It is configured through its own
  UI rather than this repo, so adding one is a manual step outside IaC. The
  hypervisor guard makes it unnecessary rather than merely postponed.
- **TrueNAS itself is still unmanaged by this repo** and has no efidisk
  (`WARN: no efidisk configured! Using temporary efivars disk`), carried over
  from `cold-start-resilience.md`.

## Verification

Deployed 2026-08-04. All four targets report `changed=0` on a re-run from a
clean tree (`make pve` / `media` / `dl` / `infra`), and `make verify` is green
on all five hosts.

**The probe, in all four directions**, exercised against the live hypervisor
*before* the guard was deployed — a guard only ever observed in the healthy
state is a guard nobody can tell is broken:

| scenario | verdict | bounded at |
|---|---|---|
| storage healthy | `ok` | 0.6s |
| port closed, host answers ICMP | `down` | 0.6s |
| bogus VMID 999 | **`inconclusive`** | 0.5s |
| unroutable IP | `down` | 7.6s |

The third row is the one worth keeping: a VMID the guard cannot read comes
back `inconclusive`, not `down` and not `ok`. Worst case is 7.6s against
`TimeoutStartSec=120`.

**The alert path, read back out of ntfy** rather than inferred from an exit
code — the rule CLAUDE.md sets for alerting. The deployed script was run with
two constants changed (probe port, to force a verdict without touching
TrueNAS; state dir, so live dedupe state stayed clean) and both transitions
were then read back off the topic:

```
[16:36:56] priority=5 tags=rotating_light,floppy_disk
  TITLE: STORAGE DOWN: NFS on thurgadin
  storage VM 100 is running and 192.168.1.20 answers ICMP, but nothing is
  listening on 2050. NFS is not being served; clients will wedge.

[16:36:59] priority=3 tags=floppy_disk
  TITLE: Storage recovered on thurgadin
  NFS on 192.168.1.20:2049 is answering again.
```

The live state file was confirmed untouched by the exercise.

**node_exporter**, measured from each host's own loopback:

| host | filesystem series | `/srv` NFS series |
|---|---|---|
| svc-media | 3 (`/`, `/boot`, `/boot/efi`) | **0** |
| svc-download | 16 (incl. container overlays) | **0** |
| svc-infra | 3 | **0** |

A caution earned the hard way during this very check: measuring `:9100` from
the *hypervisor* returns zero series for everything, because firewalld admits
only svc-infra. That reads exactly like a successful exclusion and is in fact
"could not look". Scrape node_exporter from the host itself or from svc-infra,
never from a third machine.

## Reproducing this test

```bash
ssh root@192.168.1.10 'qm shutdown 100 --timeout 180'
# ... wait ...
ssh root@192.168.1.10 'qm start 100'
```

Do **not** probe by touching `/srv/media` from a client: the probe wedges in
D-state, cannot be killed, and adds to the pile. Read `/proc`, systemd state
and the journals instead, and use `ps -eo stat=,pid=,comm=` rather than
`args=`.
