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

# In the catalog group because it validates a provisioned artifact the same
# way: the dashboards are files this repo owns and Grafana loads verbatim. The
# metric cross-check is the reason it exists — a name renamed in a play leaves
# the panel blank and reports nothing anywhere.
#
# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "catalog"

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = ROOT / "roles/svc_infra/files/grafana-dashboards"

# Files whose `stdin:` blocks contain the metric lines the emitters publish.
EMITTER_PATHS = (
    "roles/svc_infra/tasks/scan.yml",
    "release.yml",
    "roles/service_vm/tasks/container-drift.yml",
    # A shell template rather than a play, and the only emitter that is one.
    # Its metric lines sit at the left margin of a heredoc for exactly this
    # reason, and its --prefix is spelled the same way the plays spell theirs,
    # so both collectors below read it without a special case.
    "roles/svc_infra/templates/chat-egress-probe.sh.j2",
)

# node_exporter's own series, which this repo does not emit but legitimately
# queries: the two textfile-collector series the estate dashboard's freshness
# row reads, plus the standard node_exporter series homelab-nodes.json plots.
# Explicit rather than a prefix rule: the point of the cross-check is that an
# unknown homelab_* name is an error, and a blanket "anything starting with
# node_ is fine" would be one loosening away from letting a typo through.
BUILTIN = {
    "node_textfile_scrape_error",
    "node_textfile_mtime_seconds",
    "node_boot_time_seconds",
    "node_load1",
    "node_filesystem_avail_bytes",
    "node_filesystem_size_bytes",
    "node_cpu_seconds_total",
    "node_memory_MemAvailable_bytes",
    "node_memory_MemTotal_bytes",
    "node_network_receive_bytes_total",
    "node_network_transmit_bytes_total",
}

DATASOURCE_UID = "prometheus"

# The --prefix argument, in either shape the emitters use it: an inline argv list
# ("'--prefix', 'homelab_scan']") or YAML list items ("- --prefix\n- homelab_drift").
# Matching across the intervening quotes, commas, brackets and newlines is what
# makes one pattern cover both; anchoring to end-of-line does not, because the
# inline form closes its list on the same line.
PREFIX_ARG = re.compile(r"--prefix['\",\s\]\[-]*?(homelab_[a-z0-9_]+)", re.S)

# Prometheus functions and keywords that appear in exprs and are not metrics.
# "instance" is a label name, not a metric — it shows up here because
# `metrics_in_expr`'s regex accepts anything followed by `)`, which is right
# for a bare function call like `time()` but also matches a label inside an
# aggregation clause such as `sum by (instance) (...)`. Homelab-nodes.json's
# CPU/network panels use exactly that clause.
NOT_METRICS = {"sum", "topk", "time", "rate", "increase", "avg", "max", "min",
               "count", "by", "without", "and", "or", "unless", "instance"}


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
    # `\[` belongs in the lookahead: a range-vector selector like
    # `rate(homelab_typo_total[5m])` has the metric name followed directly by
    # `[`, not `{`, whitespace or end-of-string. Without it the name simply
    # fails to match at all and the typo passes silently — the Trend row's
    # rate()/increase() panels are exactly where such a query would land.
    found = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b(?=\s*[\{\[\s\)]|$)", expr))
    return {name for name in found if name not in NOT_METRICS and not name.isdigit()}


# Shapes the extractor must handle, checked on every run. No dashboard currently
# writes a bare `name[5m]` range selector, so without this the `\[` branch above
# is unexercised by anything in the repo — reverting it would break the gate's
# ability to catch a typo and every existing check would still pass. That is the
# failure this whole gate exists to prevent, so it is not allowed to apply to the
# gate itself.
#
# Each case is (expr, the metric names that must be extracted).
EXTRACTION_CASES = (
    ("rate(homelab_typo_total[5m])", {"homelab_typo_total"}),
    ("increase(homelab_scan_images_failed[7d])", {"homelab_scan_images_failed"}),
    ('homelab_scan_vulnerabilities{severity="critical"}', {"homelab_scan_vulnerabilities"}),
    ("sum(homelab_drift_containers_drifted)", {"homelab_drift_containers_drifted"}),
    ("time() - homelab_scan_last_success_timestamp_seconds",
     {"homelab_scan_last_success_timestamp_seconds"}),
    # `instance` is a label here, not a metric, and `by`/`sum` are keywords.
    ("sum by (instance) (homelab_release_errors)", {"homelab_release_errors"}),
)


def extraction_self_check() -> list[str]:
    """Prove the extractor still sees each query shape the dashboards can use."""
    problems: list[str] = []
    for expr, expected in EXTRACTION_CASES:
        got = metrics_in_expr(expr)
        if got != expected:
            problems.append(
                f"metrics_in_expr({expr!r}) returned {sorted(got)}, "
                f"expected {sorted(expected)} — the extractor is blind to a shape "
                "a panel can legitimately use, so a typo in one would pass silently"
            )
    return problems


def main() -> int:
    failures: list[str] = extraction_self_check()
    dashboards = sorted(DASHBOARD_DIR.glob("*.json"))
    if not dashboards:
        print(f"no dashboards found in {DASHBOARD_DIR}", file=sys.stderr)
        return 1

    # Checked against the extraction's own result, not against `known`: BUILTIN
    # is a non-empty static set, so `known` can never be empty even if the
    # extraction found nothing — a guard on `known` would be unreachable and
    # a broken extraction would pass silently as long as every dashboard
    # happened to query only BUILTIN names.
    emitted = emitted_metric_names()
    if not emitted:
        print("collected zero emitted metric names — the extraction is broken, "
              "which would make this gate pass everything", file=sys.stderr)
        return 1
    known = emitted | BUILTIN

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
