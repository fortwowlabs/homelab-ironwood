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
import time
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


def test_first_run_without_success_has_no_last_success_line() -> None:
    # A first-ever run legitimately has no prior success to carry forward —
    # there must be nothing to read yet, so the line is simply absent, not
    # fabricated as a zero or a "now".
    with tempfile.TemporaryDirectory() as tmp:
        run(tmp, ["--file", "scan", "--prefix", "homelab_scan"], SAMPLE_IN)
        body = Path(tmp, "scan.prom").read_text(encoding="utf-8")
        check("no last_success line on a first run",
              "homelab_scan_last_success_timestamp_seconds" not in body, body)


def test_carries_last_success_forward_without_flag() -> None:
    # The whole file is rewritten every run, so simply omitting --success
    # would DELETE a prior success rather than freeze it. The publisher must
    # instead read the old line back off disk and copy it forward verbatim
    # while the run timestamp still advances.
    with tempfile.TemporaryDirectory() as tmp:
        run(tmp, ["--file", "scan", "--prefix", "homelab_scan", "--success"], SAMPLE_IN)
        first = Path(tmp, "scan.prom").read_text(encoding="utf-8")
        first_success = next(
            l for l in first.splitlines()
            if l.startswith("homelab_scan_last_success_timestamp_seconds"))
        first_run = next(
            l for l in first.splitlines()
            if l.startswith("homelab_scan_run_timestamp_seconds"))

        time.sleep(1.1)  # force the integer run timestamp to actually advance

        proc = run(tmp, ["--file", "scan", "--prefix", "homelab_scan"], SAMPLE_IN)
        second = Path(tmp, "scan.prom").read_text(encoding="utf-8")
        second_lines = second.splitlines()
        success_lines = [
            l for l in second_lines
            if l.startswith("homelab_scan_last_success_timestamp_seconds")]
        second_run = next(
            l for l in second_lines
            if l.startswith("homelab_scan_run_timestamp_seconds"))

        check("no --success still exits 0", proc.returncode == 0, proc.stderr)
        check("exactly one last_success line survives", len(success_lines) == 1,
              second)
        check("last_success carried forward verbatim",
              success_lines and success_lines[0] == first_success,
              f"{first_success!r} -> {success_lines!r}")
        check("run timestamp advanced", second_run != first_run,
              f"{first_run!r} -> {second_run!r}")


def test_success_overwrites_existing_last_success() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run(tmp, ["--file", "scan", "--prefix", "homelab_scan", "--success"], SAMPLE_IN)
        first = Path(tmp, "scan.prom").read_text(encoding="utf-8")
        first_success = next(
            l for l in first.splitlines()
            if l.startswith("homelab_scan_last_success_timestamp_seconds"))

        time.sleep(1.1)

        run(tmp, ["--file", "scan", "--prefix", "homelab_scan", "--success"], SAMPLE_IN)
        second = Path(tmp, "scan.prom").read_text(encoding="utf-8")
        second_lines = second.splitlines()
        success_lines = [
            l for l in second_lines
            if l.startswith("homelab_scan_last_success_timestamp_seconds")]

        check("exactly one last_success line after two --success runs",
              len(success_lines) == 1, second)
        check("--success overwrites rather than carries forward the old stamp",
              success_lines and success_lines[0] != first_success,
              f"{first_success!r} vs {success_lines!r}")


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


def test_missing_required_flag() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proc = run(tmp, ["--file", "scan"], SAMPLE_IN)
        check("missing required flag exits 1", proc.returncode == 1, f"rc={proc.returncode}")
        check("missing flag writes no file", not Path(tmp, "scan.prom").exists())


def test_unrecognized_flag() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proc = run(tmp, ["--file", "scan", "--prefix", "homelab_scan", "--nope"], SAMPLE_IN)
        check("unrecognized flag exits 1", proc.returncode == 1, f"rc={proc.returncode}")
        check("unrecognized flag writes no file", not Path(tmp, "scan.prom").exists())


def main() -> int:
    if not HELPER.exists():
        print(f"missing {HELPER}", file=sys.stderr)
        return 1
    tests = (
        test_publishes_valid_input,
        test_success_flag_adds_success_timestamp,
        test_first_run_without_success_has_no_last_success_line,
        test_carries_last_success_forward_without_flag,
        test_success_overwrites_existing_last_success,
        test_labels_land_on_timestamp_series,
        test_empty_stdin_preserves_previous_file,
        test_malformed_line_writes_nothing,
        test_bad_labels_rejected,
        test_bad_prefix_rejected,
        test_missing_required_flag,
        test_unrecognized_flag,
    )
    for fn in tests:
        fn()
    if failures:
        print("homelab-metric-write FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print(f"homelab-metric-write: {len(tests)} case(s) OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
