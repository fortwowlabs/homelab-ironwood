#!/usr/bin/env python3
"""Validate the inference model roster's schema and its stated exceptions.

The roster spans two hosts and six roles, and until now lived as prose tables
in two documents that already disagreed with each other. This is the gate that
makes it data.

The rule that earns this file is the alignment exception. `qwen3-coder` being
un-abliterated and `qwen3:30b` being deliberately aligned are DECISIONS, and a
catalog that cannot tell a decision from an oversight is not worth keeping. So
every `abliterated: false` entry must say why, in writing.

It also cross-checks scripts/abliteration_control.py's ROSTER tuple against the
catalog. That script is the only thing that can detect a wrong or failed model,
and a model added to the catalog but not to the control is a model nobody is
checking.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROSTER_PATH = ROOT / "inventory/group_vars/all/models.yml"
CONTROL_PATH = ROOT / "scripts/abliteration_control.py"

CARD_TOTAL_MIB = 24564
TIERS = {"terra", "mbp"}
ROLES = {"chat", "code", "vision", "embed", "autocomplete", "baseline"}
REQUIRED = {"name", "tier", "role", "abliterated", "why"}
OPTIONAL = {"default", "alignment_exception", "measured_mib", "measured_on", "num_ctx"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ok_entry(**overrides) -> dict:
    """A minimal valid entry, for the self-check to mutate."""
    entry = {
        "name": "example/model:1b",
        "tier": "terra",
        "role": "chat",
        "abliterated": True,
        "why": "example",
        "default": True,
    }
    entry.update(overrides)
    return entry


def check_roster(roster: list[dict], control_roster: tuple[str, ...]) -> list[str]:
    """Return every problem with this roster. Empty list means valid."""
    problems: list[str] = []
    seen: set[str] = set()
    defaults: list[str] = []

    for entry in roster:
        name = entry.get("name", "<unnamed>")

        missing = REQUIRED - entry.keys()
        if missing:
            problems.append(f"{name}: missing required {sorted(missing)}")
        unknown = entry.keys() - REQUIRED - OPTIONAL
        if unknown:
            problems.append(
                f"{name}: unknown field {sorted(unknown)} — a typo here is "
                "silently ignored by everything that reads the catalog")

        if name in seen:
            problems.append(f"{name}: appears twice in the roster")
        seen.add(name)

        tier = entry.get("tier")
        if tier not in TIERS:
            problems.append(f"{name}: {tier!r} is not a known tier {sorted(TIERS)}")
        role = entry.get("role")
        if role not in ROLES:
            problems.append(f"{name}: {role!r} is not a known role {sorted(ROLES)}")

        if not str(entry.get("why", "")).strip():
            problems.append(f"{name}: `why` is empty — say what it is for")

        if entry.get("abliterated") is False and not str(
                entry.get("alignment_exception", "")).strip():
            problems.append(
                f"{name}: is not abliterated and must carry `alignment_exception` "
                "saying why. Every other chat model here is abliterated on "
                "purpose, so an un-abliterated one is either a decision or an "
                "oversight and the catalog has to say which")

        if entry.get("default"):
            defaults.append(name)

        mib = entry.get("measured_mib")
        if mib is not None:
            on = entry.get("measured_on")
            if not on:
                problems.append(
                    f"{name}: has measured_mib but no measured_on. An undated "
                    "measurement cannot be judged against the roster it was "
                    "taken with")
            elif not DATE_RE.match(str(on)):
                problems.append(f"{name}: measured_on {on!r} is not YYYY-MM-DD")
            if mib <= 0:
                problems.append(
                    f"{name}: measured_mib {mib} is not a measurement. A model "
                    "occupies memory, so zero means a placeholder was never "
                    "filled in from a survey run")
            if tier == "terra" and mib >= CARD_TOTAL_MIB:
                problems.append(
                    f"{name}: measured_mib {mib} exceeds the card ({CARD_TOTAL_MIB} "
                    "MiB), which is not a possible measurement")

    if len(defaults) != 1:
        problems.append(
            f"expected exactly one entry with `default: true`, found {defaults} "
            "— Open WebUI needs one and only one default")

    should_control = {
        e["name"] for e in roster
        if e.get("tier") == "terra" and e.get("abliterated") is True
        and e.get("role") in {"chat", "code"}
    }
    for name in sorted(should_control - set(control_roster)):
        problems.append(
            f"{name}: abliterated but not in abliteration_control.py's ROSTER — "
            "nothing would ever verify it is actually uncensored")
    for name in sorted(set(control_roster) - should_control):
        problems.append(
            f"{name}: in abliteration_control.py's ROSTER but not in the catalog "
            "as an abliterated terra chat/code model — the control would fail "
            "against a model nobody declared")
    return problems


# Each case is (description, roster, control_roster, substring that must appear
# in some problem). A case with `None` as the substring must produce NO
# problems. Without this table every rule below could be deleted and the gate
# would still pass everything, which is the exact failure this repo keeps
# hitting.
SELF_CHECK_CASES = (
    ("valid minimal roster",
     [_ok_entry()], ("example/model:1b",), None),
    ("missing required field",
     [{k: v for k, v in _ok_entry().items() if k != "why"}],
     ("example/model:1b",), "missing required"),
    ("why is empty or whitespace",
     [_ok_entry(why="   ")], ("example/model:1b",), "say what it is for"),
    ("unknown field (typo)",
     [_ok_entry(measured_mb=100)], ("example/model:1b",), "unknown field"),
    ("bad tier",
     [_ok_entry(tier="laptop")], ("example/model:1b",), "not a known tier"),
    ("bad role",
     [_ok_entry(role="therapy")], ("example/model:1b",), "not a known role"),
    ("duplicate name",
     [_ok_entry(), _ok_entry(default=False)],
     ("example/model:1b",), "appears twice"),
    ("no default chat model",
     [_ok_entry(default=False)], ("example/model:1b",), "exactly one"),
    ("two default chat models",
     [_ok_entry(), _ok_entry(name="other:1b", default=True)],
     ("example/model:1b", "other:1b"), "exactly one"),
    ("un-abliterated with no stated reason",
     [_ok_entry(abliterated=False)], (), "must carry `alignment_exception`"),
    ("un-abliterated with a stated reason is fine",
     [_ok_entry(abliterated=False, alignment_exception="deliberately aligned")],
     (), None),
    ("measurement with no date",
     [_ok_entry(measured_mib=20000)], ("example/model:1b",), "undated"),
    ("measured_mib left at the zero placeholder",
     [_ok_entry(measured_mib=0, measured_on="2026-08-11")],
     ("example/model:1b",), "not a measurement"),
    ("measurement larger than the card",
     [_ok_entry(measured_mib=30000, measured_on="2026-08-11")],
     ("example/model:1b",), "exceeds the card"),
    ("bad date format",
     [_ok_entry(measured_mib=20000, measured_on="11/08/2026")],
     ("example/model:1b",), "YYYY-MM-DD"),
    ("abliterated terra model missing from the control script",
     [_ok_entry()], (), "not in abliteration_control.py"),
    ("control script names a model not in the catalog",
     [_ok_entry()], ("example/model:1b", "ghost:7b"), "not in the catalog"),
)


def self_check() -> list[str]:
    """Prove each rule still fires. A gate must not be able to fail silently."""
    problems: list[str] = []
    for description, roster, control, expected in SELF_CHECK_CASES:
        got = check_roster(roster, control)
        if expected is None:
            if got:
                problems.append(
                    f"self-check {description!r}: expected no problems, got {got}")
        elif not any(expected in p for p in got):
            problems.append(
                f"self-check {description!r}: expected a problem containing "
                f"{expected!r}, got {got or 'no problems at all'} — the rule is "
                "not firing, so a real roster with this defect would pass")
    return problems


def load_roster() -> list[dict]:
    data = yaml.safe_load(ROSTER_PATH.read_text(encoding="utf-8")) or {}
    roster = data.get("model_roster")
    if not isinstance(roster, list) or not roster:
        raise SystemExit(
            f"{ROSTER_PATH}: no `model_roster` list. Refusing to report a clean "
            "run against a catalog that could not be read")
    return roster


def load_control_roster() -> tuple[str, ...]:
    """Read the ROSTER tuple out of abliteration_control.py without importing it.

    Importing would be simpler but that script talks to the network at import
    time in future revisions; parsing the literal keeps this gate offline.
    """
    tree = ast.parse(CONTROL_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ROSTER" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise SystemExit(
        f"{CONTROL_PATH}: no ROSTER assignment found. It was renamed or removed, "
        "and this cross-check silently stopped covering anything")


def main() -> int:
    failures = self_check()
    if failures:
        print("the validator's own self-check failed:", file=sys.stderr)
        for problem in failures:
            print(f"  {problem}", file=sys.stderr)
        return 1

    problems = check_roster(load_roster(), load_control_roster())
    if problems:
        print(f"{ROSTER_PATH.name}: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("Model roster: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
