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
