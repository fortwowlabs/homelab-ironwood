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

# The roster is the only description of which models exist on which host. It
# sits with the catalog gates because it validates the same kind of artifact:
# a data file this repo owns and other things read verbatim.
#
# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "catalog"

ROOT = Path(__file__).resolve().parents[1]
ROSTER_PATH = ROOT / "inventory/group_vars/all/models.yml"
CONTROL_PATH = ROOT / "scripts/abliteration_control.py"
VISION_CONTROL_PATH = ROOT / "scripts/vision_control.py"

CARD_TOTAL_MIB = 24564
TIERS = {"terra", "mbp"}
ROLES = {"chat", "code", "vision", "embed", "autocomplete", "baseline"}
REQUIRED = {"name", "tier", "role", "abliterated", "why"}
OPTIONAL = {"default", "alignment_exception", "measured_mib", "measured_on",
            "num_ctx", "held", "held_reason"}
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


def check_roster(roster: list[dict], control_roster: tuple[str, ...],
                 vision_roster: tuple[str, ...] = ()) -> list[str]:
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

        # `held` means the weights are deliberately on disk but must not be
        # served - a model tried, rejected, and kept for a later re-check. It
        # exists so that retention stops reading as undeclared drift in
        # roster_reconcile.py; without it a deliberate decision leaves that
        # check permanently red, and nobody reads a check that is always red.
        held = entry.get("held")
        if held is not None and not isinstance(held, bool):
            problems.append(
                f"{name}: `held` is {held!r}, not a boolean — anything truthy "
                "would silently shelve a model that is meant to be served")
        if held is True and not str(entry.get("held_reason", "")).strip():
            problems.append(
                f"{name}: is `held` and must carry `held_reason`. Weights kept "
                "on disk without a written reason are indistinguishable from "
                "weights nobody got round to deleting, and the whole point of "
                "the state is to record which")
        if held is not True and str(entry.get("held_reason", "")).strip():
            problems.append(
                f"{name}: carries `held_reason` but is not `held: true` — the "
                "reason has no effect, so the model is still served and still "
                "expected to pass the controls")
        if held is True and entry.get("default"):
            problems.append(
                f"{name}: is both `held` and `default: true` — the default is "
                "what Open WebUI hands every user, and this one is on disk "
                "precisely because it does not work")

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
            if isinstance(mib, bool) or not isinstance(mib, (int, float)):
                # A quoted number ("20000") is a realistic authoring mistake in
                # YAML. Without this guard `mib <= 0` below raises TypeError -
                # a traceback instead of a reported problem - the first time
                # someone quotes it.
                problems.append(
                    f"{name}: measured_mib {mib!r} is not a number — it looks "
                    "like it was written as a quoted string in the YAML")
            elif mib <= 0:
                problems.append(
                    f"{name}: measured_mib {mib} is not a measurement. A model "
                    "occupies memory, so zero means a placeholder was never "
                    "filled in from a survey run")
            elif tier == "terra" and mib >= CARD_TOTAL_MIB:
                problems.append(
                    f"{name}: measured_mib {mib} exceeds the card ({CARD_TOTAL_MIB} "
                    "MiB), which is not a possible measurement")

    if len(defaults) != 1:
        problems.append(
            f"expected exactly one entry with `default: true`, found {defaults} "
            "— Open WebUI needs one and only one default")

    # `held` models are exempt from both controls, and that exemption is the
    # only part of this state with teeth. A held model is on disk BECAUSE it is
    # known broken, so it cannot answer a control prompt or see an image —
    # requiring it to would make the gate unpassable and invite someone to
    # delete the rule rather than the entry.
    should_control = {
        e["name"] for e in roster
        if e.get("tier") == "terra" and e.get("abliterated") is True
        and e.get("role") in {"chat", "code"} and e.get("held") is not True
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

    # Same shape of drift, different control. A model declared `role: vision`
    # that nobody ever sends an image to is the worst case here, because a
    # vision model whose image half did not load answers TEXT questions
    # perfectly - it is indistinguishable from a working one until something
    # asks it to look at a picture.
    should_see = {
        e["name"] for e in roster
        if e.get("tier") == "terra" and e.get("role") == "vision"
        and e.get("held") is not True
    }
    for name in sorted(should_see - set(vision_roster)):
        problems.append(
            f"{name}: declared `role: vision` but not in vision_control.py's "
            "ROSTER — nothing would ever verify it can actually see")
    for name in sorted(set(vision_roster) - should_see):
        problems.append(
            f"{name}: in vision_control.py's ROSTER but not in the catalog as a "
            "terra vision model — the control would fail against a model nobody "
            "declared")
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
    ("measured_mib one MiB under the card total is accepted",
     [_ok_entry(measured_mib=CARD_TOTAL_MIB - 1, measured_on="2026-08-11")],
     ("example/model:1b",), None),
    ("measured_mib one MiB over the card total is flagged",
     [_ok_entry(measured_mib=CARD_TOTAL_MIB + 1, measured_on="2026-08-11")],
     ("example/model:1b",), "exceeds the card"),
    ("mbp tier above the terra card total is not flagged - different host, "
     "different memory",
     [_ok_entry(tier="mbp", measured_mib=CARD_TOTAL_MIB + 1000,
                measured_on="2026-08-11")],
     (), None),
    ("measured_mib quoted as a YAML string is a reported problem, not a crash",
     [_ok_entry(measured_mib="20000", measured_on="2026-08-11")],
     ("example/model:1b",), "is not a number"),
    ("bad date format",
     [_ok_entry(measured_mib=20000, measured_on="11/08/2026")],
     ("example/model:1b",), "YYYY-MM-DD"),
    ("abliterated terra model missing from the control script",
     [_ok_entry()], (), "not in abliteration_control.py"),
    ("control script names a model not in the catalog",
     [_ok_entry()], ("example/model:1b", "ghost:7b"), "not in the catalog"),
    # The `held` state. The exemption case is the one that matters: if a held
    # model were still required in the control roster, `make validate` could
    # never pass with one declared.
    ("a held model is exempt from the abliteration control",
     [_ok_entry(), _ok_entry(name="shelved:1b", default=False, held=True,
                             held_reason="emits a channel marker and stops")],
     ("example/model:1b",), None),
    # Inlined rather than built from _seer(), which is defined below this table
    # and would NameError at import time.
    ("a held vision model is exempt from the vision control",
     [_ok_entry(), _ok_entry(name="shelved-eye:1b", role="vision",
                             default=False, held=True,
                             held_reason="image half never loads")],
     ("example/model:1b",), None),
    ("held without a reason",
     [_ok_entry(held=True)], ("example/model:1b",), "must carry `held_reason`"),
    ("held with a whitespace-only reason",
     [_ok_entry(held=True, held_reason="  ")],
     ("example/model:1b",), "must carry `held_reason`"),
    ("held_reason written without held: true has no effect",
     [_ok_entry(held_reason="looks documented, changes nothing")],
     ("example/model:1b",), "has no effect"),
    ("held: false is still served and still controlled",
     [_ok_entry(held=False)], (), "not in abliteration_control.py"),
    ("held written as a YAML string instead of a boolean",
     [_ok_entry(held="true", held_reason="a reason")],
     ("example/model:1b",), "not a boolean"),
    ("the held model cannot also be the default",
     [_ok_entry(held=True, held_reason="known broken")],
     ("example/model:1b",), "both `held` and `default: true`"),
)


def _seer(**overrides) -> dict:
    """A minimal valid vision entry, for the vision cross-check cases.

    `default` is set because the exactly-one-default rule is global and would
    otherwise fire on every case here and mask the rule under test.
    """
    entry = _ok_entry(name="seer:1b", role="vision", abliterated=False,
                      alignment_exception="stock weights, deliberately aligned")
    entry.update(overrides)
    return entry


# The vision cross-check needs its own table because it varies a third
# argument the cases above do not. Same reasoning as SELF_CHECK_CASES: without
# these, both directions of the vision drift check could be deleted and this
# gate would still pass everything.
# Each case is (description, roster, vision_roster, expected substring or None).
VISION_SELF_CHECK_CASES = (
    ("a declared vision model that the control never asks about",
     [_seer()], (), "not in vision_control.py"),
    ("declared and controlled is fine",
     [_seer()], ("seer:1b",), None),
    ("vision control names a model the catalog does not declare",
     [_seer()], ("seer:1b", "ghost-eye:1b"),
     "not in the catalog as a terra vision model"),
    ("a non-vision model is not expected in the vision control",
     [_ok_entry()], (), None),
)


def _matching_control(roster: list[dict]) -> tuple[str, ...]:
    """The abliteration roster that would satisfy `roster`.

    Deliberately mirrors check_roster's own selection so the vision cases can
    vary ONE thing. Without it every vision case would also trip the
    abliteration cross-check and the substring assertions would pass for the
    wrong reason.
    """
    return tuple(
        e["name"] for e in roster
        if e.get("tier") == "terra" and e.get("abliterated") is True
        and e.get("role") in {"chat", "code"} and e.get("held") is not True)


def self_check() -> list[str]:
    """Prove each rule still fires. A gate must not be able to fail silently."""
    problems: list[str] = []
    for description, roster, vision, expected in VISION_SELF_CHECK_CASES:
        got = check_roster(roster, _matching_control(roster), vision)
        if expected is None:
            if got:
                problems.append(
                    f"vision self-check {description!r}: expected no problems, "
                    f"got {got}")
        elif not any(expected in p for p in got):
            problems.append(
                f"vision self-check {description!r}: expected a problem "
                f"containing {expected!r}, got {got or 'no problems at all'} — "
                "the vision drift check is not firing, so a model nobody sends "
                "an image to would pass as verified")
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


def load_control_roster(path: Path = CONTROL_PATH) -> tuple[str, ...]:
    """Read the ROSTER tuple out of a control script without importing it.

    Importing would be simpler but that script talks to the network at import
    time in future revisions; parsing the literal keeps this gate offline.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ROSTER" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise SystemExit(
        f"{path}: no ROSTER assignment found. It was renamed or removed, "
        "and this cross-check silently stopped covering anything")


def main() -> int:
    failures = self_check()
    if failures:
        print("the validator's own self-check failed:", file=sys.stderr)
        for problem in failures:
            print(f"  {problem}", file=sys.stderr)
        return 1

    problems = check_roster(load_roster(), load_control_roster(),
                            load_control_roster(VISION_CONTROL_PATH))
    if problems:
        print(f"{ROSTER_PATH.name}: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("Model roster: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
