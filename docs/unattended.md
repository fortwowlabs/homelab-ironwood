# Leaving it alone for six months

This estate is designed to run without an operator. That is not the same as
running without faults — it means every fault that needs a human produces a
notification, and every notification that fails to arrive is itself detected.

This page is the departure checklist and the field guide for what arrives on
your phone while you are away.

## The governing idea

Alerting that only pushes messages has a blind spot shaped exactly like the
disaster you care about. ntfy runs on svc-media; if svc-media, the network, or
the power dies, the alerts stop — and stopped alerts look identical to
"everything is fine".

So there are two directions:

```text
inward   things break  -> ntfy  -> phone            (detail, immediate)
outward  things work   -> healthchecks.io           (absence is the alarm)
```

Nothing else in the design matters as much as that second row. If you read one
section here, read [The dead-man's switch](#the-dead-mans-switch).

## Before you leave

1. **Claim every admin account.** Any service still showing a first-run setup
   screen will be claimed by whoever reaches it first. See
   [First login](first-login-walkthrough.md). Open WebUI at
   `chat.fortwow.dev` is the usual straggler.
2. **Create the healthchecks.io checks** and paste their UUIDs into the vault —
   the [full procedure](#the-dead-mans-switch) is below. Until this is done
   there is no external safety net, and nothing will tell you that.
3. **Install the runner's vault password** on svc-infra if this estate has
   been rebuilt — see "The nightly verification runner" in
   [Operations](operations.md). `make verify` fails loudly if you forget.
4. **Subscribe the phone to both ntfy topics**, mute `homelab-deploy`, leave
   `homelab-alerts` loud and allowed to bypass Do Not Disturb.
5. **Run `make deploy` from a clean tree and confirm `changed=0`,** then
   `make verify`. That pair is the whole guarantee: what is running equals
   what is committed, and it works.
6. **Prove an alert reaches you.** Not a container check — an actual
   notification on the actual phone:

   ```bash
   # on any service VM
   systemd-run --unit=testalert -p OnFailure=notify-failure@testalert.service \
       -p Type=oneshot /bin/false
   systemctl reset-failed testalert.service
   ```

7. **Pause one healthchecks.io check** in its web UI and confirm the external
   email or push actually arrives. A check with no notification channel is
   worse than no check, because it looks like coverage.

## What will page you

| Alert title | Means | First response |
|---|---|---|
| `UNIT FAILED on <host>: <unit>` | a timer or service failed; the journal tail is in the body | read the body; most are self-explanatory |
| `FAILED UNITS on <host> (n)` | something has been failed for a while | `systemctl status`, fix, `systemctl reset-failed` |
| `STILL FAILED on <host> (n)` | the same set, six hours later | as above; it will repeat every 6h until cleared |
| `recovered on <host>` | nothing is failed any more | none — this closes an earlier alert |
| `DISK alarm on <host>` | local FS ≥85% or NFS ≥90% | see [Incidents](incidents.md); the 2026-07-20 outage started here |
| `PVE disk alarm` | a ZFS pool or PVE root ≥80% | **most urgent of the disk alerts** — a full pool freezes every guest |
| `ZFS on thurgadin: …` | zed saw a pool event | `zpool status -v`; a degraded pool needs hands |
| `SMART warning on thurgadin` | a disk is reporting problems | order a replacement; this is your warning time |
| `homelab-certwatch` failed | the wildcard is under 21 days and has not renewed | check `certbot-renew.service` and the Cloudflare token — everything goes untrusted if this expires |
| `homelab-backups-fresh` failed | some VM's newest backup is over 26h old | body names the host and its newest file |
| `homelab-verify@svcops` failed | nightly verification failed | body has the Ansible output; a real application fault |
| `homelab deploy FAILED` / `VERIFY FAILED` | you ran something remotely and it broke | ordinary deploy failure |
| an email from healthchecks.io | **a ping stopped arriving** | see below — this is the serious one |

Alerts deduplicate: a persisting condition re-alerts every six hours rather
than every fifteen minutes, and clearing one sends a single all-clear. If your
phone is quiet, nothing has been suppressed.

## The dead-man's switch

Five jobs ping [healthchecks.io](https://healthchecks.io) when they succeed.
healthchecks.io alerts *you* when a ping does not arrive:

| Check | Pinged by | Period / grace | Its silence means |
|---|---|---|---|
| `homelab-verify` | nightly verification, svc-infra 04:00 | 1 day / 6 h | verification stopped running at all |
| `homelab-backups` | backup freshness check, svc-infra 04:40 | 1 day / 6 h | svc-infra is down or the NAS is unreachable |
| `homelab-pve` | health check on the hypervisor, 06:30 | 1 day / 6 h | the hypervisor is gone — so is everything else |
| `homelab-heartbeat` | svc-media, 07:00 | 1 day / 3 h | **ntfy is down, so no other alert can reach you** |
| `homelab-scan` | nightly security scan, svc-infra 05:30 | 1 day / 6 h | the scan stopped running, so its silence is not "nothing found" |

The last one is the keystone. Every inward alert travels through ntfy on
svc-media; that heartbeat is the only thing that can report svc-media's own
death, and it checks that ntfy answers before reporting.

### One-time setup

Nothing automates this — the account is yours.

1. Sign up at healthchecks.io (the free tier covers 20 checks; this uses 5).
2. **Add notification channels first** — email plus a phone push. A check with
   no channel fails silently.
3. Create the five checks above with those periods and grace windows.
4. Copy each check's ping UUID — the last path element of its ping URL, not
   the whole URL — and run `make vault-edit`:

   ```yaml
   vault_hc_ping_verify:    "…"
   vault_hc_ping_backups:   "…"
   vault_hc_ping_pve:       "…"
   vault_hc_ping_heartbeat: "…"
   vault_hc_ping_scan:      "…"
   ```

   `vault_hc_ping_scan` behaves differently from the other four: leaving it
   empty omits the key from `/etc/homelab-healthchecks.env` entirely rather
   than rendering it blank. The verification gate rejects any *empty*
   `HC_PING_*` value, and svc-media renders the same file while never running
   the scan, so an unconditional key would fail verification on a host that has
   no use for it. The scan still runs and still reports without the UUID — it
   just has no external safety net, and says so in its journal on every run.

5. `make deploy`, then trigger one job by hand and confirm the ping lands.

Empty UUIDs are safe: the ping is skipped and logged (`journalctl -t hc-ping`)
and nothing fails. That is also exactly what "no external safety net" looks
like, which is why step 7 of the departure checklist exists.

## What is *not* covered

Stated plainly, because a monitoring page that implies total coverage is worse
than one that admits its edges:

- **Nothing updates itself.** Images are digest-pinned and packages are only
  installed on demand. Six months away means six months of unpatched
  containers — a deliberate trade of security currency for the certainty that
  nothing changes under you while nobody is watching.

  What changed in July 2026 is that the *cost* of that trade is now measured
  rather than assumed. The nightly scan (see [Security](security.md)) counts
  pending errata and image CVEs and publishes the number; it still applies
  nothing. The trade is unchanged, but it is now an informed one, and a sudden
  jump in the count is escalated rather than discovered six months later.
- **Nothing restarts a wedged application.** A container that is up but not
  working is caught by the nightly verification only if a gate covers it.
- **No alert can fix anything.** Every response above needs a network path
  home; keep Tailscale working on the phone and a laptop.
- **The vault is not in git.** A total loss needs the vault out of band, or
  every secret gets regenerated.
- **SSO protects the proxied path, not the service.** `forward_auth` applies to
  `https://<name>.<domain>`. Every protected service stays reachable
  unauthenticated on its direct `IP:port` from `lan_cidr` and the tailnet. That
  is deliberate — it is the way back in if Authelia itself breaks while nobody
  is home — but it means the SSO layer is a boundary against the internet and
  against casual browsing, not against anything already on the LAN.
- **The nightly runner is root-equivalent across the estate.** `svcops` on
  svc-infra holds a passphraseless SSH key authorised for the `NOPASSWD:ALL`
  deploy account on all three VMs, plus `.vault_pass`. That capability used to
  exist only on the workstation, which is powered off; it is now on an
  always-on VM. Accepted knowingly: the runner cannot verify anything it cannot
  reach, and the alternative is no verification while away. Recorded as a
  follow-up in `automation-opportunities.md`.

### The `changed=0` check needs two runs after a commit

The workflow in `CLAUDE.md` ends with a clean-tree deploy reporting `changed=0`.
Immediately after any commit, the first `make infra` reports **`changed=3`**:
`.deployed-rev` on svc-infra still holds the previous revision, so the runner
checkout-sync block rebuilds the archive, unpacks it and records the new
revision. Run `make infra` a second time and it settles to `changed=0`. This is
expected, not drift — but it does mean the guarantee is "the second deploy is
clean", and step 5 of the departure checklist above should be read that way.

## Coming home

```bash
make validate && make preflight
make verify
git -C . status --porcelain    # must be empty
```

Then read `journalctl -t hc-ping -t notify-failure` on each host for anything
that fired and cleared while you were gone — the ntfy history itself is only
kept for about 12 hours in memory, so the journals are the durable record.
