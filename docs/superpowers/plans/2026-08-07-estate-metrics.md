# Estate Health Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the nightly scan, weekly release check and container drift results as Prometheus series via node_exporter's textfile collector on svc-infra, and chart them in a provisioned Grafana dashboard.

**Architecture:** `roles/svc_infra` enables node_exporter's textfile collector and installs one helper script, `homelab-metric-write`, which owns atomic publishing, sample validation and run/success timestamps. The three existing report plays call it with numbers they already hold, so a chart can never disagree with the ntfy message. A dashboard JSON drops into the existing provisioning path. Two new offline gates catch the failure mode that matters: a metric renamed in a script while a panel still queries the old name.

**Tech Stack:** Ansible (ansible-core in `.venv`), Python 3 (helper + validators, stdlib only), rootless podman Quadlets, Prometheus, Grafana file provisioning, bash (drift script).

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-07-grafana-estate-metrics-design.md`. Read it before Task 1.
- **Branch:** stay on `docs/grafana-estate-metrics`, which already carries the spec at `9a5fcaf`. Do not open a second branch; the spec and its implementation merge together.
- **Never `git add -A`.** Stage explicit paths. The repo root holds working notes quoting live credentials.
- **Never echo vault secrets.** Nothing in this plan touches the vault; no task here needs `no_log`.
- **`make validate` before every commit.** It is the gate that fires first; CI runs only after merge to `main`.
- **Metric writes must never fail their play.** Every emitter task carries `failed_when: false` and `changed_when: false`.
- **Metric writes must never live in `roles/svc_infra/tasks/files.yml`.** A file that changes every run would make every `make infra` report `changed`, destroying the `changed=0` proof. Writes belong in `scan.yml`, `release.yml` and `container-drift.yml`.
- **Textfile dir:** `/opt/homelab/appdata/node-exporter-textfile`, root-owned, files mode `0644`.
- **Helper path:** `/usr/local/sbin/homelab-metric-write`.
- **Metric prefix:** `homelab_`. Emit **no `# HELP` or `# TYPE` lines** — the three drift files share timestamp metric names and duplicate TYPE declarations across merged textfiles make node_exporter reject them.
- **Drift label is `vm=`, never `instance=`.** All files are published by svc-infra's exporter, so Prometheus stamps `instance="svc-infra"` on everything.
- **Freshness thresholds:** scan 26h (`93600`), drift 26h (`93600`), release 8d (`691200`).
- **Python:** stdlib only. `$(PYTHON)` in the Makefile is the `.venv` interpreter.

## File Structure

**Create:**

| Path | Responsibility |
|---|---|
| `roles/svc_infra/files/homelab-metric-write` | The helper. Plain file, not a `.j2` — it needs no interpolation, so the fixture test executes the real artifact byte-for-byte instead of a re-rendered approximation. |
| `tests/validate_metric_write.py` | Exercises the helper's refusal paths, which a live run never reaches. |
| `tests/validate_grafana_dashboards.py` | Dashboard JSON parses, datasource uid is right, and every queried metric name is one an emitter actually writes. |
| `roles/svc_infra/files/grafana-dashboards/homelab-estate.json` | The dashboard. |
| `docs/superpowers/plans/2026-08-07-estate-metrics.md` | This plan. |

**Modify:**

| Path | Change |
|---|---|
| `inventory/group_vars/all/main.yml` | Add `infra_textfile_dir`, `infra_metric_write_bin`. Must be group_vars, not role defaults — `scan.yml` and `release.yml` do not apply the role. |
| `roles/svc_infra/templates/node-exporter.container.j2` | Volume mount + `--collector.textfile.directory`. |
| `roles/svc_infra/tasks/files.yml` | Create the directory, install the helper. No restart-list edit needed: `infra_node_exporter_template` already feeds it at line 1024. |
| `roles/svc_infra/tasks/scan.yml` | Emit scan metrics after `Summarise the image scan`. |
| `release.yml` | Emit release metrics after `Summarise the run`. |
| `roles/service_vm/templates/container-drift.sh.j2` | One machine-readable summary line, after the cannot-look guards. |
| `roles/service_vm/tasks/container-drift.yml` | Emit-before-assert; per-VM file delegated to svc-infra. |
| `tests/validate_container_drift.py` | Assert the metrics line is present for clean/drifted and absent for cannot-look. |
| `tests/validate_scan_readonly.py` | Add the helper to `SCAN_PATHS`. |
| `Makefile` | Wire the two new validators. |
| `CLAUDE.md`, `docs/services.md` | Record the bridge and the dashboard. |

---

### Task 1: The helper script and its refusal paths

**Files:**
- Create: `roles/svc_infra/files/homelab-metric-write`
- Create: `tests/validate_metric_write.py`
- Modify: `Makefile` (add to `validate-shell`)

**Interfaces:**
- Consumes: nothing. This is the foundation task.
- Produces: the CLI every emitter uses —
  `homelab-metric-write --dir DIR --file BASENAME --prefix METRIC_PREFIX [--labels 'k="v",k2="v2"'] [--success]`, samples on stdin.
  Exit codes: `0` published, `1` bad arguments or I/O failure, `2` malformed input lines, `3` no input lines (previous file left intact).
  Writes `DIR/BASENAME.prom` and appends `PREFIX_run_timestamp_seconds` always, `PREFIX_last_success_timestamp_seconds` only with `--success`.

- [ ] **Step 1: Write the failing test**

Create `tests/validate_metric_write.py`:

```python
#!/usr/bin/env python3
"""Exercise homelab-metric-write against fixtures, including every refusal.

The helper publishes the .prom files that carry this estate's scan, release and
drift numbers into node_exporter. On a healthy host it succeeds every time, so
left alone it would be another check nobody could tell had stopped working.

The refusals are what matter, and one of them is the whole reason the helper
exists rather than an Ansible `template` task:

  empty stdin     leave the PREVIOUS file alone and exit 3. A broken caller
                  must not replace real numbers with zeros, because an old
                  number shows up as staleness and a zero shows up as good news.
  malformed line  write nothing at all and exit 2. node_exporter rejects a
                  whole file for one bad line, so a partial write would take
                  the good metrics down with the bad one.
  bad --labels    write nothing and exit 1, rather than emit a timestamp
                  series that silently fails to parse on the host.

Also asserted: no HELP/TYPE lines (the three drift files share timestamp metric
names, and duplicate TYPE across merged textfiles makes node_exporter reject
them and raise node_textfile_scrape_error), and no .prom.tmp left behind on any
path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "roles/svc_infra/files/homelab-metric-write"

SAMPLE_IN = 'homelab_scan_images_total 48\nhomelab_scan_vulnerabilities{severity="critical"} 12\n'

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        failures.append(f"{label}: {detail}" if detail else label)


def run(tmpdir: str, args: list[str], stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HELPER), "--dir", tmpdir, *args],
        input=stdin, capture_output=True, text=True, check=False,
    )


def test_publishes_valid_input() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proc = run(tmp, ["--file", "scan", "--prefix", "homelab_scan"], SAMPLE_IN)
        out = Path(tmp, "scan.prom")
        check("valid input exits 0", proc.returncode == 0, proc.stderr)
        check("valid input writes the file", out.exists())
        body = out.read_text(encoding="utf-8")
        check("sample preserved", "homelab_scan_images_total 48" in body)
        check("run timestamp appended", "homelab_scan_run_timestamp_seconds " in body)
        check("no success timestamp without --success",
              "homelab_scan_last_success_timestamp_seconds" not in body)
        check("no HELP/TYPE emitted",
              not any(l.startswith("#") for l in body.splitlines()), body)
        check("mode is 0644", oct(out.stat().st_mode & 0o777) == "0o644")
        check("no tmp file left", not list(Path(tmp).glob("*.tmp")))


def test_success_flag_adds_success_timestamp() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run(tmp, ["--file", "scan", "--prefix", "homelab_scan", "--success"], SAMPLE_IN)
        body = Path(tmp, "scan.prom").read_text(encoding="utf-8")
        check("--success adds the success timestamp",
              "homelab_scan_last_success_timestamp_seconds " in body)


def test_labels_land_on_timestamp_series() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run(tmp, ["--file", "drift-svc-media", "--prefix", "homelab_drift",
                  "--labels", 'vm="svc-media"', "--success"],
            'homelab_drift_containers_running{vm="svc-media"} 14\n')
        body = Path(tmp, "drift-svc-media.prom").read_text(encoding="utf-8")
        check("labels applied to run timestamp",
              'homelab_drift_run_timestamp_seconds{vm="svc-media"} ' in body, body)
        check("labels applied to success timestamp",
              'homelab_drift_last_success_timestamp_seconds{vm="svc-media"} ' in body, body)


def test_empty_stdin_preserves_previous_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp, "scan.prom")
        run(tmp, ["--file", "scan", "--prefix", "homelab_scan"], SAMPLE_IN)
        before = out.read_bytes()
        proc = run(tmp, ["--file", "scan", "--prefix", "homelab_scan"], "")
        check("empty stdin exits 3", proc.returncode == 3, f"rc={proc.returncode}")
        check("empty stdin leaves the old file byte-identical", out.read_bytes() == before)
        check("empty stdin leaves no tmp file", not list(Path(tmp).glob("*.tmp")))


def test_malformed_line_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp, "scan.prom")
        run(tmp, ["--file", "scan", "--prefix", "homelab_scan"], SAMPLE_IN)
        before = out.read_bytes()
        proc = run(tmp, ["--file", "scan", "--prefix", "homelab_scan"],
                   "homelab_scan_images_total 48\nthis is not a metric\n")
        check("malformed input exits 2", proc.returncode == 2, f"rc={proc.returncode}")
        check("malformed input leaves the old file untouched", out.read_bytes() == before)
        check("malformed input leaves no tmp file", not list(Path(tmp).glob("*.tmp")))


def test_bad_labels_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proc = run(tmp, ["--file", "scan", "--prefix", "homelab_scan",
                         "--labels", "vm=svc-media"], SAMPLE_IN)
        check("unquoted label value exits 1", proc.returncode == 1, f"rc={proc.returncode}")
        check("bad labels write no file", not Path(tmp, "scan.prom").exists())


def test_bad_prefix_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proc = run(tmp, ["--file", "scan", "--prefix", "not-a-valid-prefix"], SAMPLE_IN)
        check("hyphenated prefix exits 1", proc.returncode == 1, f"rc={proc.returncode}")
        check("bad prefix writes no file", not Path(tmp, "scan.prom").exists())


def main() -> int:
    if not HELPER.exists():
        print(f"missing {HELPER}", file=sys.stderr)
        return 1
    for fn in (
        test_publishes_valid_input,
        test_success_flag_adds_success_timestamp,
        test_labels_land_on_timestamp_series,
        test_empty_stdin_preserves_previous_file,
        test_malformed_line_writes_nothing,
        test_bad_labels_rejected,
        test_bad_prefix_rejected,
    ):
        fn()
    if failures:
        print("homelab-metric-write FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("homelab-metric-write: 7 case(s) OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python tests/validate_metric_write.py`
Expected: FAIL — `missing …/roles/svc_infra/files/homelab-metric-write`, exit 1.

- [ ] **Step 3: Write the helper**

Create `roles/svc_infra/files/homelab-metric-write`:

```python
#!/usr/bin/env python3
"""Publish a Prometheus textfile-collector metric file, atomically and safely.

The callers — scan.yml, release.yml and container-drift.yml — already hold the
numbers they want charted. This owns everything mechanical about getting them
onto disk where node_exporter's textfile collector will scrape them, so that
three plays do not each solve it slightly differently.

Four guarantees, each because the alternative is a chart that lies:

  atomic       write to .tmp then rename(), so a half-written file is never
               scraped.
  validated    every input line must parse as a Prometheus sample. node_exporter
               rejects a WHOLE file for one bad line, so a partial write would
               take the good metrics down with the bad one.
  timestamped  run and last-success are separate series, so "it ran and could
               not look" is distinguishable from "it measured a zero".
  non-empty    zero input lines leaves the previous file alone. A broken caller
               must not replace real numbers with zeros: an old number shows up
               as staleness, a zero shows up as good news.

No HELP or TYPE lines are emitted, deliberately. The three drift files share the
metric names homelab_drift_run_timestamp_seconds and
homelab_drift_last_success_timestamp_seconds, and node_exporter merges every
.prom in the directory — duplicate TYPE declarations across those files make it
reject them and raise node_textfile_scrape_error. Untyped samples are read as
untyped gauges, which is what all of these are.

Installed by roles/svc_infra as a plain file rather than a .j2 template: it
interpolates nothing, and keeping it out of Jinja means tests/validate_metric_write.py
executes the real artifact rather than a re-rendered approximation of it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

# A Prometheus sample: name, optional labels, value. Deliberately strict —
# anything this does not match is something node_exporter would reject, and
# failing here with the caller's file name in the message beats discovering it
# as a silently absent series.
LABEL = r'[a-zA-Z_][a-zA-Z0-9_]*="[^"\\\n]*"'
SAMPLE = re.compile(
    r"^[a-zA-Z_:][a-zA-Z0-9_:]*"
    rf"(?:\{{{LABEL}(?:\s*,\s*{LABEL})*\}})?"
    r"\s+[+-]?(?:\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|Inf|NaN)$"
)
METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
FILE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MALFORMED = 2
EXIT_EMPTY = 3


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish metric samples to a node_exporter textfile directory.",
    )
    parser.add_argument("--dir", required=True,
                        help="textfile collector directory")
    parser.add_argument("--file", required=True,
                        help="basename, without .prom (e.g. drift-svc-media)")
    parser.add_argument("--prefix", required=True,
                        help="metric prefix for the appended timestamp series")
    parser.add_argument("--labels", default="",
                        help='labels for the timestamp series, e.g. vm="svc-media"')
    parser.add_argument("--success", action="store_true",
                        help="also bump the last-success timestamp")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    # --prefix and --file are separate arguments because a file basename may
    # contain hyphens (drift-svc-media) and a metric name may not.
    if not METRIC_NAME.match(args.prefix):
        print(f"--prefix {args.prefix!r} is not a valid metric name", file=sys.stderr)
        return EXIT_ERROR
    if not FILE_NAME.match(args.file):
        print(f"--file {args.file!r} may only contain [A-Za-z0-9_.-]", file=sys.stderr)
        return EXIT_ERROR

    samples: list[str] = []
    malformed: list[str] = []
    for raw in sys.stdin.read().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        (samples if SAMPLE.match(line) else malformed).append(line)

    if malformed:
        print(f"{args.file}: {len(malformed)} malformed metric line(s); "
              "nothing written, previous file left in place:", file=sys.stderr)
        for line in malformed[:5]:
            print(f"  {line}", file=sys.stderr)
        return EXIT_MALFORMED

    if not samples:
        print(f"{args.file}: no metric lines on stdin; leaving the previous file "
              "in place. A stale file is detectable, a file of zeros is not.",
              file=sys.stderr)
        return EXIT_EMPTY

    suffix = f"{{{args.labels}}}" if args.labels else ""
    now = int(time.time())
    stamps = [f"{args.prefix}_run_timestamp_seconds{suffix} {now}"]
    if args.success:
        stamps.append(f"{args.prefix}_last_success_timestamp_seconds{suffix} {now}")

    # Validate what we generated, not just what we were handed. A malformed
    # --labels would otherwise produce a file node_exporter rejects wholesale,
    # taking the caller's good samples down with it.
    for stamp in stamps:
        if not SAMPLE.match(stamp):
            print(f"--labels {args.labels!r} builds an invalid series "
                  f"({stamp!r}); nothing written", file=sys.stderr)
            return EXIT_ERROR
    samples.extend(stamps)

    target = os.path.join(args.dir, f"{args.file}.prom")
    tmp = f"{target}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(samples) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # 0644 and root-owned: container UID 0 maps to the unprivileged homelab
        # account in the rootless exporter unit, and world-readable is all it
        # needs. Nothing in the container ever writes here.
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except OSError as exc:
        print(f"{args.file}: could not publish {target}: {exc}", file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return EXIT_ERROR

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/validate_metric_write.py`
Expected: PASS — `homelab-metric-write: 7 case(s) OK`, exit 0.

- [ ] **Step 5: Wire it into `make validate`**

In `Makefile`, in the `validate-shell` target, immediately after the
`validate_container_drift.py` line and its comment block, add:

```makefile
# Python rather than shell, but it belongs with the drift check above for the
# same reason: it is a script whose refusal paths are the point, and on a
# healthy host it succeeds every time. Its "leave the old file alone rather
# than publish zeros" branch is the one that keeps a broken emitter legible.
	$(PYTHON) tests/validate_metric_write.py
```

- [ ] **Step 6: Run the full gate**

Run: `make validate`
Expected: PASS, including the new line.

- [ ] **Step 7: Commit**

```bash
git add roles/svc_infra/files/homelab-metric-write tests/validate_metric_write.py Makefile
git commit -m "feat: a metric publisher that refuses to write zeros"
```

---

### Task 2: Enable the textfile collector and install the helper

**Files:**
- Modify: `inventory/group_vars/all/main.yml` (after the node_exporter block, ~line 190)
- Modify: `roles/svc_infra/templates/node-exporter.container.j2:25-28`
- Modify: `roles/svc_infra/tasks/files.yml:676-686`

**Interfaces:**
- Consumes: the helper from Task 1.
- Produces: `infra_textfile_dir` = `/opt/homelab/appdata/node-exporter-textfile` and `infra_metric_write_bin` = `/usr/local/sbin/homelab-metric-write`, both usable from any play (they are group_vars, so `scan.yml` and `release.yml` see them without applying the role). The directory exists on svc-infra and node_exporter scrapes `*.prom` from it.

- [ ] **Step 1: Add the two variables**

In `inventory/group_vars/all/main.yml`, directly after the `node_exporter_fs_types_exclude` block, add:

```yaml
# --- estate metrics bridge (Grafana sub-project A) --------------------------
# node_exporter's textfile collector, enabled on svc-infra only. It is how the
# nightly scan, the weekly release check and the container drift check get their
# numbers into Prometheus: each already computes them and previously reduced
# them to one ntfy line, which gave this estate thorough threshold alerting and
# no trending at all.
#
# These live here rather than in roles/svc_infra/defaults/main.yml for the same
# reason scan_report_dir does: scan.yml and release.yml do not apply that role,
# and role defaults are only in scope for a play that does.
#
# Root-owned, files 0644. Container UID 0 maps to the unprivileged homelab
# account in the rootless exporter unit and world-readable is sufficient —
# nothing inside the container ever writes here, so there is no subuid
# ownership problem and no `podman unshare chown`.
infra_textfile_dir: /opt/homelab/appdata/node-exporter-textfile
infra_metric_write_bin: /usr/local/sbin/homelab-metric-write
```

- [ ] **Step 2: Mount the directory and enable the collector**

In `roles/svc_infra/templates/node-exporter.container.j2`, after the
`Volume=/:/rootfs:ro` line, add:

```
# Plain :ro, not :Z. This unit already sets SecurityLabelDisable=true above so
# the node and filesystem collectors can read host /proc metadata, so label
# separation is off for this container and the textfile mount follows the same
# convention /proc, /sys and /rootfs use. On a host whose exporter did NOT
# disable labelling this would need :Z, and an Ansible-written .prom would
# arrive with a default context the container could not read — a silently empty
# chart rather than an error.
Volume={{ infra_textfile_dir }}:/var/lib/node_exporter/textfile:ro
```

and append to the existing `Exec=` line (keep it one line):

```
 --collector.textfile.directory=/var/lib/node_exporter/textfile
```

- [ ] **Step 3: Create the directory and install the helper**

In `roles/svc_infra/tasks/files.yml`, immediately before
`- name: Render the node_exporter Quadlet` (line 678), insert:

```yaml
# The textfile collector's directory and the helper that writes into it. The
# WRITES do not happen here — they happen in scan.yml, release.yml and
# container-drift.yml. A metric file changes on every run by definition, so a
# template task in this role would make every `make infra` report changed and
# the "clean tree deploys to changed=0" proof would stop meaning anything.
- name: Create the node_exporter textfile directory
  ansible.builtin.file:
    path: "{{ infra_textfile_dir }}"
    state: directory
    owner: root
    group: root
    mode: "0755"

- name: Install the metric publisher
  ansible.builtin.copy:
    src: homelab-metric-write
    dest: "{{ infra_metric_write_bin }}"
    owner: root
    group: root
    mode: "0755"
```

- [ ] **Step 4: Validate**

Run: `make validate`
Expected: PASS. `validate_systemd_units.py` parses the changed Quadlet; on macOS it text-matches, and CI will run `systemd-analyze verify` after merge.

- [ ] **Step 5: Deploy to svc-infra and confirm the collector is live**

```bash
make infra
```

Then confirm the exporter restarted and the collector is enabled — a mounted
directory the exporter never read would produce an empty dashboard with no error:

```bash
ssh svc-infra 'systemctl --user --machine=homelab@ status node-exporter --no-pager | head -5'
ssh svc-infra 'curl -s localhost:9100/metrics | grep -c node_textfile'
```

Expected: unit active; the grep returns at least 1 (`node_textfile_scrape_error`
exists once the collector is enabled, even with an empty directory).

- [ ] **Step 6: Commit**

```bash
git add inventory/group_vars/all/main.yml \
        roles/svc_infra/templates/node-exporter.container.j2 \
        roles/svc_infra/tasks/files.yml
git commit -m "feat: enable node_exporter's textfile collector on svc-infra"
```

---

### Task 3: Emit the nightly scan as metrics

**Files:**
- Modify: `roles/svc_infra/tasks/scan.yml` (after `Summarise the image scan`, which ends at line 93)
- Modify: `tests/validate_scan_readonly.py` (`SCAN_PATHS`)

**Interfaces:**
- Consumes: `infra_metric_write_bin`, `infra_textfile_dir` (Task 2); the existing `infra_image_scan` fact, whose keys are `ok`, `images` (list of dicts with `ref`, `ok`, `critical`, `high`, `eosl`), `scanned`, `failed`, `critical`, `high`, `eosl`.
- Produces: `scan.prom` containing `homelab_scan_images_total`, `homelab_scan_images_scanned`, `homelab_scan_images_failed`, `homelab_scan_images_eosl`, `homelab_scan_vulnerabilities{severity}`, `homelab_scan_image_vulnerabilities{image,severity}`, plus `homelab_scan_run_timestamp_seconds` and `homelab_scan_last_success_timestamp_seconds`.

- [ ] **Step 1: Add the emitter**

In `roles/svc_infra/tasks/scan.yml`, between `Summarise the image scan` and
`Mark the infra scan complete`, insert:

```yaml
# Publish the summary as Prometheus series. Deliberately reads the same
# infra_image_scan fact that feeds the ntfy message rather than re-parsing
# state.json, so a chart and an alert cannot disagree about the same night.
#
# --success is gated on infra_image_scan.ok, which that fact already defines as
# "ran AND scanned > 0" — the positive control is inherited rather than
# reinvented here. A run where every image failed to scan therefore bumps the
# run timestamp and not the success timestamp, which is exactly the "it ran and
# could not look" state the two series exist to separate.
#
# Per-image series carry a digest-stripped `image` plus a short `digest`.
#
# `image` is stripped because a full digest in the primary label means every
# `make image-bump` retires two series and creates two new ones, so the history
# would restart on precisely the day you want to see a bump's effect.
#
# `digest` exists because stripping alone is not enough, which the first live run
# proved: two DIFFERENT valkey digests collapsed to one label set, so scan.prom
# held two lines reading `{image="docker.io/valkey/valkey",severity="critical"}`
# and the exporter published one of them and silently dropped the other — with
# node_textfile_scrape_error still 0. A number that is one of two, chosen
# arbitrarily, reporting no error, is the exact failure this whole design exists
# to remove. apps.yml deliberately shares one pin across three valkey services
# and two postgres services (see the BUMP PROCEDURE block), so repos with more
# than one distinct pin are a standing feature of this estate, not an anomaly.
#
# Seven characters, not the whole digest, and no chart groups on it: the
# dashboard sums by `image`, so trends stay continuous across a bump and only
# this extra dimension churns. Shared pins on the SAME digest still appear once.
- name: Publish the image scan as metrics
  ansible.builtin.command:
    # argv as a computed list rather than a literal one, because --success is
    # conditional: appending it inside the expression keeps the flag out of the
    # argument vector entirely on a run that measured nothing, instead of
    # passing it an empty string that argparse would treat as a positional.
    argv: >-
      {{ [infra_metric_write_bin,
          '--dir', infra_textfile_dir,
          '--file', 'scan',
          '--prefix', 'homelab_scan']
         + (['--success'] if infra_image_scan.ok else []) }}
    stdin: |
      homelab_scan_images_total {{ infra_image_scan.images | length }}
      homelab_scan_images_scanned {{ infra_image_scan.scanned }}
      homelab_scan_images_failed {{ infra_image_scan.failed }}
      homelab_scan_images_eosl {{ infra_image_scan.eosl | length }}
      homelab_scan_vulnerabilities{severity="critical"} {{ infra_image_scan.critical }}
      homelab_scan_vulnerabilities{severity="high"} {{ infra_image_scan.high }}
      {% for image in infra_image_scan.images | selectattr('ok') %}
      {% set repo = image.ref | regex_replace('@sha256:.*$', '') %}
      {# '\\1', not '\1': Jinja parses the replacement string as a literal
         before regex_replace ever sees it, and '\1' there is the one-char
         octal escape \x01 (SOH), not a backreference. That shipped once
         already — every digest came out as the same control character, so
         the two valkey pins still collided under an identical, wrong label,
         invisibly, with node_textfile_scrape_error still 0. #}
      {% set digest = image.ref | regex_replace('^.*@sha256:(.{7}).*$', '\\1') %}
      homelab_scan_image_vulnerabilities{image="{{ repo }}",digest="{{ digest }}",severity="critical"} {{ image.critical }}
      homelab_scan_image_vulnerabilities{image="{{ repo }}",digest="{{ digest }}",severity="high"} {{ image.high }}
      {% endfor %}
  register: infra_scan_metrics
  changed_when: false
  # Telemetry must not be able to break the check it describes. A failed publish
  # surfaces as the dashboard's freshness row going red, not as a failed scan.
  failed_when: false

- name: Warn if the scan metrics could not be published
  ansible.builtin.debug:
    msg: >-
      metric publish FAILED (rc={{ infra_scan_metrics.rc | default('none') }}):
      {{ infra_scan_metrics.stderr | default('') }} —
      the estate dashboard will show this emitter as stale
  when: (infra_scan_metrics.rc | default(0)) != 0
```

Replace the awkward first `argv` element with the plain variable — write it as:

```yaml
    argv: >-
      {{ [infra_metric_write_bin,
          '--dir', infra_textfile_dir,
          '--file', 'scan',
          '--prefix', 'homelab_scan']
         + (['--success'] if infra_image_scan.ok else []) }}
```

- [ ] **Step 2: Add the helper to the read-only gate**

In `tests/validate_scan_readonly.py`, add to the `SCAN_PATHS` tuple:

```python
    # Invoked from roles/svc_infra/tasks/scan.yml, so it executes under a scan
    # path and belongs inside this gate rather than beside it. It only ever
    # writes a metrics file, but that is a claim this gate should be the one to
    # confirm.
    "roles/svc_infra/files/homelab-metric-write",
```

- [ ] **Step 3: Validate**

Run: `make validate`
Expected: PASS. `validate_scan_readonly.py` now covers the helper.

- [ ] **Step 4: Run the scan and read the metrics back**

```bash
make scan
ssh svc-infra 'cat /opt/homelab/appdata/node-exporter-textfile/scan.prom'
ssh svc-infra 'curl -s localhost:9100/metrics | grep "^homelab_scan_"'
ssh svc-infra 'curl -s localhost:9100/metrics | grep node_textfile_scrape_error'
```

Expected: the file holds the samples; the exporter republishes them; and
`node_textfile_scrape_error 0`. A non-zero scrape error means node_exporter
rejected the file — read its journal, do not proceed.

- [ ] **Step 5: Commit**

```bash
git add roles/svc_infra/tasks/scan.yml tests/validate_scan_readonly.py
git commit -m "feat: publish the nightly image scan as Prometheus series"
```

---

### Task 4: Emit the weekly release check as metrics

**Files:**
- Modify: `release.yml` (after `Summarise the run`, line 122-131)

**Interfaces:**
- Consumes: `infra_metric_write_bin`, `infra_textfile_dir`; the existing facts `release_ok`, `release_new`, `release_behind`, `release_counts` (keys include `current`, `error`), `release_unmeasured`, and `release.summary.images_examined`.
- Produces: `release.prom` containing `homelab_release_images_total`, `homelab_release_images_comparable`, `homelab_release_images_unmeasured`, `homelab_release_images_behind`, `homelab_release_images_current`, `homelab_release_new`, `homelab_release_errors`, plus the two timestamp series under prefix `homelab_release`.

**Derivation note the implementer must not get wrong:** `comparable` is not a
fact — it is `release.summary.images_examined` minus `release_unmeasured | length`.
`images_examined` counts every pinned image (48); `release_unmeasured` counts
those whose verdict is `error`, `no-feed` or `unknown-version`. The difference is
the 27 the report can actually compare, and charting it against the total is
what makes coverage visible as it accrues.

- [ ] **Step 1: Add the emitter**

In `release.yml`, immediately after the `Compose the one-line summary` task and
before `Write the release report`, insert (matching that block's indentation —
these tasks sit inside a `block:`):

```yaml
    # Same reasoning as scan.yml's emitter: read the facts that feed ntfy, so
    # the chart and the message cannot disagree. --success follows release_ok,
    # which is the report's own "this run measured something" verdict.
    #
    # `comparable` is derived, not a fact: images_examined counts every pinned
    # image, release_unmeasured counts the ones with no usable answer, and the
    # difference is what the report can actually compare. Charting it against
    # the total is what makes coverage legible as it accrues — and keeps
    # "unmeasured" a visible quantity instead of folding it into "fine".
    - name: Publish the release check as metrics
      ansible.builtin.command:
        argv: >-
          {{ [infra_metric_write_bin,
              '--dir', infra_textfile_dir,
              '--file', 'release',
              '--prefix', 'homelab_release']
             + (['--success'] if release_ok else []) }}
        stdin: |
          homelab_release_images_total {{ release.summary.images_examined | default(0) }}
          homelab_release_images_unmeasured {{ release_unmeasured | length }}
          homelab_release_images_comparable {{ (release.summary.images_examined | default(0) | int) - (release_unmeasured | length) }}
          homelab_release_images_behind {{ release_behind | length }}
          homelab_release_images_current {{ release_counts.current | default(0) }}
          homelab_release_new {{ release_new | length }}
          homelab_release_errors {{ release_counts.error | default(0) }}
      register: release_metrics
      changed_when: false
      failed_when: false

    - name: Warn if the release metrics could not be published
      ansible.builtin.debug:
        msg: >-
          metric publish FAILED (rc={{ release_metrics.rc | default('none') }}):
          {{ release_metrics.stderr | default('') }} —
          the estate dashboard will show this emitter as stale
      when: (release_metrics.rc | default(0)) != 0
```

- [ ] **Step 2: Validate**

Run: `make validate`
Expected: PASS.

- [ ] **Step 3: Run the report and read the metrics back**

**Budget warning:** a full release run costs ~45 of the 60 unauthenticated
GitHub requests/hour. Two runs in one hour is one too many — the second reports
`error` for everything. Run this once and inspect the output; do not re-run to
"check again".

```bash
make release-report
ssh svc-infra 'cat /opt/homelab/appdata/node-exporter-textfile/release.prom'
ssh svc-infra 'curl -s localhost:9100/metrics | grep "^homelab_release_"'
```

Expected: `homelab_release_images_total 48`, `homelab_release_images_comparable 27`
(or whatever coverage currently is — the point is that comparable + unmeasured
equals total). Verify that sum by hand; if it does not add up, the derivation is
wrong and the coverage chart would be wrong with it.

- [ ] **Step 4: Commit**

```bash
git add release.yml
git commit -m "feat: publish the weekly release check as Prometheus series"
```

---

### Task 5: Give the drift script a machine-readable summary

**Files:**
- Modify: `roles/service_vm/templates/container-drift.sh.j2:132` (insert after the last cannot-look guard)
- Modify: `tests/validate_container_drift.py` (`CASES` assertions)

**Interfaces:**
- Consumes: nothing new.
- Produces: on stdout, for rc 0 and rc 1 only, exactly one line matching
  `drift_metrics checked=<int> units=<int> drifted=<int> orphan=<int>`. Absent on rc 2.

**Placement matters and is the whole point of this task.** The line goes *after*
the three cannot-look guards and *before* the drift branch, so it prints for a
clean run and for a run that found drift — the two outcomes whose numbers mean
something — and never for a run that could not look. A cannot-look must publish
nothing, so the helper's empty-stdin refusal keeps the previous file and the
freshness panel goes red.

- [ ] **Step 1: Extend the test first**

In `tests/validate_container_drift.py`, extend the `CASES` dict so each case
also asserts on the metrics line. Replace the `CASES` definition with:

```python
# case -> (podman ps output, expected rc, expected substring, expected metrics line)
# The metrics line is consumed by the emitter in container-drift.yml. It must
# appear for a clean run AND for a run that found drift, and must NOT appear for
# any cannot-look: publishing counts nobody could measure is the failure the
# whole metrics design exists to avoid.
CASES = {
    "clean": (f"sonarr|{SONARR}\nsystemd-noname|{OTHER}\n", 0, "OK",
              "drift_metrics checked=2 units=2 drifted=0 orphan=0"),
    "drifted": (f"sonarr|lscr.io/linuxserver/sonarr@sha256:{'d' * 64}\n", 1, "DRIFTED",
                "drift_metrics checked=1 units=2 drifted=1 orphan=0"),
    "orphan": (f"sonarr|{SONARR}\nstranger|{STRANGER}\n", 1, "NO QUADLET",
               "drift_metrics checked=2 units=2 drifted=0 orphan=1"),
    "empty": ("", 2, "CANNOT LOOK", None),
}
```

Then, in the loop that runs each case, after the existing rc and substring
assertions, add:

```python
    expected_metrics = case[3]
    if expected_metrics is None:
        if "drift_metrics" in proc.stdout:
            failures.append(
                f"{name}: printed a drift_metrics line on a cannot-look. "
                "Counts nobody could measure must not be published."
            )
    elif expected_metrics not in proc.stdout:
        failures.append(
            f"{name}: expected {expected_metrics!r} in stdout, got:\n{proc.stdout}"
        )
```

**Note for the implementer:** the existing loop unpacks `CASES` as a 3-tuple.
Update that unpacking to take four values, and read the surrounding code before
editing — the `units=2` counts above assume the fixture writes two Quadlet unit
files. If the fixture writes a different number, use the actual count; the
assertion must match the fixture, not the other way round.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python tests/validate_container_drift.py`
Expected: FAIL — three cases report a missing `drift_metrics` line.

- [ ] **Step 3: Add the line to the script**

In `roles/service_vm/templates/container-drift.sh.j2`, after the
`if (( checked == 0 )); then … fi` block ending at line 132 and before the
`if (( drift > 0 || orphan > 0 ))` block at line 134, insert:

```bash
# Machine-readable summary, parsed by the metric emitter in container-drift.yml.
# Placed AFTER the cannot-look guards above and BEFORE the drift branch below,
# so it prints for a clean run and for a run that found drift — the two
# outcomes whose numbers mean anything — and never for a run that could not
# look. A cannot-look exits before here on purpose: the emitter then sends the
# publisher nothing, the publisher keeps the previous file, and the staleness
# is what shows up. Publishing zeros there would read as an all-clear.
printf 'drift_metrics checked=%d units=%d drifted=%d orphan=%d\n' \
    "$checked" "$quadlet_files" "$drift" "$orphan"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python tests/validate_container_drift.py`
Expected: PASS, all cases.

- [ ] **Step 5: Validate**

Run: `make validate`
Expected: PASS — ShellCheck and `validate_shell_templates.py` also cover this template.

- [ ] **Step 6: Commit**

```bash
git add roles/service_vm/templates/container-drift.sh.j2 tests/validate_container_drift.py
git commit -m "feat: have the drift check report its counts, not just its verdict"
```

---

### Task 6: Emit drift metrics before asserting

**Files:**
- Modify: `roles/service_vm/tasks/container-drift.yml:15-27`

**Interfaces:**
- Consumes: the `drift_metrics` line from Task 5; `infra_metric_write_bin`, `infra_textfile_dir`, and `infra_host` (already defined in group_vars, used by `prometheus.yml.j2`).
- Produces: `drift-<hostname>.prom` on svc-infra for each VM, containing `homelab_drift_containers_running{vm}`, `homelab_drift_containers_drifted{vm}`, `homelab_drift_containers_orphaned{vm}`, `homelab_drift_quadlet_units{vm}`, plus the two timestamp series under prefix `homelab_drift` carrying the same `vm` label.

**Two structural changes, both load-bearing:**

1. `failed_when` moves off the `command` onto a separate `assert`. Today the
   play stops the instant drift is found, so an emitter placed after it would
   never run — the chart would go blind at exactly the moment it matters.
   Strictness is unchanged: the same rc still fails the play, one task later.
2. One file per VM, delegated to svc-infra. The check runs on all three VMs but
   only svc-infra has the textfile directory. Three VMs writing a shared
   `drift.prom` would each clobber the others' series, leaving you charting
   whichever host happened to run last.

- [ ] **Step 1: Restructure the task file**

Replace everything from `- name: Assert no container has drifted from its Quadlet unit`
to the end of `roles/service_vm/tasks/container-drift.yml` with:

```yaml
# rc 1 is drift found, rc 2 is the script refusing to guess. Both must fail the
# play — but NOT here. The assert is a separate task below so the metrics get
# published first: a failed_when on this task would abort the play the moment
# drift appeared, and the drift chart would go blank in the one situation it
# exists for. The rc check itself is unchanged, only later.
- name: Check whether any container has drifted from its Quadlet unit
  ansible.builtin.command: /usr/local/sbin/homelab-container-drift.sh
  register: service_vm_container_drift
  changed_when: false
  failed_when: false

# Only rc 0 and rc 1 print the line — a cannot-look exits before it. So an
# absent line means "could not measure", and the right response is to publish
# nothing at all: the publisher keeps the previous file and the freshness panel
# goes red. Publishing zeros here would render as a clean estate.
- name: Extract the drift counts
  ansible.builtin.set_fact:
    service_vm_drift_counts: >-
      {{ service_vm_container_drift.stdout
         | regex_search('drift_metrics checked=(\d+) units=(\d+) drifted=(\d+) orphan=(\d+)',
                        '\1', '\2', '\3', '\4') }}

# delegate_to svc-infra because that is the only host with a textfile directory;
# the vm label is what keeps the three hosts' series apart. It is deliberately
# NOT `instance`: these files are published by svc-infra's exporter, so
# Prometheus stamps instance="svc-infra" on all of them, and reusing that label
# would overwrite it and make each series lie about where it came from.
#
# --success is unconditional here, because this task only runs when the counts
# were parsed at all, and that only happens on a run that measured something.
- name: Publish the container drift counts as metrics
  ansible.builtin.command:
    argv:
      - "{{ infra_metric_write_bin }}"
      - --dir
      - "{{ infra_textfile_dir }}"
      - --file
      - "drift-{{ inventory_hostname }}"
      - --prefix
      - homelab_drift
      - --labels
      - 'vm="{{ inventory_hostname }}"'
      - --success
    stdin: |
      homelab_drift_containers_running{vm="{{ inventory_hostname }}"} {{ service_vm_drift_counts[0] }}
      homelab_drift_quadlet_units{vm="{{ inventory_hostname }}"} {{ service_vm_drift_counts[1] }}
      homelab_drift_containers_drifted{vm="{{ inventory_hostname }}"} {{ service_vm_drift_counts[2] }}
      homelab_drift_containers_orphaned{vm="{{ inventory_hostname }}"} {{ service_vm_drift_counts[3] }}
  delegate_to: "{{ infra_host }}"
  register: service_vm_drift_metrics
  changed_when: false
  failed_when: false
  # `is not none`, not `| default([])`. regex_search returns Python None on a
  # no-match, and `default([])` substitutes only for Undefined — never for None —
  # so `None | length` raises "object of type 'NoneType' has no len()" right here,
  # on exactly the cannot-look path this guard exists to handle. That shipped once:
  # the emit task errored fatally and the play never reached the assert, so the
  # operator got an opaque Jinja crash instead of the script's own explanation of
  # why it could not look. verify.yml's outer rescue still failed the run, so rc 2
  # was never masked — but the diagnostic, which is the whole point of this path,
  # was lost.
  when: service_vm_drift_counts is not none and service_vm_drift_counts | length == 4

- name: Warn if the drift metrics could not be published
  ansible.builtin.debug:
    msg: >-
      metric publish FAILED (rc={{ service_vm_drift_metrics.rc | default('none') }}):
      {{ service_vm_drift_metrics.stderr | default('') }} —
      the estate dashboard will show {{ inventory_hostname }} drift as stale
  when:
    - service_vm_drift_counts is not none
    - service_vm_drift_counts | length == 4
    - (service_vm_drift_metrics.rc | default(0)) != 0

- name: Report the container drift result
  ansible.builtin.debug:
    msg: "{{ service_vm_container_drift.stdout_lines }}"

# The gate, unchanged in strictness and now the last word. rc 1 is drift, rc 2
# is "could not look", and they stay deliberately uncollapsed: a check that only
# tested for drift would let a broken query pass as a clean estate, which is the
# failure mode this repo has written down four separate times.
- name: Assert no container has drifted from its Quadlet unit
  ansible.builtin.assert:
    that: service_vm_container_drift.rc == 0
    fail_msg: >-
      {{ service_vm_container_drift.stdout | default('') }}
      {{ service_vm_container_drift.stderr | default('') }}
    success_msg: "container drift: OK"
```

- [ ] **Step 2: Validate**

Run: `make validate`
Expected: PASS. `validate_verify_safety.py` covers the verify path — if it
objects to the restructure, read what it is protecting and satisfy it rather
than loosening it.

- [ ] **Step 3: Run verify and confirm all three VMs published**

```bash
make verify
ssh svc-infra 'ls -la /opt/homelab/appdata/node-exporter-textfile/'
ssh svc-infra 'curl -s localhost:9100/metrics | grep "^homelab_drift_"'
```

Expected: three `drift-*.prom` files; series for all three `vm` values; and each
`homelab_drift_containers_running` non-zero. The three should sum to the same
total the drift check audits (54 at the time of writing).

- [ ] **Step 4: Prove the assert still fails on drift**

The restructure moved the gate. Confirm it still bites, or this task has
quietly disabled the check it was meant to preserve:

```bash
ssh svc-infra 'sudo /usr/local/sbin/homelab-container-drift.sh; echo "rc=$?"'
```

Expected: `rc=0` on a healthy host. Then temporarily edit a Quadlet's `Image=`
on svc-infra to a different digest, run `make verify`, and confirm the play
**fails** at `Assert no container has drifted` while
`drift-svc-infra.prom` still shows `homelab_drift_containers_drifted{vm="svc-infra"} 1`.
Revert the edit and re-run `make verify` to green before committing.

- [ ] **Step 5: Commit**

```bash
git add roles/service_vm/tasks/container-drift.yml
git commit -m "feat: publish drift counts before the assert that stops the play"
```

---

### Task 7: The dashboard and its gate

**Files:**
- Create: `roles/svc_infra/files/grafana-dashboards/homelab-estate.json`
- Create: `tests/validate_grafana_dashboards.py`
- Modify: `Makefile` (add to `validate-catalog`)

**Interfaces:**
- Consumes: every metric name produced by Tasks 3, 4 and 6.
- Produces: a provisioned dashboard at uid `homelab-estate`, in the `Homelab` folder.

**Dashboard-level fields.** `uid: "homelab-estate"`, `title: "Homelab estate"`,
`schemaVersion: 39`, `refresh: "15m"`, `time: {"from": "now-30d", "to": "now"}`,
`timezone: "browser"`, `tags: ["homelab"]`.

`now-30d` is not cosmetic: these series update nightly and weekly, so Grafana's
6h default would render an empty dashboard and the obvious conclusion would be
that the whole bridge is broken.

**Every panel** carries `"datasource": {"type": "prometheus", "uid": "prometheus"}`
at panel level and on each target. The uid is the stable one set in
`grafana-datasource.yml.j2`; that file's own header records that a uid mismatch
already took the Grafana container down once.

**Every stat panel** sets `"noValue": "no data"` in `fieldConfig.defaults`.
Grafana renders an absent series as `0` by default, which would turn a dead
emitter into a green tile reading "0 critical CVEs" — the exact failure this
design exists to prevent.

**Panel inventory.** Rows are `"type": "row"` panels with `collapsed: false`.

| # | Type | Title | `expr` | gridPos (h,w,x,y) | Notes |
|---|---|---|---|---|---|
| — | row | Now | — | 1,24,0,0 | |
| 1 | stat | Critical CVEs | `homelab_scan_vulnerabilities{severity="critical"}` | 4,4,0,1 | thresholds: green 0, red 1 |
| 2 | stat | High CVEs | `homelab_scan_vulnerabilities{severity="high"}` | 4,4,4,1 | |
| 3 | stat | Images behind upstream | `homelab_release_images_behind` | 4,4,8,1 | no thresholds — behind is expected here |
| 4 | stat | Containers drifted | `sum(homelab_drift_containers_drifted)` | 4,4,12,1 | thresholds: green 0, red 1 |
| 5 | stat | Containers with no unit | `sum(homelab_drift_containers_orphaned)` | 4,4,16,1 | thresholds: green 0, red 1 |
| 6 | stat | Images that failed to scan | `homelab_scan_images_failed` | 4,4,20,1 | thresholds: green 0, red 1 |
| — | row | Trend | — | 1,24,0,5 | |
| 7 | timeseries | CVEs by severity | `homelab_scan_vulnerabilities` | 8,12,0,6 | legend `{{severity}}` |
| 8 | timeseries | Images behind upstream | `homelab_release_images_behind` | 8,12,12,6 | |
| 9 | timeseries | Running containers by VM | `homelab_drift_containers_running` | 6,24,0,14 | legend `{{vm}}` |
| — | row | Coverage | — | 1,24,0,20 | |
| 10 | timeseries | Release coverage | A: `homelab_release_images_comparable` B: `homelab_release_images_total` | 7,12,0,21 | legends "comparable", "pinned total" |
| 11 | timeseries | Scan coverage | A: `homelab_scan_images_scanned` B: `homelab_scan_images_total` | 7,12,12,21 | legends "scanned", "pinned total" |
| — | row | Freshness | — | 1,24,0,28 | |
| 12 | stat | Scan age | `time() - homelab_scan_last_success_timestamp_seconds` | 4,6,0,29 | unit `s`, thresholds green 0 / red 93600 |
| 13 | stat | Release age | `time() - homelab_release_last_success_timestamp_seconds` | 4,6,6,29 | unit `s`, thresholds green 0 / red 691200 |
| 14 | stat | Drift age by VM | `time() - homelab_drift_last_success_timestamp_seconds` | 4,6,12,29 | unit `s`, legend `{{vm}}`, thresholds green 0 / red 93600 |
| 15 | stat | Textfile collector errors | `node_textfile_scrape_error{instance="svc-infra"}` | 4,6,18,29 | thresholds green 0 / red 1 |
| 16 | timeseries | Age of each published file | `time() - node_textfile_mtime_seconds` | 6,24,0,33 | unit `s`, legend `{{file}}` |
| — | row | Worst offenders | — | 1,24,0,39 | |
| 17 | table | Critical CVEs by image | `topk(15, homelab_scan_image_vulnerabilities{severity="critical"})` | 10,24,0,40 | `instant: true`, `format: "table"`; show the `digest` column |

Panel 17 shows one row per `image` **and** `digest`, so a repo pinned twice at
different digests appears twice — which is the point. Keep the `digest` column
visible or the two rows look like a duplicate bug rather than two real pins.
Anything wanting a per-repo total must say `sum by (image) (…)` explicitly;
nothing on this dashboard groups on `digest`, so trends stay continuous across a
bump. See Task 3 for why that label exists — the first live run published one of
two colliding valkey series and silently dropped the other.

Panels 15 and 16 are the bridge's own health check, and they are the closest
thing here to a positive control: `node_textfile_scrape_error` is node_exporter's
own verdict on whether it could parse the files, and `node_textfile_mtime_seconds`
exists per file whether or not our series inside it do. A file that stopped
being written shows up in panel 16 even if its `homelab_*` series vanished
entirely.

Different freshness thresholds per emitter, per the spec: the release check runs
weekly, so a single 26h threshold would show it permanently red, which is how a
freshness panel becomes something nobody looks at.

- [ ] **Step 1: Write the failing gate**

Create `tests/validate_grafana_dashboards.py`:

```python
#!/usr/bin/env python3
"""Assert every provisioned Grafana dashboard can actually render.

Three failure modes, all of which produce a dashboard that looks fine in git and
is empty or broken on the host:

  bad datasource uid   Grafana fails provisioning at boot outright. The header
                       of grafana-datasource.yml.j2 records the time this took
                       the whole container down.
  renamed metric       a panel querying homelab_scan_criticals when the emitter
                       writes homelab_scan_vulnerabilities renders a blank
                       graph and reports no error anywhere.
  stat with no noValue Grafana draws an absent series as 0, so a dead emitter
                       becomes a green tile reading "0 critical CVEs" — the
                       exact reading this whole design exists to prevent.

The metric cross-check is the one worth having. Names are collected from the
emitters themselves — the metric lines they pipe to homelab-metric-write, plus
the two timestamp series the publisher appends to each --prefix — so renaming a
metric in a play and forgetting the panel fails the build rather than the chart.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = ROOT / "roles/svc_infra/files/grafana-dashboards"

# Files whose `stdin:` blocks contain the metric lines the emitters publish.
EMITTER_PATHS = (
    "roles/svc_infra/tasks/scan.yml",
    "release.yml",
    "roles/service_vm/tasks/container-drift.yml",
)

# node_exporter's own series, which this repo does not emit but the freshness
# row legitimately queries. Explicit rather than a prefix rule: the point of the
# cross-check is that an unknown homelab_* name is an error, and a blanket
# "anything starting with node_ is fine" would be one loosening away from
# letting a typo through.
BUILTIN = {
    "node_textfile_scrape_error",
    "node_textfile_mtime_seconds",
}

DATASOURCE_UID = "prometheus"

# The --prefix argument, in either shape the emitters use it: an inline argv list
# ("'--prefix', 'homelab_scan']") or YAML list items ("- --prefix\n- homelab_drift").
# Matching across the intervening quotes, commas, brackets and newlines is what
# makes one pattern cover both; anchoring to end-of-line does not, because the
# inline form closes its list on the same line.
PREFIX_ARG = re.compile(r"--prefix['\",\s\]\[-]*?(homelab_[a-z0-9_]+)", re.S)

# Prometheus functions and keywords that appear in exprs and are not metrics.
NOT_METRICS = {"sum", "topk", "time", "rate", "increase", "avg", "max", "min",
               "count", "by", "without", "and", "or", "unless"}


def emitted_metric_names() -> set[str]:
    names: set[str] = set()
    for rel in EMITTER_PATHS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        # Metric lines as written in the stdin: blocks.
        names.update(re.findall(r"^\s*(homelab_[a-z0-9_]+)\s*\{", text, re.M))
        names.update(re.findall(r"^\s*(homelab_[a-z0-9_]+)\s+\S", text, re.M))
        # The publisher appends these two to every --prefix it is given, so they
        # are emitted names even though they appear nowhere as literal text.
        for prefix in PREFIX_ARG.findall(text):
            names.add(f"{prefix}_run_timestamp_seconds")
            names.add(f"{prefix}_last_success_timestamp_seconds")
    return names


def metrics_in_expr(expr: str) -> set[str]:
    found = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b(?=\s*[\{\s\)]|$)", expr))
    return {name for name in found if name not in NOT_METRICS and not name.isdigit()}


def main() -> int:
    failures: list[str] = []
    dashboards = sorted(DASHBOARD_DIR.glob("*.json"))
    if not dashboards:
        print(f"no dashboards found in {DASHBOARD_DIR}", file=sys.stderr)
        return 1

    known = emitted_metric_names() | BUILTIN
    if not known:
        print("collected zero emitted metric names — the extraction is broken, "
              "which would make this gate pass everything", file=sys.stderr)
        return 1

    for path in dashboards:
        try:
            board = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"{path.name}: does not parse: {exc}")
            continue

        if not board.get("uid"):
            failures.append(f"{path.name}: no uid. Provisioning needs a stable one.")
        if not board.get("title"):
            failures.append(f"{path.name}: no title.")

        for panel in board.get("panels", []):
            title = panel.get("title", "<untitled>")
            ptype = panel.get("type")
            if ptype == "row":
                continue

            ds = panel.get("datasource") or {}
            if ds.get("uid") != DATASOURCE_UID:
                failures.append(
                    f"{path.name}: panel {title!r} datasource uid is "
                    f"{ds.get('uid')!r}, expected {DATASOURCE_UID!r}"
                )

            if ptype == "stat":
                if "noValue" not in panel.get("fieldConfig", {}).get("defaults", {}):
                    failures.append(
                        f"{path.name}: stat panel {title!r} has no noValue. "
                        "An absent series would render as 0 and read as good news."
                    )

            for target in panel.get("targets", []):
                tds = target.get("datasource") or {}
                if tds.get("uid") != DATASOURCE_UID:
                    failures.append(
                        f"{path.name}: panel {title!r} target datasource uid is "
                        f"{tds.get('uid')!r}, expected {DATASOURCE_UID!r}"
                    )
                expr = target.get("expr", "")
                if not expr:
                    failures.append(f"{path.name}: panel {title!r} has an empty expr")
                    continue
                for metric in sorted(metrics_in_expr(expr)):
                    if metric not in known:
                        failures.append(
                            f"{path.name}: panel {title!r} queries {metric!r}, "
                            "which no emitter writes. Renamed in the play and "
                            "not in the dashboard, or a typo — either way the "
                            "panel would render blank with no error."
                        )

    if failures:
        print("Grafana dashboard validation FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"grafana dashboards: {len(dashboards)} file(s) OK, "
          f"{len(known)} known metric name(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the gate against the existing dashboard**

Run: `.venv/bin/python tests/validate_grafana_dashboards.py`
Expected: FAIL. `homelab-nodes.json` predates this gate and will report findings
(most likely missing `noValue` on its stat panels, possibly datasource shape).
**Fix `homelab-nodes.json` to satisfy the gate** — those are real defects by the
same reasoning, and its Uptime/Load/Root-filesystem stat tiles have exactly the
"absent series reads as 0" problem. Do not weaken the gate to let it pass.

- [ ] **Step 3: Write the dashboard**

Create `roles/svc_infra/files/grafana-dashboards/homelab-estate.json` from the
panel inventory above. Two fully-worked panels to copy the shape from — a stat
with thresholds and `noValue`:

```json
{
  "type": "stat",
  "title": "Critical CVEs",
  "datasource": { "type": "prometheus", "uid": "prometheus" },
  "gridPos": { "h": 4, "w": 4, "x": 0, "y": 1 },
  "fieldConfig": {
    "defaults": {
      "noValue": "no data",
      "thresholds": {
        "mode": "absolute",
        "steps": [
          { "color": "green", "value": null },
          { "color": "red", "value": 1 }
        ]
      }
    },
    "overrides": []
  },
  "options": {
    "colorMode": "value",
    "graphMode": "none",
    "reduceOptions": { "calcs": ["lastNotNull"], "fields": "", "values": false }
  },
  "targets": [
    {
      "refId": "A",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "expr": "homelab_scan_vulnerabilities{severity=\"critical\"}",
      "instant": true
    }
  ]
}
```

and a timeseries with a legend template:

```json
{
  "type": "timeseries",
  "title": "CVEs by severity",
  "datasource": { "type": "prometheus", "uid": "prometheus" },
  "gridPos": { "h": 8, "w": 12, "x": 0, "y": 6 },
  "fieldConfig": {
    "defaults": {
      "custom": { "drawStyle": "line", "lineWidth": 2, "fillOpacity": 10 }
    },
    "overrides": []
  },
  "options": {
    "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
    "tooltip": { "mode": "multi", "sort": "desc" }
  },
  "targets": [
    {
      "refId": "A",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "expr": "homelab_scan_vulnerabilities",
      "legendFormat": "{{severity}}"
    }
  ]
}
```

Row panels take this shape:

```json
{
  "type": "row",
  "title": "Now",
  "collapsed": false,
  "gridPos": { "h": 1, "w": 24, "x": 0, "y": 0 },
  "panels": []
}
```

- [ ] **Step 4: Run the gate to verify it passes**

Run: `.venv/bin/python tests/validate_grafana_dashboards.py`
Expected: PASS — `grafana dashboards: 2 file(s) OK, N known metric name(s)`.

- [ ] **Step 5: Prove the gate can fail**

A gate nobody has seen fail is a gate nobody can trust. Temporarily change one
panel's `expr` to `homelab_scan_criticals`, re-run, confirm it reports the
unknown metric, then revert.

- [ ] **Step 6: Wire it into `make validate`**

In `Makefile`, in the `validate-catalog` target after `validate_infra_catalog.py`, add:

```makefile
# Sits with the catalog gates because it validates a provisioned artifact the
# same way: the dashboards are files this repo owns and Grafana loads verbatim.
# The metric cross-check is the reason it exists — a name renamed in a play
# leaves the panel blank and reports nothing anywhere.
	$(PYTHON) tests/validate_grafana_dashboards.py
```

- [ ] **Step 7: Validate and deploy**

```bash
make validate
make infra
```

Expected: PASS, then the deploy restarts Grafana (the dashboards feed
`infra_grafana_dashboards` into the restart list at `files.yml:1030`).

- [ ] **Step 8: Confirm the dashboard renders with data**

Open `https://grafana.<domain>` (or `http://svc-infra:3005`), Homelab folder,
"Homelab estate". Confirm: panels show data rather than "no data", the
freshness tiles read in hours not weeks, and `Textfile collector errors` is 0.

If tiles read "no data", check `node_textfile_scrape_error` first — a non-zero
value means node_exporter rejected a file and none of its series exist.

- [ ] **Step 9: Commit**

```bash
git add roles/svc_infra/files/grafana-dashboards/homelab-estate.json \
        roles/svc_infra/files/grafana-dashboards/homelab-nodes.json \
        tests/validate_grafana_dashboards.py Makefile
git commit -m "feat: chart estate health, and gate the panels against the emitters"
```

---

### Task 8: Document the bridge

**Files:**
- Modify: `CLAUDE.md` (new subsection under "Verification means the application works")
- Modify: `docs/services.md` (Grafana entry)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing executable. Sub-projects B and C depend on this being written down.

- [ ] **Step 1: Add the CLAUDE.md subsection**

After the "So does scanning, and it fails the same way" section, add:

```markdown
### Trending is separate from alerting, and both are needed

Every check here reduced its numbers to a threshold and threw the number away.
That is why the rpool fill was an outage rather than a slope somebody noticed a
week earlier: nothing was wrong until it was entirely wrong, because no series
existed to look at.

node_exporter's textfile collector on svc-infra is the bridge. Anything that
already knows a number can publish it:

```bash
homelab-metric-write --dir /opt/homelab/appdata/node-exporter-textfile \
    --file scan --prefix homelab_scan --success <<'EOF'
homelab_scan_images_total 48
EOF
```

Four rules, learned the hard way:

- **The write goes in the play, never in `roles/svc_infra`.** A metrics file
  changes every run, so a template task in the role would make every
  `make infra` report `changed` and the `changed=0` proof would stop meaning
  anything.
- **Emit the number the alert used.** Every emitter reads the same fact that
  feeds ntfy. A second independent parse of `state.json` can drift from the
  first, and then two numbers disagree with no way to tell which is right.
- **Never publish zeros you did not measure.** `homelab-metric-write` refuses
  empty input and leaves the previous file in place, because a stale number is
  detectable and a zero reads as good news. The drift script's cannot-look
  paths exit *before* printing counts for the same reason.
- **Emit before you assert.** `container-drift.yml` publishes and then asserts,
  in that order. The other way round, the chart goes blank exactly when
  something is wrong.

`node_textfile_scrape_error` is node_exporter's own verdict on whether it could
parse the files, and the estate dashboard charts it. If a panel is empty, read
that before anything else — one malformed line makes node_exporter reject a
whole file, so a typo takes every series in it down at once. This is why the
publisher validates each line before writing, and why nothing emits `# HELP` or
`# TYPE`: the three drift files share timestamp metric names, and duplicate TYPE
declarations across merged textfiles make node_exporter reject them.
```

- [ ] **Step 2: Note the dashboard in docs/services.md**

Find the Grafana entry and add:

```markdown
Two provisioned dashboards, both owned by this repo (`allowUiUpdates: false`, so
UI edits revert on restart — copy a dashboard to a new name to experiment):

- **Homelab nodes** — host CPU, memory, disk and network from node_exporter.
- **Homelab estate** — CVE counts, images behind upstream, container drift, and
  a freshness row that reports how old each of those numbers is. Fed by the
  textfile collector rather than a scrape; see the trending section in
  `CLAUDE.md`. Its default range is 30 days because the series update nightly
  and weekly — a 6h window shows nothing and looks broken.
```

- [ ] **Step 3: Validate**

Run: `make validate`
Expected: PASS — `validate_links.py` checks the docs.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/services.md
git commit -m "docs: record the metrics bridge and its four rules"
```

---

### Task 9: Clean-tree deploy, and break it on purpose

**Files:** none — this is the verification the whole plan exists to earn.

**Interfaces:**
- Consumes: every task above.
- Produces: a deployed state that provably equals the committed tree.

- [ ] **Step 1: Confirm the tree is clean**

Run: `git status --porcelain`
Expected: no output. Untracked files count.

- [ ] **Step 2: Deploy from the clean tree**

```bash
make infra
```

Expected: svc-infra reports `changed=3` on this first run — the nightly runner's
`git archive` sync (rebuild, unpack, record `.deployed-rev`). Confirm it is
*those three tasks* and not a genuine diff.

- [ ] **Step 3: Deploy again and require changed=0**

```bash
make infra
```

Expected: `changed=0`. Anything else is a divergence between the deployed state
and the commit, and must be explained before merging. Do not paper over a real
diff by quoting the second number — check which tasks changed.

- [ ] **Step 4: Full verify**

```bash
make verify
```

Expected: green, including the restructured drift assert on all three VMs.

- [ ] **Step 5: Break the could-not-look path deliberately**

A healthy run never exercises the refusal branch, so this is the only way to
know it works:

```bash
ssh svc-infra 'sudo cp /opt/homelab/appdata/node-exporter-textfile/scan.prom /tmp/scan.prom.bak'
ssh svc-infra 'sudo /usr/local/sbin/homelab-metric-write --dir /opt/homelab/appdata/node-exporter-textfile --file scan --prefix homelab_scan </dev/null; echo "rc=$?"'
ssh svc-infra 'sudo diff /tmp/scan.prom.bak /opt/homelab/appdata/node-exporter-textfile/scan.prom && echo IDENTICAL'
```

Expected: `rc=3`, and `IDENTICAL`. The previous numbers survived rather than
being replaced with zeros. Then confirm the same for a malformed line:

```bash
ssh svc-infra 'echo "garbage" | sudo /usr/local/sbin/homelab-metric-write --dir /opt/homelab/appdata/node-exporter-textfile --file scan --prefix homelab_scan; echo "rc=$?"'
ssh svc-infra 'sudo diff /tmp/scan.prom.bak /opt/homelab/appdata/node-exporter-textfile/scan.prom && echo IDENTICAL'
ssh svc-infra 'sudo rm /tmp/scan.prom.bak'
```

Expected: `rc=2`, `IDENTICAL`, and `node_textfile_scrape_error` still 0.

- [ ] **Step 6: Confirm the alert paths are untouched**

The emitters were added to plays that publish to ntfy. Confirm the messages
still arrive and read the same as before:

```bash
curl -s "http://<svc-media>:8080/homelab-alerts/json?poll=1&since=10m"
```

(`since` takes `24h`/`168h`/`all`, not `7d`. Retention is ~12h in memory, so
poll promptly.)

- [ ] **Step 7: Merge, push, and delete the branch**

```bash
git switch main
git merge --ff-only docs/grafana-estate-metrics
git push
git branch -d docs/grafana-estate-metrics
git push origin --delete docs/grafana-estate-metrics
```

Then watch the CI run on `main` — it runs `systemd-analyze verify` against the
changed Quadlet, which no macOS workstation can do. A red run means something
already merged is broken and needs a follow-up commit.

While you are here: `feat/ha-host-networking` is also fully merged and should be
deleted the same way.

---

## Self-Review

**Spec coverage.** Every section of the spec maps to a task: the bridge and
helper to Tasks 1–2, the metric contract to Task 1 (timestamps, no HELP/TYPE)
and Task 2 (`vm` label reasoning lives in Task 6), the three emitters to Tasks
3, 4 and 6 with the drift script prerequisite in Task 5, the dashboard and its
two settings to Task 7, all four validation gates to Tasks 1, 3, 5 and 7, and
the six-step live verification to Tasks 2–9. The spec's non-goals (the
credential canary, sub-projects B and C, Grafana alerting, the other two VMs'
collectors) appear in no task, correctly.

**Gaps found and closed while reviewing:**

- The spec's per-emitter freshness table needed different thresholds in the
  dashboard; Task 7's panel inventory now carries 93600 for scan and drift and
  691200 for release explicitly.
- The spec did not mention `node_textfile_scrape_error` or
  `node_textfile_mtime_seconds`. They are node_exporter's own verdict on the
  bridge, and closer to a real positive control than anything the emitters can
  say about themselves, so panels 15 and 16 were added and the gate allowlists
  them.
- Nothing in the spec said the *existing* `homelab-nodes.json` would fail the
  new gate. Task 7 Step 2 makes fixing it part of the work rather than a reason
  to weaken the gate.
- The `comparable` value has no backing fact and must be derived; Task 4 states
  the derivation and Step 3 checks the arithmetic by hand.

**Code in this plan was executed before it was written down.** The helper and its
seven-case test were extracted and run together: all seven pass, including the
two refusals that keep a broken emitter legible. The dashboard gate's two
extraction regexes were run against every `expr` in the panel inventory and
against both argument shapes the emitters use.

That last check found a bug. The first draft matched `--prefix` values with an
end-of-line anchor, which works for the YAML list form
(`- --prefix\n- homelab_drift`) but not for the inline argv form
(`'--prefix', 'homelab_scan']`, which closes its list on the same line). The
consequence would have been invisible in the worst way: `homelab_scan` and
`homelab_release` would never be recognised as prefixes, so the freshness
panels querying `homelab_scan_last_success_timestamp_seconds` would fail the
gate as "no emitter writes this" — a gate blocking correct work, which is how
gates get loosened. `PREFIX_ARG` now matches across the intervening quotes,
commas and brackets and is verified against all three call sites.

**Placeholder scan.** No TBDs. Two places deliberately defer to the
implementer's reading of live code rather than guessing: Task 5's `units=`
fixture counts (the assertion must match what the fixture writes) and Task 7
Step 2's list of `homelab-nodes.json` findings (whatever the gate reports).
Both say explicitly what to do in either case.

**Type consistency.** The helper's flags are `--dir/--file/--prefix/--labels/--success`
in Task 1 and identically in Tasks 3, 4 and 6. Exit codes `0/1/2/3` are used
consistently in the helper, its test, and Task 9's live checks. Metric names in
Task 7's panel inventory match the emitters exactly: `homelab_scan_vulnerabilities`,
`homelab_scan_image_vulnerabilities`, `homelab_scan_images_{total,scanned,failed,eosl}`,
`homelab_release_images_{total,comparable,unmeasured,behind,current}`,
`homelab_release_{new,errors}`, `homelab_drift_containers_{running,drifted,orphaned}`,
`homelab_drift_quadlet_units`, and the `_run_timestamp_seconds` /
`_last_success_timestamp_seconds` pairs under prefixes `homelab_scan`,
`homelab_release`, `homelab_drift`. The gate in Task 7 mechanically enforces
this from here on.
