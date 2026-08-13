#!/usr/bin/env python3
"""Compare the roster in git against Ollama and against Open WebUI.

Open WebUI's model list is its OWN TABLE, not a view over Ollama. So removing a
model upstream leaves the entry behind, and a user who picks it gets a failure
at generation time rather than an absence in the dropdown.

That is not hypothetical. aratan/qwen3.6-claude-coder-35b was deleted from
Ollama on 2026-08-10 and its row is still live in Open WebUI with is_active = 1.
Nothing in the estate would ever have reported it. This is that report.

    scripts/roster_reconcile.py --webui-db /opt/homelab/appdata/open-webui/webui.db
    scripts/roster_reconcile.py --self-check

Exit codes:
    0  the three agree
    1  a mismatch, or a source could not be read
    2  bad arguments
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROSTER_PATH = ROOT / "inventory/group_vars/all/models.yml"
DEFAULT_OLLAMA = "http://192.168.1.40:11434"


def reconcile(catalog: set[str], ollama: set[str], webui: set[str],
              held: set[str] | None = None) -> list[tuple[str, str, str]]:
    """Every disagreement between the three, most serious first.

    `held` is the subset of the catalog whose weights are deliberately on disk
    but which must not be served - see `held:` in models.yml. They are declared,
    so they are not drift; but being installed is exactly what makes them
    reachable, so serving one is a finding of its own.
    """
    held = held or set()
    findings: list[tuple[str, str, str]] = []
    for name in sorted(webui - ollama):
        findings.append((
            "BROKEN", name,
            "selectable in Open WebUI but not installed in Ollama - a user who "
            "picks it gets a failure at generation time, not an absence"))
    # Intersected with `ollama` so this cannot double-report: a held model that
    # is ALSO gone from Ollama is already the BROKEN case above, and one finding
    # per model is what makes the output countable.
    for name in sorted(webui & held & ollama):
        findings.append((
            "SERVED_HELD", name,
            "held in models.yml as retained-but-unusable, yet selectable in "
            "Open WebUI - the weights are on disk on purpose, which is what "
            "makes this reachable; a user who picks it gets known-broken output"))
    for name in sorted(ollama - catalog):
        findings.append((
            "UNDECLARED", name,
            "installed in Ollama but not in models.yml - undeclared drift"))
    for name in sorted(catalog - ollama):
        findings.append((
            "MISSING", name,
            "declared in models.yml but not installed in Ollama"))
    return findings


# Ordering matters as much as detection: BROKEN is the one with a live user
# impact and must not be buried under a list of MISSING entries.
RECONCILE_CASES = (
    ("all three agree", {"a"}, {"a"}, {"a"}, set(), []),
    ("the aratan case - stale in Open WebUI",
     {"a"}, {"a"}, {"a", "ghost"}, set(), [("BROKEN", "ghost")]),
    ("installed but undeclared",
     {"a"}, {"a", "extra"}, {"a"}, set(), [("UNDECLARED", "extra")]),
    ("declared but not installed",
     {"a", "planned"}, {"a"}, {"a"}, set(), [("MISSING", "planned")]),
    ("BROKEN sorts above MISSING",
     {"a", "planned"}, {"a"}, {"a", "ghost"}, set(),
     [("BROKEN", "ghost"), ("MISSING", "planned")]),
    ("all three categories at once, in severity order and sorted within it",
     {"a", "planned"}, {"a", "extra"}, {"a", "extra", "ghost", "zeta"}, set(),
     [("BROKEN", "ghost"), ("BROKEN", "zeta"), ("UNDECLARED", "extra"),
      ("MISSING", "planned")]),
    # The held cases. The first is the whole point of the state: weights on
    # disk on purpose must stop reading as drift, or the check is red forever
    # and nobody reads a check that is always red.
    ("a held model installed and not served is NOT drift",
     {"a", "shelved"}, {"a", "shelved"}, {"a"}, {"shelved"}, []),
    ("a held model that is served is reported",
     {"a", "shelved"}, {"a", "shelved"}, {"a", "shelved"}, {"shelved"},
     [("SERVED_HELD", "shelved")]),
    ("a held model gone from Ollama reports BROKEN once, not twice",
     {"a", "shelved"}, {"a"}, {"a", "shelved"}, {"shelved"},
     [("BROKEN", "shelved"), ("MISSING", "shelved")]),
    ("SERVED_HELD sorts below BROKEN and above UNDECLARED",
     {"a", "shelved"}, {"a", "shelved", "extra"}, {"a", "shelved", "ghost"},
     {"shelved"},
     [("BROKEN", "ghost"), ("SERVED_HELD", "shelved"),
      ("UNDECLARED", "extra")]),
    ("held defaults to empty - an unheld roster behaves exactly as before",
     {"a", "shelved"}, {"a", "shelved"}, {"a", "shelved"}, None, []),
)


def self_check() -> list[str]:
    problems: list[str] = []
    for description, catalog, ollama, webui, held, expected in RECONCILE_CASES:
        got = [(sev, name) for sev, name, _ in
               reconcile(catalog, ollama, webui, held)]
        if got != expected:
            problems.append(
                f"reconcile {description!r}: got {got}, expected {expected} — the "
                "comparison is wrong, so a model that is broken for users would "
                "not be reported")
    return problems


def catalog_names() -> tuple[set[str], set[str]]:
    """(every terra model declared, the subset of those marked `held: true`)."""
    data = yaml.safe_load(ROSTER_PATH.read_text(encoding="utf-8")) or {}
    roster = data.get("model_roster") or []
    terra = [e for e in roster if e.get("tier") == "terra"]
    return ({e["name"] for e in terra},
            {e["name"] for e in terra if e.get("held") is True})


def ollama_names(base_url: str, timeout: int) -> set[str]:
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags",
                                timeout=timeout) as resp:
        tags = json.load(resp)
    names = {m["name"] for m in tags.get("models", [])}
    if not names:
        raise SystemExit(
            "Ollama reported zero models. That is almost certainly a broken "
            "query rather than an empty host, and reporting every catalogued "
            "model as MISSING would be worse than not running")
    # /api/tags suffixes bare names with :latest; the catalog writes them bare.
    return {n[: -len(":latest")] if n.endswith(":latest") else n for n in names}


def webui_names(db_path: str) -> set[str]:
    if not Path(db_path).is_file():
        raise SystemExit(
            f"{db_path}: no such file. That path is svc-infra's Open WebUI "
            "database by default (make DB=... to point at a different one, "
            "e.g. when running this off the workstation)")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # `base_model_id IS NULL` is load-bearing, not a tidiness filter.
        # Personas (Workspace -> Models) are rows in this SAME table, carrying
        # the base model they wrap in base_model_id. Their ids are things like
        # `thera`, which no Ollama tag will ever match — so without this clause
        # every persona reports as BROKEN and the one finding that matters is
        # buried under false ones on the very first run.
        rows = con.execute(
            "SELECT id FROM model WHERE is_active = 1 AND base_model_id IS NULL"
        ).fetchall()
    finally:
        con.close()
    return {r[0][: -len(":latest")] if r[0].endswith(":latest") else r[0]
            for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA)
    parser.add_argument("--webui-db",
                        help="path to Open WebUI's webui.db (read-only)")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    if args.self_check:
        problems = self_check()
        for problem in problems:
            print(problem, file=sys.stderr)
        print("self-check: OK" if not problems else "self-check: FAILED")
        return 1 if problems else 0

    if not args.webui_db:
        parser.print_usage(sys.stderr)
        print("--webui-db is required. Without it the check cannot see the one "
              "mismatch it exists to find", file=sys.stderr)
        return 2

    catalog, held = catalog_names()
    findings = reconcile(catalog, ollama_names(args.ollama_url, args.timeout),
                         webui_names(args.webui_db), held)
    if not findings:
        print("Roster reconciliation: OK - catalog, Ollama and Open WebUI agree")
        return 0
    for severity, name, explanation in findings:
        print(f"{severity:<12} {name}", file=sys.stderr)
        print(f"{'':<12} {explanation}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
