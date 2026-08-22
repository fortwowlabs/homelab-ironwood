#!/usr/bin/env python3
"""Assert the image scan actually covers every image this estate runs.

`scan_images` in inventory/group_vars/all/main.yml gathers digests from eight
different variables across three files. That enumeration is hand-written, and
the way it fails is silent in the worst direction: add a `foo_image:` variable
or a new catalog and the scan does not error, does not warn, and does not scan
it. The report still says OK, with a total that is quietly missing an image.

So coverage is checked the other way round. This sweeps every `@sha256:`
reference out of the group_vars YAML by regex — a dumb, structure-independent
read that cannot be fooled by a new variable name — renders `scan_images`
against the real catalogs, and requires the two sets to match exactly.

An extra in scan_images is also a failure: it means the list names an image the
repo no longer pins, which wastes a registry round trip and reports findings for
something that is not deployed.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "catalog"


ROOT = Path(__file__).resolve().parents[1]
GROUP_VARS = ROOT / "inventory/group_vars/all"

# Files whose digests must all be covered. all_vault.yml.example is excluded:
# it is a template of placeholders, not a source of deployed images.
SOURCES = ("main.yml", "apps.yml", "infra-apps.yml", "minecraft.yml")

DIGEST_RE = re.compile(r"[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}")

# The pinned scanner is itself an image, and it is deliberately NOT scanned:
# it is the tool, not the estate, and pointing it at itself would report
# findings nobody in this repo can act on.
EXEMPT = {"trivy_image"}


def load(name: str) -> dict:
    document = yaml.safe_load((GROUP_VARS / name).read_text(encoding="utf-8"))
    return document if isinstance(document, dict) else {}


def digests_in_repo() -> set[str]:
    """Every image reference pinned anywhere in group_vars, by raw text sweep."""
    found: set[str] = set()
    for name in SOURCES:
        text = (GROUP_VARS / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            # Skip comments: several carry example or historical digests, and a
            # digest quoted in prose is documentation, not a deployed image.
            if stripped.startswith("#"):
                continue
            if any(key in line for key in EXEMPT):
                continue
            found.update(DIGEST_RE.findall(line))
    return found


def rendered_scan_images() -> tuple[set[str], str | None]:
    """Evaluate the scan_images expression against the real catalogs."""
    merged: dict = {}
    for name in SOURCES:
        merged.update(load(name))

    expression = merged.get("scan_images")
    if not isinstance(expression, str):
        return set(), "scan_images is missing from inventory/group_vars/all/main.yml"

    environment = Environment(undefined=StrictUndefined, autoescape=False)
    # Ansible's `unique` preserves first-seen order; Jinja core has no such
    # filter, so supply the same semantics rather than a set().
    environment.filters["unique"] = lambda seq: list(dict.fromkeys(seq))
    try:
        # Jinja renders a list as its Python repr, which literal_eval reads back
        # exactly. StrictUndefined means a typo'd variable name inside the
        # expression fails here rather than silently yielding a shorter list.
        text = environment.from_string(expression).render(**merged)
    except Exception as exc:  # noqa: BLE001 - surface any Jinja failure as a gate failure
        return set(), f"scan_images does not render: {exc}"

    try:
        values = ast.literal_eval(text)
    except Exception as exc:  # noqa: BLE001
        return set(), f"scan_images did not render to a list: {exc}"

    if not isinstance(values, list):
        return set(), f"scan_images rendered to {type(values).__name__}, not a list"

    # Duplicates are checked HERE, on the list, because everything downstream
    # compares sets and a set silently absorbs the bug. That is not theoretical:
    # the expression originally read `listA + listB + ... | unique | sort`, and
    # because a filter binds tighter than `+` in Jinja the dedup applied only to
    # the last list. Fifty references went to the scanner with three duplicates,
    # each scanned twice and counted twice in the totals, while this gate — set
    # based — reported a contented 47.
    duplicates = sorted({ref for ref in values if values.count(ref) > 1})
    if duplicates:
        return set(), (
            "scan_images contains duplicates, so those images would be scanned "
            "twice and their findings counted twice: " + ", ".join(duplicates)
        )

    return set(values), None


def main() -> int:
    failures: list[str] = []

    pinned = digests_in_repo()
    scanned, error = rendered_scan_images()
    if error:
        failures.append(error)

    if not failures:
        missing = sorted(pinned - scanned)
        for image in missing:
            failures.append(
                f"{image} is pinned in group_vars but is not reachable from "
                f"scan_images — it would never be scanned, and the report would "
                f"still say OK. Add its source variable to scan_images."
            )

        extra = sorted(scanned - pinned)
        for image in extra:
            failures.append(
                f"{image} is in scan_images but is pinned nowhere in group_vars "
                f"— the scan would report findings for an image this estate does "
                f"not run."
            )

    if failures:
        print("Scan image coverage validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"Scan image coverage: OK ({len(scanned)} images, all pinned digests covered)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
