# Estate health as Prometheus metrics (Grafana sub-project A)

Date: 2026-08-07
Status: **shipped** 2026-08-09. Implemented on branch `docs/grafana-estate-metrics`.

Read the amendments at the bottom of this file before relying on any detail here:
four things diverged from this design during implementation, and two of them
changed the metric contract.

## The problem

This repo computes almost every number worth charting and then throws it away.

`diskguard.sh.j2` runs `zpool list -H -o name,capacity`, compares it to a
threshold, and discards the percentage. `pve-health.sh.j2` does the same for
pool capacity, `df / /var/lib/vz` and scrub age. The nightly scan reduces
per-image vulnerability counts to one ntfy line. `release-check` reduces 48
images to a summary. The drift check counts running containers, Quadlet units,
drifted and orphaned, and prints prose.

The result is comprehensive **threshold alerting** and essentially no
**trending**. That is the gap that turned the 2026-07-20 rpool fill into an
outage rather than a slope somebody noticed a week earlier: nothing was wrong
until it was 100% wrong, because no series existed to look at.

Grafana is already deployed on svc-infra:3005 with a file-provisioned
Prometheus datasource (`uid: prometheus`) and a dashboard provisioning path at
`roles/svc_infra/files/grafana-dashboards/`. One dashboard lives there,
`homelab-nodes.json`, over node_exporter host metrics. The missing piece is not
Grafana. It is that Prometheus scrapes only itself and node_exporter on three
VMs, so host CPU/memory/disk/network is the entire metric surface that exists.

## Scope

Three domains were identified as worth charting: per-container health,
hypervisor/ZFS capacity, and the nightly reports. They are three separate
collection problems, so they are three sub-projects:

| | Sub-project | Delivers | Risk |
|---|---|---|---|
| **A** | *This spec.* Textfile bridge + report metrics on svc-infra | CVE counts, images-behind, drift counts as trends | Low |
| **B** | PVE/ZFS capacity + NFS free space on thurgadin | Pool fill trend, scrub age, `/var/lib/vz`, restored NFS space | Medium |
| **C** | Per-container metrics on all three VMs | Per-container CPU/memory/restarts | Medium-high |

A comes first because B and C both reuse its mechanism and its conventions. B
is second because it is the highest-value single chart in the estate. C is last:
most moving parts, least decision-support.

**In scope for A:** the collection bridge, three emitters (nightly CVE scan,
weekly release check, container drift), one dashboard, and the validation gates.

**Explicitly out of scope for A:**

- The credential canary. Its three tri-state probes were considered and
  dropped — the weakest signal of the four, and it has no positive control to
  begin with (see the long note in `CLAUDE.md`).
- Sub-projects B and C.
- Grafana alerting. ntfy remains the only alert path. Adding a second one
  means two things to keep working and two places for a threshold to disagree.
- Enabling the textfile collector on svc-download and svc-media. All three
  emitters run on svc-infra, so only svc-infra needs it. This leaves the three
  near-identical `node-exporter.container.j2` templates divergent, which is
  accepted; C unifies them when it needs to.

## Two constraints that shaped the design

**The `changed=0` invariant.** A metrics file changes on every run by
definition. If an Ansible `template` or `copy` task in `roles/svc_infra` writes
it, every `make infra` reports `changed`, and "a deploy that reports
`changed=0` against a clean tree is proof that what is running equals what is
committed" stops being true. So the writes live in `scan.yml`, `release.yml`
and the verify path — plays, not the deploy role — and they are `command` tasks
with `changed_when: false`.

**The chart and the alert must not disagree.** Each report already reduces its
numbers to an ntfy message. If a chart derives the same number by a second
independent parse of `state.json`, the two can drift and there is no way to
know which is lying.

### Approaches rejected

- **Ansible renders the `.prom` directly** from the same fact that feeds ntfy.
  Fewest moving parts and the numbers provably match, but it puts file-diff
  churn inside plays whose `changed` counts are read, and it walks into
  SELinux context restoration (Ansible resets context on write).
- **A standalone collector script on its own timer**, reading `state.json` and
  `release-state.json` independently. Cleanly decoupled and could refresh more
  often, but it is a *second* reader of the same state, which is exactly the
  divergence risk above, and it duplicates parsing that already exists.

### Chosen: plays emit through a shared helper

`roles/svc_infra` installs the helper; the plays call it with numbers they
already hold. The numbers stay tied to the fact that feeds the alert, the
mechanical concerns are solved once in a testable place, and `command` tasks
keep deploy `changed` counts honest. It matches how `release_check_bin`
(`/usr/local/sbin/homelab-release-check.py`) already ships.

## The bridge

Two lines in `roles/svc_infra/templates/node-exporter.container.j2`:

```
Volume=/opt/homelab/appdata/node-exporter-textfile:/var/lib/node_exporter/textfile:ro
Exec=… --collector.textfile.directory=/var/lib/node_exporter/textfile
```

Plain `:ro`, not `:Z`. The unit already sets `SecurityLabelDisable=true` so the
node and filesystem collectors can read host `/proc` metadata, so label
separation is off for this container and the textfile mount follows the same
convention `/proc`, `/sys` and `/rootfs` use. This is why the SELinux
context-restoration hazard does not apply here — worth recording, because it
would apply on a host whose exporter did not already disable labelling.

The directory is created by `roles/svc_infra` under the existing appdata
convention, root-owned, with files at mode `0644`. Container UID 0 maps to the
unprivileged `homelab` account in this rootless unit, and world-readable is
sufficient. Nothing inside the container ever writes here, so there is no
`podman unshare chown` and no subuid ownership problem.

## The helper

`/usr/local/sbin/homelab-metric-write`, installed by `roles/svc_infra`. Reads
metric lines on stdin:

```bash
homelab-metric-write --dir /opt/homelab/appdata/node-exporter-textfile \
    --file scan --prefix homelab_scan --success <<'EOF'
homelab_scan_images_total 48
homelab_scan_vulnerabilities{severity="critical"} 12
EOF
```

Arguments:

- `--dir <path>` — the textfile collector directory to publish into. Required;
  there is no default, so a caller that omits it exits 1.
- `--file <basename>` — the `.prom` file to publish, e.g. `drift-svc-media`.
- `--prefix <name>` — metric prefix for the timestamp series it appends.
  Separate from `--file` because file basenames may contain hyphens and metric
  names may not.
- `--labels <k="v",…>` — applied to the appended timestamp series, so those
  carry the same dimensions as the findings. Callers label their own lines.
- `--success` — also bump the last-success timestamp.

It owns four guarantees so no caller has to:

1. **Atomic publish.** Write `<file>.prom.tmp`, then `rename()`. A
   half-written file is never scraped.
2. **Timestamps.** Appends `<prefix>_run_timestamp_seconds` on every
   invocation, and `<prefix>_last_success_timestamp_seconds` only with
   `--success`.
3. **Line validation.** Each line must match a `name{labels} value` shape.
   Malformed input is refused.
4. **Refuses to publish nothing.** Zero valid input lines leaves the previous
   file untouched and exits non-zero. This is the important one: a broken
   caller must not replace real numbers with zeros. Keeping the old file makes
   the failure surface as staleness, which is detectable; publishing zeros
   looks like good news.

## The metric contract

All series are prefixed `homelab_`. Every emitter publishes three kinds:

- **The finding** — what you want to chart.
- **A denominator that cannot legitimately be zero** if the emitter really
  looked: `homelab_scan_images_total` (48), `homelab_release_images_comparable`
  (30 at the time of writing, and rising as pins gain a recorded tag — the
  identity that matters is comparable + unmeasured == total), and
  `homelab_drift_containers_running`, which is per-VM and so is
  non-zero per `vm` label rather than any single fleet number — the three sum
  to the 54 the drift check audits. Zero on any of them means *could not
  look*, and the dashboard renders it that way rather than as green.
- **The timestamp pair.** `run_timestamp` advances whenever the emitter
  executes; `last_success_timestamp` only when it measured something. The two
  diverging is the machine-readable form of "it ran and could not look" — the
  distinction this repo has had to relearn four separate times.

Drift metrics carry `vm="svc-media"` and **not** `instance`. All files are
published by svc-infra's exporter, so Prometheus stamps `instance="svc-infra"`
on everything here; reusing `instance` would overwrite that and make the series
lie about its origin.

## The emitters

### Nightly CVE scan — `roles/svc_infra/tasks/scan.yml`

One `command` task after the existing `Summarise the image scan`, reading the
`infra_image_scan` fact. `--success` is passed when `infra_image_scan.ok`,
which that fact already defines as *ran and scanned > 0* — the positive control
is inherited rather than reinvented.

Series: `homelab_scan_vulnerabilities{severity}`,
`homelab_scan_images_total`, `homelab_scan_images_scanned`,
`homelab_scan_images_failed`, `homelab_scan_images_eosl`, and per-image
`homelab_scan_image_vulnerabilities{image,digest,severity}` (see amendment 1).

Per-image labels use the ref **with the digest stripped**
(`ghcr.io/owner/name`). A digest in the label means every `image-bump` retires
two series and creates two new ones, so history restarts on exactly the day you
want to see a bump's effect.

### Weekly release check — `release.yml`

Same shape, after the existing summary facts:
`homelab_release_images_behind`, `homelab_release_images_comparable`,
`homelab_release_images_total` (48). Charting comparable against total makes
coverage visibly accrue as pins gain a `# tag:`, and keeps *unmeasured* legible
as its own quantity instead of folded into "fine".

### Container drift — `roles/service_vm/tasks/container-drift.yml`

Three changes:

1. **A machine-readable summary line.** `container-drift.sh.j2` already
   computes `checked`, `quadlet_files`, `drift` and `orphan` and formats them
   into prose. Append one stable line — `drift_metrics checked=27 units=30
   drifted=0 orphan=0` — parsed from `stdout`. One run serves both the assert
   and the metrics; running the script twice risks the two runs seeing
   different state.

2. **Emit before asserting.** `failed_when: rc != 0` currently sits on the
   `command` task, so the play stops the moment drift is found and a metric
   write placed after it would never execute — the chart would go blind exactly
   when it matters. Drop `failed_when` from the command, emit, then a separate
   `assert` fails on the same rc. Strictness is unchanged; only the telemetry
   escapes first.

3. **One file per VM.** The check runs on all three VMs but only svc-infra has
   the directory, so svc-media and svc-download `delegate_to: svc-infra`.
   Files are `drift-svc-infra.prom`, `drift-svc-media.prom`,
   `drift-svc-download.prom`. Three VMs writing one shared `drift.prom` would
   each clobber the others' series, charting whichever host ran last.

### Two accepted tradeoffs

**Metric writes are non-fatal.** A failed `homelab-metric-write` warns but does
not fail scan, release or verify. Telemetry must not be able to break the
checks it describes, and ntfy alerting is untouched, so a broken emitter never
costs an alert. The cost is that a *persistently* broken emitter is caught only
by the dashboard's freshness row, not by a gate. This is the weakest link in
the design.

**Partial verifies produce legitimate staleness.** `make dl` and `make media`
verify one VM, so the other VMs' drift series go stale between full runs. The
freshness threshold must tolerate that: keyed to the nightly full verify, not
to the last partial one.

Thresholds are therefore **per emitter, not global**, because these run on
different schedules and one number cannot serve both:

| Emitter | Schedule | Stale after |
|---|---|---|
| Scan | nightly | 26h |
| Drift | nightly full verify | 26h |
| Release | weekly, Mon 08:30 | 8d |

A single 26h threshold would show the release series as permanently stale,
which is how a freshness panel becomes something nobody looks at.

## The dashboard

`roles/svc_infra/files/grafana-dashboards/homelab-estate.json`, uid
`homelab-estate`. Separate from `homelab-nodes` — that one is host metrics,
this is estate health. It drops into the existing provisioning path and
appears on restart; no UI import. `allowUiUpdates: false` already means the
file is the source of truth and UI edits revert.

Rows:

- **Now** — stat tiles: critical CVEs, high CVEs, images behind, containers
  drifted, containers with no unit, images that failed to scan.
- **Trend** — CVE counts by severity, images behind, container count over
  time. The slope, not the number, is the point of the whole sub-project.
- **Coverage** — comparable vs total (27/48), scanned vs total.
- **Freshness** — `time() - last_success_timestamp` per emitter, per VM for
  drift, with thresholds.
- **Worst offenders** — table of per-image critical counts, sorted descending.

Two settings that matter more than they look:

- **Default time range 30 days.** Grafana's 6h default would render an empty
  dashboard for nightly and weekly series, and the obvious conclusion would be
  that the whole thing is broken.
- **`noValue` set on every stat tile**, so a missing series reads as "no data"
  rather than `0`. Grafana renders an absent series as zero by default, which
  would turn a dead emitter into a green tile reading "0 critical CVEs" —
  precisely the failure this design exists to prevent.

## Validation gates

Added to `make validate`:

- **Dashboard JSON parses, has a uid, and every panel's datasource uid is
  `prometheus`.** A typo fails at Grafana boot; `grafana-datasource.yml.j2`'s
  own header records that a datasource uid mismatch already took the container
  down once.
- **Every metric name queried by the dashboard is one an emitter writes**,
  checked against a declared list. This is the gate most worth having: rename a
  metric in a script and the panel goes quietly blank, which nothing else
  catches.
- **Fixture tests for the helper**: empty stdin refuses and leaves the previous
  file intact, malformed lines are rejected, no temp file is left behind. Same
  approach as the drift check's cannot-look paths, which are fixture-tested
  because a live run always says OK.
- **`tests/validate_scan_readonly.py`** gains the helper and the new scan-path
  tasks *inside* its coverage rather than around it. Nothing here pulls or
  upgrades, but the gate should be the thing that confirms it.

The exporter template change is a Quadlet, so CI's `systemd-analyze verify`
covers it — the one check a macOS workstation cannot run.

## Verifying it works

A container that is up and a unit that is active prove nothing here. After
deploy:

1. `make infra` twice; expect `changed=0` on the second run (the first reports
   the runner's three-task checkout sync).
2. Run `make scan`, the release report, and `make verify` once. Until an
   emitter runs the directory is empty and the dashboard is legitimately blank.
3. `curl svc-infra:9100/metrics | grep homelab_` — the series must be present
   on the host Prometheus actually scrapes.
4. Query Prometheus for one series, confirming it is stored, not merely
   exposed.
5. Load the dashboard and confirm panels have data.
6. **Break it deliberately once.** Hand the helper empty stdin; confirm the
   previous file survives and freshness begins climbing. A healthy run never
   exercises the could-not-look path, so this is the only way to know it works.

## Follow-ups this spec creates

- Sub-project B (PVE/ZFS capacity) gets its own spec and reuses this
  mechanism. It additionally restores NFS free space, which was deliberately
  removed from node_exporter because the in-scrape collector hung in D-state
  and took the whole scrape down; a textfile script with a timeout cannot do
  that, because a hang leaves a stale file rather than a dead scrape.
- Sub-project C (per-container metrics) gets its own spec and decides between
  `prometheus-podman-exporter` and a coarser textfile script.
- The three `node-exporter.container.j2` templates are divergent until C
  unifies them.

## Amendments — what diverged during implementation

Four things in the design above turned out to be wrong. Each was found by a check
with a required positive result, and none was caught by `make validate`, by a
successful deploy, or by `node_textfile_scrape_error` — which is the argument this
design makes about the estate, holding against the design itself. They are
recorded here rather than silently corrected upstream, because the reasoning is
the useful part.

**1. Per-image series needed a `digest` label as well as a stripped `image`.**

The design said to strip the digest so series would not churn across
`make image-bump`. That is necessary but not sufficient: `apps.yml` deliberately
pins one repo at more than one digest (three valkey services share one pin, a
fourth uses another), so stripping collapsed two *different* pins onto one label
set. `scan.prom` then held two lines reading
`homelab_scan_image_vulnerabilities{image="docker.io/valkey/valkey",severity="critical"}`,
node_exporter published one and discarded the other, and
`node_textfile_scrape_error` stayed `0` throughout. Shipped contract is
`{image, digest, severity}`, digest being the first 7 characters. Nothing groups
on `digest`, so trends stay continuous; a per-repo total needs an explicit
`sum by (image)`.

**2. "last_success does not advance" required the publisher to carry it forward.**

The design's central mechanism — `run_timestamp` advances every run,
`last_success_timestamp` only when something was measured, and the two diverging
is the machine-readable form of "it ran and could not look" — did not work as
written. The publisher rewrites the whole file rather than merging, so omitting
the success stamp did not freeze the old one, it **deleted** it. The freshness
tile then read "no data" instead of climbing past its threshold, so the red state
the design relies on could never fire. The publisher now preserves any existing
`<prefix>_last_success_timestamp_seconds` line verbatim when `--success` is
absent.

**3. Emitters must suppress findings they did not measure, not just withhold
`--success`.**

With an empty result set the scan emitter rendered six *valid* lines of zeros —
and a zero vulnerability count is indistinguishable from a healthy estate. The
design assumed the non-zero denominator would carry the signal, which is true in
the data and false on a dashboard, because no tile charted it. Findings are now
gated on the same `ok` condition that gates `--success`, so they go absent and
`noValue` renders "no data". Denominators and the run timestamp still publish
unconditionally — those are the could-not-look evidence.

**4. The drift emitter's guard tests for `None`, not for an empty list.**

`regex_search` returns Python `None` on a no-match, and `| default([])`
substitutes only for *Undefined*. `None | length` therefore raised on exactly the
cannot-look path the guard existed to handle, and the play never reached the
assert — so the operator got an opaque Jinja error instead of the script's own
account of why it could not look. The test is now
`is not none and … | length == 4`, hoisted into one fact that both consumers read
so a fix cannot be applied to one site and missed at the other.

### Smaller corrections

- The publisher's exit codes are `0` published, `1` bad arguments or I/O failure,
  `2` malformed input lines, `3` no input lines. argparse's own usage errors are
  remapped from its default `2` to `1`, because `2` is reserved for malformed
  input and a caller's bad flag must not look like bad data.
- `--dir` is required and has no default. The design's example omitted it.
- A third gate shipped that this design did not anticipate:
  `tests/validate_grafana_dashboards.py`, which cross-checks every metric name a
  panel queries against the names the emitters actually write. A renamed metric
  otherwise leaves a panel blank and reports nothing anywhere.
- `tests/validate_verify_safety.py` gained `container-drift.yml` and an assertion
  that the drift rc gate stays unconditional and stays positioned after the
  metrics publish. Moving the `failed_when` onto a separate `assert` made the gate
  deletable without any gate noticing, which is the failure mode this repo has
  written down more than once.
