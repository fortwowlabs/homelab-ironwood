#!/usr/bin/env python3
"""Validate the Open WebUI persona catalog before it is seeded.

A persona is a system prompt pointing at a base model. Two of its failure
modes are invisible until somebody actually uses it, which is why this gate
exists rather than letting the seeder discover them:

  * A persona whose `base_model` is not installed still CREATES cleanly and
    still appears in the dropdown. It fails on the first message, not on the
    seed, so a broken persona looks exactly like a working one until a person
    hits it.
  * A duplicated `id` silently collapses two personas into one, because the
    id is what the seeder matches on.

It also refuses a persona with an empty system prompt. That is the whole
substance of a persona -- one without it is an alias for the base model
wearing a different name, which is worse than absent because it looks
configured.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

GATE_GROUP = "catalog"

ROOT = Path(__file__).resolve().parents[1]
PERSONAS_PATH = ROOT / "inventory/group_vars/all/personas.yml"
MODELS_PATH = ROOT / "inventory/group_vars/all/models.yml"

REQUIRED = {"id", "name", "base_model", "description", "public", "system"}
OPTIONAL = {"params"}
# Open WebUI routes on this id and it appears in URLs.
ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
# Long enough that an accidentally-truncated prompt is caught. The shortest
# real persona here is ~400 characters.
MIN_SYSTEM_CHARS = 60


def check_persona(entry: object, index: int, roster: set[str]) -> list[str]:
    where = f"openwebui_personas[{index}]"
    if not isinstance(entry, dict):
        return [f"{where}: must be a mapping"]

    problems: list[str] = []
    missing = REQUIRED - set(entry)
    if missing:
        problems.append(f"{where}: missing {sorted(missing)}")
    unknown = set(entry) - REQUIRED - OPTIONAL
    if unknown:
        problems.append(f"{where}: unknown fields {sorted(unknown)}")

    persona_id = entry.get("id")
    if isinstance(persona_id, str) and not ID_RE.fullmatch(persona_id):
        problems.append(
            f"{where}: id {persona_id!r} must be lowercase letters, digits and "
            "hyphens -- it appears in URLs and is the seeder's match key"
        )

    base = entry.get("base_model")
    if isinstance(base, str) and base not in roster:
        problems.append(
            f"{where}: base_model {base!r} is not in models.yml. A persona on an "
            "absent model still creates and still appears in the dropdown -- it "
            "fails on first use, which is why this is checked here"
        )

    system = entry.get("system")
    if not isinstance(system, str) or not system.strip():
        problems.append(f"{where}: system prompt is empty; that is the whole persona")
    elif len(system.strip()) < MIN_SYSTEM_CHARS:
        problems.append(
            f"{where}: system prompt is {len(system.strip())} characters, under "
            f"{MIN_SYSTEM_CHARS} -- looks truncated rather than deliberate"
        )

    if "public" in entry and not isinstance(entry["public"], bool):
        problems.append(f"{where}: public must be a boolean, got {entry['public']!r}")

    params = entry.get("params", {})
    if not isinstance(params, dict):
        problems.append(f"{where}: params must be a mapping")
    elif "system" in params:
        problems.append(
            f"{where}: put the prompt in `system:`, not in params -- the seeder "
            "sets params.system itself and would overwrite this"
        )
    return problems


# This gate can only fail loudly if its own rules still fire. Each case is
# (description, entry, must_fail).
_ROSTER = {"model-a"}
_OK = {"id": "ok", "name": "Ok", "base_model": "model-a", "description": "d",
       "public": True, "system": "x" * 80}
SELF_CHECK_CASES = (
    ("a valid persona passes", _OK, False),
    ("absent base model caught", {**_OK, "base_model": "not-installed"}, True),
    ("empty system caught", {**_OK, "system": "   "}, True),
    ("short system caught", {**_OK, "system": "be nice"}, True),
    ("bad id caught", {**_OK, "id": "Not_Valid"}, True),
    ("missing field caught", {k: v for k, v in _OK.items() if k != "description"}, True),
    ("unknown field caught", {**_OK, "temperature": 0.7}, True),
    ("system in params caught", {**_OK, "params": {"system": "no"}}, True),
    ("non-boolean public caught", {**_OK, "public": "yes"}, True),
)


def self_check() -> list[str]:
    problems: list[str] = []
    for description, entry, must_fail in SELF_CHECK_CASES:
        failed = bool(check_persona(entry, 0, _ROSTER))
        if failed != must_fail:
            verb = "did not flag" if must_fail else "wrongly flagged"
            problems.append(
                f"self-check {description!r}: the checker {verb} it, so this gate "
                "can no longer detect the failure it exists for"
            )
    return problems


def main() -> int:
    failures = self_check()

    roster_doc = yaml.safe_load(MODELS_PATH.read_text(encoding="utf-8"))
    roster = {m["name"] for m in roster_doc.get("model_roster", []) if "name" in m}
    if not roster:
        print(f"read zero models from {MODELS_PATH.name} -- the cross-check "
              "would pass everything", file=sys.stderr)
        return 1

    doc = yaml.safe_load(PERSONAS_PATH.read_text(encoding="utf-8"))
    personas = (doc or {}).get("openwebui_personas")
    if not personas:
        print("openwebui_personas is empty or absent", file=sys.stderr)
        return 1

    seen: dict[str, int] = {}
    for index, entry in enumerate(personas):
        failures.extend(check_persona(entry, index, roster))
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            if entry["id"] in seen:
                failures.append(
                    f"openwebui_personas[{index}]: duplicate id {entry['id']!r} "
                    f"(also at [{seen[entry['id']]}]) -- the seeder matches on id, "
                    "so one would silently shadow the other"
                )
            seen[entry["id"]] = index

    if failures:
        print("Persona catalog validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"Persona catalog: OK ({len(personas)} personas, "
          f"{sum(1 for p in personas if p.get('public'))} public)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
