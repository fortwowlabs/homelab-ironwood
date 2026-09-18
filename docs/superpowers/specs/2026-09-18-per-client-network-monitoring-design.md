# Per-client network traffic monitoring — phase 1 design

Date: 2026-09-18
Status: design approved, not implemented
Phase: 1 of 3 (per-client bandwidth accounting)

## The question this answers

"Which device used how much internet bandwidth, when?" — answerable per device,
by name, over a window of at least a month, from a Grafana dashboard.

Two further questions are explicitly deferred to later phases so that this one
can ship and be trusted first:

- **Phase 2 — what are they talking to.** Per-device destination visibility by
  name, via Suricata `dns` and `tls` (SNI) events into Loki.
- **Phase 3 — alerting on traffic.** Suricata IDS signatures for known-bad, plus
  behavioral rules ("this camera has never uploaded 2 GB before") on the phase 1
  metrics.

Phases 2 and 3 reuse this phase's observation point, its device-identity map and
its Grafana/ntfy consumption surface. They are not designed here beyond the
sketch in the final section, because designing them now would bake in
assumptions that phase 1 has not yet tested.

## Decisions already settled

These were decided during design and are not open questions. They are recorded
so a later reader does not reopen them without new information.

| Decision | Value | Why |
|---|---|---|
| Observation point | pfSense, LAN interface | Only traffic crossing the router is in scope |
| Traffic scope | Internet-bound only | Intra-LAN is a known, accepted blind spot |
| Addressing | IPv4 only | No IPv6 delegated; per-IP keying is therefore valid |
| Store for volume | Prometheus | ~50 devices is ~100 series; fits the existing bridge |
| Store for detail | Loki (phase 2) | Destinations are high-cardinality; Prometheus is the wrong shape |
| Alert type | Both signature and behavioral | Deferred to phase 3 |
| Forensic retention | Not a goal | Retention is sized for trending, not investigation |

### The intra-LAN blind spot, stated plainly

The LAN is flat: clients and the three service VMs all live in
`192.168.1.0/24`. Traffic between two hosts on that segment is switched locally
and **never reaches the router**. A laptop streaming from Jellyfin on
`192.168.1.30`, or pulling from SABnzbd on `192.168.1.31`, is invisible to this
design and always will be.

On a media-heavy homelab that is plausibly the majority of all bytes moved. This
was accepted deliberately: closing it requires either VLAN segmentation that
forces the traffic through pfSense, or a switch SPAN port feeding a passive
sensor. Neither is justified by the questions being asked. The dashboard must
therefore be titled and described so that nobody reads it as total network
usage — it is **internet usage**, and the difference is large.

## Architecture

```text
pfSense 192.168.1.1                      svc-infra 192.168.1.32
───────────────────                      ──────────────────────
softflowd (LAN interface)
        │ NetFlow v9 / UDP 2055
        ▼
                                  goflow2 container
                                         │ newline-JSON flow records
                                         │ /opt/homelab/appdata/goflow2/flows.json
                                         ▼
                                  netflow-rollup.py        systemd timer, 60s
                                         │ per-client cumulative byte counters
                                         │ + synthetic positive control
                                         ▼
                                  homelab-metric-write     existing bridge, unchanged
                                         │
                                         ▼
                                  textfile collector ──► Prometheus ──► Grafana
                                                                │         "homelab-network"
                                                                └──────► ntfy (pipeline health)

network_clients map (group_vars) ──────► IP → device name, at rollup time
```

## Component 1 — softflowd on pfSense

**What it does.** Observes packets on the LAN interface, assembles them into
flow records, and exports NetFlow v9 over UDP to svc-infra.

**How it is managed.** By hand, documented as a runbook in
`docs/network-flow-monitoring.md`. pfSense is not an Ansible target in this repo
(see `docs/dns-pfsense-caddy.md`); its settings stay manual and the runbook is
the record.

**Configuration that matters:**

- **Interface: LAN, not WAN.** This is the single most important setting in the
  design. On the WAN interface, pf has already applied outbound NAT, so every
  flow's source address is the router's own WAN IP. Per-client identity is gone
  completely — and the failure is silent, because the graphs populate normally
  and are uniformly wrong. On the LAN interface the addresses are pre-NAT, and
  because only router-crossing traffic reaches that interface, the capture is
  already scoped to internet-bound with no filtering needed.
- **NetFlow version 9.** v5 carries no 64-bit counters and no IPv6 headroom.
- **Collector: `192.168.1.32:2055`.**
- **Active timeout: 60s** (default is one hour). A long-running transfer is one
  flow, and a flow reports nothing until it expires. Left at the default, a
  four-hour download contributes zero bytes for four hours and then a single
  multi-gigabyte spike. The graph is not merely coarse, it is attributing bytes
  to the wrong time. Nothing errors when this is wrong.
- **Inactive timeout: 15s** (the default). This sets how quickly the positive
  control's flow becomes visible, which the rollup's grace window depends on.

**Blocking pre-check.** Confirm the `softflowd` package exists for the installed
pfSense version before any other work begins. If it has been dropped, this
design needs a different exporter and the collector side changes with it. This
is step 0 and it blocks — it is not an assumption to be discovered later.

## Component 2 — goflow2 collector

**What it does.** Listens on UDP 2055, decodes NetFlow v9, and writes one JSON
object per flow record to a file.

**How it is deployed.** A new entry in `infra_apps` rendering through the
existing `infra-app.container.j2` Quadlet template. The template already
supports `extra_ports: [{port: 2055, protocol: udp}]`, so no template change is
needed for the port.

**Dependencies.** A pinned image digest recorded per the `BUMP PROCEDURE` block
in `apps.yml`. Untracked (no `# tag:`) unless a tag is confirmed to resolve to
the pinned digest.

**Firewall.** `2055/udp` is opened to pfSense's address only, following the
pattern already used for `:9100`, which is opened to svc-infra alone rather than
to `lan_cidr`. See "Risks" below for why this is load-bearing rather than tidy.

### File handoff and rotation

goflow2 appends; the rollup consumes. The handoff is by byte offset stored in
the rollup's state file, with rotation by logrotate (daily, 3 days,
`copytruncate`) to bound disk.

`copytruncate` means the file can shrink under the reader. The rollup detects
this by comparing its stored offset against the current file size: if size is
less than the stored offset, the file was rotated and the offset resets to zero.
Without that check the rollup would seek past EOF after every rotation and
silently report zero flows forever. This path has a fixture (see Testing).

## Component 3 — netflow-rollup.py

**What it does.** Once a minute: fire the positive control, read new flow records
since the last offset, classify and attribute each one, update cumulative
counters, verify the positive control, and publish via `homelab-metric-write`.

**Where it lives.** `roles/svc_infra/files/netflow-rollup.py`, installed by the
role along with a `.service` and `.timer`.

Note for future readers: the standing rule that metric writes go in the play and
never in `roles/svc_infra` exists because a template task writing a
file-that-changes-every-run would make every `make infra` report `changed` and
destroy the `changed=0` proof. That rule does not apply here. Ansible installs a
*static* script and unit; the *writing* is done by a systemd timer on the host,
at a cadence Ansible is not involved in. Installing this in the role is correct
and does not affect idempotence.

### Classification

For each flow record, with `lan_cidr` = `192.168.1.0/24`:

| src in LAN | dst in LAN | Action |
|---|---|---|
| yes | no | `egress`, attributed to src |
| no | yes | `ingress`, attributed to dst |
| yes | yes | skipped, counted |
| no | no | skipped, counted |

Skipped flows are counted and published rather than silently discarded, so a
misconfigured capture interface shows up as a large skip count instead of as
plausible-looking but incomplete data. `reason` takes exactly three values:
`both_lan`, `neither_lan`, and `malformed` for a line that did not parse. A
corrupt *state* file is not a flow skip and does not appear here — it is a
condition of the reader, not of the data, and is handled in the table below.

### Metrics published

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `homelab_netflow_client_bytes_total` | counter | `client`, `ip`, `direction` | Cumulative bytes per device |
| `homelab_netflow_other_unmapped_bytes_total` | counter | `direction` | Aggregate for unmapped devices beyond the top 20 |
| `homelab_netflow_unmapped_clients` | gauge | — | Count of distinct unmapped source addresses seen |
| `homelab_netflow_flows_processed_total` | counter | — | Flow records successfully attributed |
| `homelab_netflow_flows_skipped_total` | counter | `reason` | Flow records not attributable |
| `homelab_netflow_positive_control` | gauge | — | 1 if the synthetic control flow was observed |
| `homelab_netflow_last_flow_timestamp_seconds` | gauge | — | Export timestamp of the newest record seen |
| `homelab_netflow_run_timestamp_seconds` | gauge | — | Emitted by `homelab-metric-write` every run |
| `homelab_netflow_last_success_timestamp_seconds` | gauge | — | Emitted only when `--success` is passed; carried forward otherwise |

Counters rather than per-interval gauges: cumulative totals mean `increase()`
answers any window — last hour, last month — from a single series, and
Prometheus already handles the reset when the collector restarts. Per-interval
gauges would require the dashboard to sum buckets and would lose data on any
missed scrape.

`client` carries the friendly name; `ip` carries the address. For an unmapped
device `client` is set to the bare address, so the series is still usable and
still legible. The aggregate bucket is a separate metric rather than a sentinel
label value, so that label sets stay consistent within each metric name.

The last two come from `homelab-metric-write` itself. **`--success` is passed
only when the positive control passed**, which is what makes the tool's
carry-forward behaviour do the right thing: on a cycle that ran but could not
look, the run stamp advances while the last-success stamp freezes at its old
value. "It ran and could not see anything" and "it measured a zero" are then
different shapes on the chart rather than the same one.

The rollup emits no `# HELP` or `# TYPE` lines, matching every other emitter
here: node_exporter merges every `.prom` in the directory and duplicate TYPE
declarations across merged files make it reject them outright.

### Cardinality bound

Series count is bounded by device count, which is bounded by the LAN. Guest
wifi could still churn short-lived unmapped addresses, so the rollup emits at
most the top 20 unmapped clients by bytes and aggregates the remainder into
`homelab_netflow_other_unmapped_bytes_total`.

Ranking is by **cumulative** bytes, not by bytes this cycle. Cumulative totals
only grow, so the top-20 set is stable; ranking on a single cycle would make
series flap in and out of existence every minute and produce gaps that look
like data loss.

Per this repo's no-silent-caps rule, the cap is visible:
`homelab_netflow_unmapped_clients` publishes the count of distinct unmapped
addresses seen since the state file was created — the same basis as the
ranking — so a dashboard showing 20 rows and a count of 63 reads as a cap
rather than as the whole picture.

## Component 4 — the device identity map

**What it does.** Maps `192.168.1.57` to "Valerie's iPad".

**Where it lives.** `network_clients` in `inventory/group_vars/all/main.yml`, a
dict of IP to friendly name.

**Why it is duplicated from pfSense.** pfSense holds the DHCP reservations and
is the real source of truth, but it is not an Ansible target and pfSense CE
exposes no API for reading them without installing a community REST API package
on the router. Duplicating roughly fifty short lines into the repo was judged
cheaper and less fragile than adding an API surface to the firewall. The runbook
records the obligation to keep the two in step.

**How drift is caught.** Unmapped addresses are never dropped. They are emitted
under their bare address and surfaced in a dedicated dashboard panel, so a new
device on the network appears as a question rather than as nothing. This is the
mitigation for the duplication: the map being out of date is *visible* rather
than silently lossy.

## Component 5 — Prometheus retention, and an `exec:` field

Prometheus currently runs on image defaults, which means **15-day retention**.
A dashboard that cannot answer "last month" fails the stated goal, so retention
moves to **45 days** — a month plus margin.

There is no way to set it today: `infra-app.container.j2` renders no `Exec=` key
and the catalog has no field for container arguments. The fix is a small,
generic addition — an optional `exec:` field rendering `Exec=` — rather than
forking Prometheus into a bespoke Quadlet. Every future image benefits.

**The trap this creates.** Setting `Exec=` replaces the image's default command
entirely. A catalog entry carrying only `--storage.tsdb.retention.time=45d`
loses `--config.file` and `--storage.tsdb.path`, and Prometheus will not start.
The entry must reproduce the image's default flags in full. This passes
`make validate` and fails on the host, so it is called out here and belongs in a
comment on the catalog entry itself.

**Sizing pre-check.** Tripling the retention window roughly triples the TSDB.
svc-infra's disk headroom is checked *before* the change, not after. This repo
has already had one outage from a pool filling up; the check is not optional.

**Blast radius.** The template change touches a file every `infra_apps` service
renders through, so it requires a full `make infra` reaching `changed=0` — after
the documented `changed=3` first-run floor — before it is trusted.

## Component 6 — the positive control

A dead collector and a quiet network produce byte-identical output: all zeros.
This is the exact failure mode this repo has hit at least five times — the image
scan that could not `chdir`, the benchmark whose counter broke, the port sweep
that found one port of eleven, the CSRF-protected login that returned identical
responses for right and wrong passwords. Every one of them passed
`make validate` and reported clean.

So this pipeline gets a **synthetic positive control**.

**Mechanism.** Each cycle, before rolling up, the timer makes one short outbound
HTTPS request from svc-infra to `https://1.1.1.1/cdn-cgi/trace`. That request
must cross pfSense, must be observed by softflowd, and must arrive as a flow
record with source `192.168.1.32`, destination `1.1.1.1`, destination port 443.
The rollup asserts it found one and publishes
`homelab_netflow_positive_control`.

**Why a raw IP.** The control tests flow capture, not name resolution. Using a
literal address keeps a DNS outage from being reported as a flow-pipeline
failure.

**Why this is stronger than the credential canary.** The canary lost its
positive control when Calibre-Web's password was fixed — a working canary and a
completely dead one now produce identical JSON, and restoring a real one would
mean standing up a deliberately insecure service. This control requires standing
up nothing insecure. It is a genuine required-positive-result: the metric cannot
read 1 unless the whole chain — pf capture, export, UDP delivery, decode, file
write, offset read, parse — actually worked.

**Timing.** NetFlow exports on flow expiry, not in real time. The control's flow
is not exported until softflowd's inactive timeout (~15s) fires, so the
assertion looks for a control flow within the **last 5 minutes**, not within the
current cycle. Asserting on the current cycle would fail permanently for reasons
that have nothing to do with correctness.

## Error handling — the cannot-look states

Following the tri-state discipline from `scan.yml`: "could not look" must be
distinguishable from "nothing found". A stale number is detectable; a zero reads
as good news.

| Condition | Behaviour |
|---|---|
| Flow file missing or unreadable | Exit before publishing; previous textfile left in place |
| Zero new records this cycle | Publish counters unchanged; do **not** publish zeros |
| Malformed JSON line | Skip the line, count it, continue |
| State file corrupt or unparseable | Reset offset to 0, publish, and withhold `--success` for that cycle |
| File shrank below stored offset | Treat as rotation, reset offset to 0 |
| Positive control absent 1 cycle | Publish `positive_control 0`; no alert yet |
| Positive control absent 2 cycles | ntfy alert |
| Last-flow age over 10 minutes | ntfy alert |

`homelab-metric-write`'s exit codes are already the right contract here: 0
published, 1 bad arguments or I/O failure, 2 malformed input (previous file
left in place), 3 no input lines at all (same). The rollup does not need to
reimplement that behaviour, and must not publish an empty metric set to signal
a problem.

## Component 7 — the Grafana dashboard

`roles/svc_infra/files/grafana-dashboards/homelab-network.json`, provisioned by
the existing dashboard provider. Panels:

1. **Top talkers** — bytes per device over the dashboard window, egress and
   ingress, as a sorted table.
2. **Per-device rate over time** — stacked, for spotting when and who.
3. **Monthly totals** — `increase()` over 30 days, the question retention was
   extended for.
4. **Unmapped devices** — addresses seen but not in `network_clients`, with the
   distinct-count alongside so the top-20 cap is legible.
5. **Pipeline health** — positive control, last-flow age,
   `time() - homelab_netflow_last_success_timestamp_seconds`, flows processed
   and skipped by reason, and `node_textfile_scrape_error`.

The description field states that this is **internet-bound traffic only** and
that intra-LAN traffic is not measured. The blind spot is documented where it
will actually be read, not only in this file.

Panel 5 is not decoration. Per the existing estate dashboard's precedent, if a
panel is empty the first thing to read is `node_textfile_scrape_error` — one
malformed line makes node_exporter reject an entire file, taking every series in
it down at once.

## Testing

### Offline gates (`make validate`)

**New — `tests/validate_network_clients.py`**, run from `validate-catalog`:
every key is a valid IPv4 address, every address falls inside `lan_cidr`,
addresses are unique, names are unique, no name collides with a reserved
sentinel, and **svc-infra's own address is present**, because it is the positive
control's source and an unmapped control host would make panel 5 confusing
exactly when it matters.

**Extended — `tests/validate_infra_catalog.py`**: the new `exec:` field is a
string or list of strings with no shell metacharacters, and `extra_ports`
entries carry a valid protocol.

**Inherited — `tests/validate_grafana_dashboards.py`**: already cross-checks
dashboard PromQL against metric names the plays emit, so a renamed metric fails
the build instead of silently blanking a panel. The new dashboard gets this for
free, which is the reason the metric names above are fixed in this document.

**New — fixture-driven tests for `netflow-rollup.py`.** This is the real test
surface, and it follows the precedent set by `validate_container_drift.py` and
`validate_dnf_makecache_retry.py`: these scripts' refusal paths are the point,
and on a healthy host they succeed every time, so without fixtures nobody could
tell the check had stopped working.

Fixtures required:

| Fixture | Asserts |
|---|---|
| Empty flow file | Publishes nothing new; previous file untouched |
| Malformed JSON line among good lines | Line skipped and counted; good lines still attributed |
| Positive control present | `positive_control` = 1 |
| Positive control absent | `positive_control` = 0, and no false alert on cycle 1 |
| 25 unmapped clients | Top 20 emitted, remainder aggregated, count reads 25 |
| Both directions | Egress and ingress attributed to the correct LAN endpoint |
| Both endpoints in LAN | Skipped with the right reason |
| Neither endpoint in LAN | Skipped with the right reason |
| Restart with existing state | Counters resume rather than reset |
| Corrupt state file | Offset resets, run completes, reason flagged |
| File shrank below offset | Treated as rotation, not as EOF |

### Live verification (`make verify`)

Assert `homelab_netflow_positive_control` equals 1 and that last-flow age is
under threshold, read from the host's textfile output — not from the vault.
`verify.yml` runs both with and without a vault, and a vault-dependent assert
has already broken the nightly runner once.

### Deploy proof

Per the repo workflow: commit first, confirm `git status --porcelain` is empty,
then `make infra`. Expect the documented `changed=3` runner-sync floor, run
again, and require `changed=0` from the second run — checking *which* tasks
changed rather than quoting the second number.

Then the application check, because a container that is `up` proves only that
the process started: read a real device's byte total off the dashboard and
confirm it moves when that device is actually used.

## Risks

**NetFlow over UDP is unauthenticated and trivially spoofable.** Any host on the
LAN can send fabricated flow records to port 2055 and inject arbitrary
attribution into the dashboard. The firewalld source restriction to pfSense's
address is the only mitigation, which makes it load-bearing rather than
housekeeping. It is worth being clear-eyed that this data is advisory, not
evidentiary — which is consistent with forensic use having been ruled out.

**The softflowd package may not exist.** Blocking step-0 check; see Component 1.

**The retention increase consumes disk.** Checked before, not after.

**The `exec:` template change has estate-wide blast radius.** Every `infra_apps`
service renders through that template. A `changed=0` proof across the whole VM
is required before it is trusted.

**Rootless podman UDP publishing.** The container publishes on the host's LAN
address, not on localhost — consistent with the known behaviour where
`curl localhost:3005` misleads about Grafana because pasta binds the LAN IP
only. Any test of the collector must target `192.168.1.32:2055`, not localhost.

## Phases 2 and 3 — sketch only

Recorded so the phase 1 spine makes sense, not designed here.

**Phase 2, destination visibility.** Suricata on pfSense with EVE JSON output
restricted to `dns`, `tls` and `alert` events — deliberately *not* `flow`
events, since phase 1 already counts bytes and raw flow events would dominate
log volume. Events ship to Loki on svc-infra, queried through the existing
Grafana. Suricata rather than Unbound query logging because **DoH and DoT hide
DNS from the resolver**: phones, browsers and some smart TVs resolve over HTTPS
and never touch pfSense's Unbound, so a DNS-log design has a silent and growing
blind spot on exactly the devices most worth auditing. TLS SNI still names the
destination. Encrypted Client Hello erodes even that over time.

Suricata drops packets silently under load. Phase 2 must assert on
`capture.kernel_drops` and treat a quiet sensor as `inconclusive` rather than
clean.

**Phase 3, alerting.** ET Open signatures via Suricata for known-bad, and
behavioral rules over the phase 1 metrics for "unusual for this device" —
sustained upload from a device that has never uploaded, a new destination for a
device with a stable destination set, a sudden fan-out in contacted hosts. Both
route to ntfy. Signature alerting needs a tuning budget; an untuned ruleset
becomes noise and then becomes ignored, which is the same "nobody looks at it"
failure this repo worries about everywhere else.
