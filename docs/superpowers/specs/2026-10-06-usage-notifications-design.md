# Usage notifications — design

**Status: designed, 2026-10-06. Sub-project 1 not yet implemented.**

The goal is to see how the household uses the estate: who requested what, what
arrived in the library, who is watching what, and what failed. Today every
notification in this repo is about the estate's health. Nothing records
usage.

The audience is the owner only. Household members get no new notifications.

## Shape: push for what is live, digest and dashboard for the rest

- **Live push** for four kinds of event: new requests, downloads finished,
  failures, and playback started.
- **Weekly digest and Grafana dashboard** for everything else, including the
  history behind those pushes.

Pushing every event would get the topic muted within a week, which is what
already happened to the routine `homelab-deploy` topic. That is why only four
kinds of event push, and each push topic is separate so it can be muted on
its own.

## Decomposition

The whole estate is in scope, but it ships as five sub-projects. They share
one backbone, and each gets its own spec and plan when it is picked up.

| # | Sub-project | Sources | Status |
|---|---|---|---|
| 1 | **Backbone + video pipeline + digest/dashboard** | Jellyfin, Seerr, Sonarr, Radarr, SABnzbd | **This spec** |
| 2 | Books & audio | Shelfmark, LazyLibrarian, Audiobookshelf, Calibre-Web | Later |
| 3 | Sign-ins | Authelia log: successful sign-ins per user and service, failed logins | Later |
| 4 | Other apps | Immich uploads, Open WebUI chats/images, Mealie, RomM | Later |

Sub-projects 2 to 4 only add collectors and new values of `kind`. They should
not need changes to the relay's core. If one does, that is a sign this
design's interface is wrong, and the change should be made here first.

## Approaches considered

- **Each app notifies ntfy directly** through its built-in ntfy or Apprise
  integration. Rejected for three reasons:
  - There is nothing durable for a digest to read: ntfy retention is about
    12h and held in memory.
  - Counts would come from a second source that can disagree with the
    pushes. This breaks the rule "emit the number the alert used".
  - Configuration would live in five apps' databases rather than in git.
- **Off-the-shelf stats apps** (Jellystat, Audiobookshelf stats, Loki).
  Rejected because they give four places to look and no single digest, and
  pushes would still need the first approach.
- **A small relay that owns the event log**: chosen.

## Architecture

```text
            svc-infra
            ┌───────────────────────────────────────────────────────┐
 Jellyfin ──┤ collector ─┐                                          │
 Seerr ─────┤ collector ─┤                         ┌─► ntfy (push)  │
 Sonarr ────┤ collector ─┼─► normalize ─► insert ──┼─► /metrics ◄── Prometheus
 Radarr ────┤ collector ─┤      (dedup on id)      └─► SQLite ◄── digest timer
 SABnzbd ───┤ collector ─┘                                          │
            └───────────────────────────────────────────────────────┘
```

### Ingestion pulls; it does not receive webhooks

The download apps run in svc-download's VPN namespace. The only egress from
that namespace is wg0. The veth link carries host-to-jail traffic only, and
`host-backstop.nft` drops anything the jail initiates. A webhook from Sonarr
to svc-infra would need a jail-to-host path and a forwarder. That is a hole in
the boundary the leak canary exists to defend.

So **the relay polls each service's own history or session API.** svc-infra
already reaches the download UI ports on the LAN, the same way Prometheus
does. This brings three further benefits:

- **All configuration is in git.** Only API keys go in the vault. Webhooks
  would be configured by hand in each app's database, which is the same "the
  row wins" drift documented for Open WebUI in `docs/services.md`.
- **It can catch up.** Each collector keeps a high-water mark, so a relay
  restart resumes from history rather than losing events.
- **It adds nothing to the jail.**

What polling costs:

- **Latency:** up to the poll interval.
- **Short playbacks:** a playback shorter than one Jellyfin poll may never be
  seen.
- **API keys:** five more in the vault.

### Collectors

| Collector | Endpoint | Interval | Emits |
|---|---|---|---|
| `jellyfin` | `GET /Sessions` | 30s | `playback.started`, `playback.stopped` |
| `seerr` | `GET /api/v1/request?sort=modified` | 60s | `request.created`, `request.approved`, `request.available` |
| `sonarr` | `GET /api/v3/history/since?date=<hwm>` | 60s | `download.grabbed`, `download.imported`, `download.failed` |
| `radarr` | `GET /api/v3/history/since?date=<hwm>` | 60s | same as Sonarr |
| `sabnzbd` | `GET /api?mode=history` | 60s | `download.completed`, `download.failed` |

**Jellyfin has no history endpoint, so playback is inferred by comparing
polls.**

- A `(session id, item id)` pair that appears is `playback.started`.
- When the pair disappears, or the item changes, that is `playback.stopped`.
  It carries the last observed position, which gives the watched duration.
- `detail` records the device name and whether the session is direct play or
  transcoding. That flag answers whether a given session is costing CPU. A
  4K HEVC stream with burned-in PGS subtitles was diagnosed as CPU-bound on
  2026-10-01.

**Each collector is independent.** One collector's failure never stops the
others.

**Interface for later sub-projects.** A collector is a function that takes
its high-water mark and returns new events plus a new mark. That signature is
what sub-projects 2 to 4 implement.

## Event model

Every collector emits one shape. That row is what gets pushed, stored and
counted.

| Field | Meaning |
|---|---|
| `id` | The source's own identifier, e.g. `sonarr:history:48213` or `jellyfin:session:<sid>:<itemId>`. Primary key. |
| `ts` | When the event happened, according to the source (UTC). |
| `service` | Collector name. |
| `kind` | One of the fixed set below. |
| `user` | Household user, or null where the source has none. |
| `title` | Display title, e.g. `The Bear S02E03`. |
| `detail` | JSON: device, play method, quality, failure reason, external ids. |
| `pushed` | Push state: `pending`, `sent`, `suppressed`, `expired`, or `none` for kinds that are recorded but never pushed. |

**Kinds:**

- `request.created`, `request.approved`, `request.available`
- `download.grabbed`, `download.completed`, `download.imported`, `download.failed`
- `playback.started`, `playback.stopped`
- `selftest`

**Deduplication is the primary key.** An insert that conflicts is dropped.
That makes three things harmless:

- poll windows that overlap,
- a restart that catches up from history,
- a collector that re-reads the same page.

**Requester attribution.** Sonarr and Radarr imports have no user. On insert,
the relay looks up the most recent `request.created` with the same TMDB or
TVDB id and copies its user into `detail.requested_by`. If nothing matches,
the import is unattributed. The relay does not guess.

## Push routing

| Topic | Kinds pushed | Priority | Example |
|---|---|---|---|
| `usage-requests` | `request.created` | default | 📥 **Valerie** requested *Dune: Part Two* (movie) |
| `usage-library` | `download.imported` | default | ✅ Ready: *The Bear* S02E03 · requested by Valerie |
| `usage-playback` | `playback.started` | low | ▶️ **Erin**: *The Bear* S02E03 · Living Room TV · transcoding |
| `usage-failures` | `download.failed` | high | ⚠️ Radarr failed: *Dune: Part Two*: no matching file |
| `usage-digest` | weekly digest | default | see below |
| `usage-selftest` | `selftest` | min | read back by verify; nobody subscribes |

**Recorded but never pushed:**

- `download.grabbed`
- `download.completed`: SABnzbd's completion would duplicate the import push.
- `playback.stopped`
- `request.approved`, `request.available`: the import push already says it
  is ready.

**Failures are not routed to `NTFY_ALERT_TOPIC`.** That topic means the estate
is broken, and a bad release does not mean that. The exception is the
relay's own health (see Failure handling), which does go there.

### Noise controls

- **Season-pack batching.** `download.imported` events for the same series
  within `usage_batch_window` (120s) are combined into one push, e.g. "✅ *The
  Bear* S02: 10 episodes ready". Each event is still stored as its own row.
- **Resume suppression.** A `playback.started` for the same user and item
  within `usage_resume_window` (30 min) of a previous one is stored with
  `pushed=suppressed`. Pausing and resuming often gets a fresh session id.
- **Ignored users.** Playback by users in `usage_push_ignore_users` (default
  `[brandon, admin]`) is recorded but never pushed. The owner's own viewing
  is not news. This setting applies to playback only. A request from the
  owner's account is still pushed.

## Metrics

The relay is long-running, so it serves `/metrics` itself on port 9470
(`usage_relay_port`). firewalld opens that port to svc-infra's own address
only. Prometheus on the same host scrapes it through
`host.containers.internal`, the same way it reaches svc-infra's
node_exporter. The textfile collector is for
one-shot jobs and is not used here.

| Series | Type | Notes |
|---|---|---|
| `homelab_usage_events_total{service,kind,user}` | counter | Bumped on successful insert only, so dedup cannot double-count |
| `homelab_usage_playback_active{user,mode}` | gauge | `mode` is `direct` or `transcode`; from the latest Jellyfin poll |
| `homelab_usage_watch_seconds_total{user}` | counter | Incremented by each `playback.stopped` duration; feeds the hours-watched panel |
| `homelab_usage_collector_last_success_timestamp{collector}` | gauge | Freshness: tells a quiet house from a blind collector |
| `homelab_usage_collector_errors_total{collector}` | counter | |
| `homelab_usage_push_failures_total{topic}` | counter | |

**Titles are never labels.** That would give unbounded cardinality. Titles
live in SQLite and the digest. `user` is bounded by the Authelia account list.
An unknown or null user is emitted as `user="none"`, never as an empty label.

## Dashboard

A provisioned **Household usage** Grafana dashboard. Like the other two, it is
repo-owned with `allowUiUpdates: false`, and its default range is 30 days.

Panels:

- Plays per person per day
- Hours watched per person, from `homelab_usage_watch_seconds_total`
- Direct play vs transcode share
- Requests per person
- Imports per day
- Failures
- A freshness row from `collector_last_success_timestamp`

## Weekly digest

`homelab-usage-digest.timer` on svc-infra runs Sunday at 18:00. It executes
`usage-relay digest`, which reads the last 7 days from the same SQLite file
the counters came from. It publishes the text in two places:

- the `usage-digest` ntfy topic,
- `https://scan.<domain>/usage.txt`, next to `releases.txt`, so it outlasts
  ntfy's retention.

```text
Week of Sep 28 – Oct 4
Valerie   9 plays · 7.2h · The Bear (6), Dune: Part Two
Erin      4 plays · 3.1h · Severance (4)
Requests  3 (Valerie 2, Erin 1) · 2 fulfilled, 1 pending
Library   +14 episodes, +2 movies · 1 failed import
Streams   62% direct play, 38% transcode
Read      61 events · 2 active accounts
```

**Accounts with no activity that week are left out.** Leaving them out means
a missing name could also be a digest that failed to read that person's rows.
The closing `Read` line rules that out: it reports how many rows the digest
actually read and how many accounts had activity.

A week with no activity at all still sends a digest, with the line
`No activity` followed by `Read 0 events · 0 active accounts`. If no digest
arrives, the timer did not run; that is not the same as a quiet week. A
query error makes `usage-relay digest` exit non-zero without publishing. A
digest built from a failed read would look like a quiet week.

## Runtime

- **Code.** Stdlib-only Python 3.12, the version Rocky 10 ships, using
  `urllib`, `sqlite3`, `http.server` and `threading`.
  - It ships as a flat package of static files in
    `roles/svc_infra/files/usage_relay/`, which keeps the `changed=0` proof
    intact.
  - It is installed to `/opt/usage-relay/usage_relay/` and run with
    `python3 -m usage_relay`.
  - There is no container image to pin or bump.
- **Service.** `usage-relay.service`, a host systemd unit on svc-infra. This
  follows the release and scan runners, which also run repo-owned code on the
  host. It also sidesteps rootless-podman UID mapping entirely.
  - It runs as the existing `homelab` service user (uid 10001).
  - A dedicated user was the first design. It was dropped because the
    nightly backup tars appdata as `homelab` through `podman unshare`, and a
    snapshot owned by anyone else would need group-permission plumbing to
    stay readable.
  - Hardening: `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
    `NoNewPrivileges=yes`. Writable paths are its `StateDirectory=` plus the
    snapshot directory.
  - A start limit (5 failed starts in 10 minutes) lets a crash-looping relay reach `failed`, where the estate's existing failed-units watcher alerts. It is deliberately not in `onfailure_units_extra`: that list is for timer-triggered units.
- **State.**
  - The live database is `/var/lib/usage-relay/usage.db`
    (`StateDirectory=usage-relay`), opened in WAL mode.
  - Once a night, at 02:30 and before the 03:xx backups, the relay writes
    `/opt/homelab/appdata/usage-relay/usage.snapshot.db` via `VACUUM INTO`.
    It also writes one at startup if none exists.
  - The snapshot is the only file in that directory, so the backup can only
    ever tar a consistent copy. A live WAL database copied mid-write is not a
    backup.
  - `usage-relay` is added to `infra_extra_backup_paths`.
  - To restore, stop the relay and copy the snapshot over `usage.db`.
- **Retention.** Rows older than `usage_retention_days` (default 365) are
  deleted nightly. This is household viewing history, and it is bounded on
  purpose.
- **Config.** `/etc/usage-relay/config.json` is rendered from inventory:
  collector base URLs, intervals, topics, windows, the ignore list and
  retention. Keys come from the vault and are rendered into a separate
  `0600` env file with `no_log: true`.
- **Firewall.** The metrics port is opened to svc-infra's own Prometheus
  only. The relay makes outbound calls to svc-media (Jellyfin, Seerr, ntfy)
  and svc-download's UI proxy ports. Both are already reachable from
  svc-infra on the LAN.

### Vault additions

| Key | Source |
|---|---|
| `vault_usage_jellyfin_api_key` | Jellyfin → Dashboard → API Keys; created once by hand |
| `vault_usage_seerr_api_key` | Seerr → Settings → General |
| `vault_usage_sonarr_api_key` | Sonarr → Settings → General |
| `vault_usage_radarr_api_key` | Radarr → Settings → General |
| `vault_usage_sabnzbd_api_key` | SABnzbd → Config → General |

Creating the keys is added to "One-time UI wiring" in `docs/services.md`.

**A missing key disables that collector and logs a warning, the same pattern
as the Beszel agents.** A disabled collector is not reported as healthy. The
verify step names it as `disabled`, so it cannot read as "looked and found
nothing".

### Seerr is ready

The household-guide audit of 2026-09-27 found Seerr reporting
`"initialized": false`. That has since been fixed. Re-checked on
2026-10-06, `/api/v1/settings/public` returns `"initialized": true` with
`mediaServerType: 2` (Jellyfin). So the Seerr collector has no prerequisite
beyond its API key.

## Behaviour on first run

**Each collector's high-water mark starts at the time of the first
successful poll, not at the start of the source's history.** Without this,
the first deploy would push a year of Sonarr imports. Jellyfin's first poll
records the sessions already in progress as a baseline without pushing them.

## Failure handling

| Failure | Behaviour |
|---|---|
| Source unreachable, 401, 5xx, timeout | Increment `collector_errors_total`; keep the high-water mark; retry next interval. Other collectors unaffected. |
| Response parses but has an unexpected shape | Log the raw body (truncated to 2 KiB, with any API key redacted); skip the whole response. Never insert a partial row. |
| A collector has had no success for 15 min | One push to `NTFY_ALERT_TOPIC` naming the collector and its last error. One recovery push when it succeeds again. No repeats in between. |
| ntfy unreachable | Rows stay `pushed=pending` and retry each cycle. When ntfy returns: `playback.*` pushes older than 10 min become `expired` (stale noise). Requests, imports and failures are still sent. |
| Relay crash | `Restart=on-failure`. Catch-up from the high-water marks plus dedup means nothing is lost or doubled, except Jellyfin playbacks that started and ended entirely during the outage. |
| SQLite write failure | Fatal for the process. Exit non-zero and let systemd restart it. A relay that pushes without recording would break "emit the number the alert used". |

## Verification

`make verify`, and therefore the nightly 04:00 run, adds two checks to
svc-infra's verify tasks.

1. **End-to-end positive control.** `python3 -m usage_relay selftest`:
   - asks the running relay to insert an event with `kind=selftest` and a
     unique id, through `POST /selftest`, which only accepts loopback
     callers,
   - waits for the push cycle,
   - asserts the event can be read back from `usage-selftest` via
     `/json?poll=1&since=5m`,
   - asserts `homelab_usage_events_total{kind="selftest"}` increased,
   - asserts the row exists in SQLite,
   - then deletes the row.

   This proves the whole pipeline works, not just that the unit is
   `active`. Selftest rows are excluded from the digest and dashboard.

2. **Each enabled collector actually looked.** Each one's
   `last_success_timestamp` must be under 5 minutes old.
   - An empty `/Sessions` that returned 200 counts as having looked.
   - A stale timestamp fails, naming the collector.
   - A disabled collector is reported as `disabled`, not as a pass or a
     failure.

The verify tasks are read-only apart from the selftest's own row. They
pass `notify_on_success=false` like the rest of the nightly run.

## Testing

Offline, under `make validate`:

- **Stdlib `unittest` tests for each collector's parser,** against recorded
  API responses saved as fixtures. They are run by a new discovered gate,
  `tests/validate_usage_relay.py`. The repo has no pytest dependency, and
  one feature is not reason enough to add one. Fixture values use `example-*` names and keys, since
  realistic-looking fake credentials trip the history scan.
- **Core logic:**
  - dedup on conflicting ids,
  - the season-pack batching window boundary,
  - resume suppression inside and outside the window,
  - the ignore list applies to playback only,
  - the first-run high-water mark pushes nothing,
  - expiry of stale playback pushes after an ntfy outage,
  - requester attribution with and without a matching request,
  - digest rendering:
    - inactive accounts are omitted,
    - the `Read` line counts match the rows queried,
    - an empty week renders `No activity`,
    - a query error exits non-zero without publishing.
- **Jellyfin session diffing:** start, stop, item change within the same
  session, and a baseline poll.
- **A validate gate** checking that every collector named in the config has a
  key reference in the env template. This catches a collector added without
  its credential.

Live, during deploy iteration:

- Play something on a household account and confirm the `usage-playback`
  push arrives with the right device and play method.
- Import a test episode and confirm `usage-library` fires once.
- Stop the relay for 2 minutes, restart it, and confirm no duplicate pushes.
- Run verify and confirm the selftest round-trip passes.

## Out of scope

- Notifications to household members, e.g. "your request is ready". Seerr
  can do this natively per user if wanted later. It is a configuration task,
  not part of this relay.
- A web UI for browsing events. The digest covers titles and Grafana covers
  trends. Revisit only if those prove insufficient.
- Sub-projects 2 to 4.
