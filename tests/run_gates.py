#!/usr/bin/env python3
"""Run every validation gate belonging to a named group.

WHY THIS EXISTS

Each `validate-*` target in the Makefile used to list its gates line by line.
That made the Makefile a conflict magnet: every branch that adds a gate appends
to the same target, so every branch conflicts there. Three branches in one week
collided on `validate-catalog` alone, always with the same trivial resolution
of keeping both lines.

Naming the group INSIDE each gate makes adding one a new file rather than a new
line, so there is nothing to collide on.

WHAT IT BUYS BEYOND THAT, WHICH MATTERS MORE

A gate that nobody invokes is a gate that silently does not run, and the old
arrangement had exactly one defence against that: remembering to add the
Makefile line. This refuses to run at all if any tests/validate_*.py declares
no group, so a gate cannot be written and then quietly left out of the build.
That check is the reason to prefer this over simply tolerating the conflicts.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE_GLOB = "tests/validate_*.py"

# Anchored at line start so a group named in prose does not satisfy it.
GATE_GROUP_RE = re.compile(r'^GATE_GROUP = "([a-z][a-z0-9-]*)"[ \t]*$', re.MULTILINE)


def discover() -> tuple[dict[str, list[Path]], list[str]]:
    """Return (group -> gates, files that declare no group)."""
    groups: dict[str, list[Path]] = {}
    ungrouped: list[str] = []
    for path in sorted(ROOT.glob(GATE_GLOB)):
        match = GATE_GROUP_RE.search(path.read_text(encoding="utf-8"))
        if match is None:
            ungrouped.append(path.relative_to(ROOT).as_posix())
            continue
        groups.setdefault(match.group(1), []).append(path)
    return groups, ungrouped


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: run_gates.py <group>", file=sys.stderr)
        return 1
    wanted = sys.argv[1]

    groups, ungrouped = discover()

    # Coverage control. A gate with no GATE_GROUP would be run by nothing, and
    # would look exactly like a passing estate. Refuse the whole build rather
    # than report a clean result for a set that is missing a member.
    if ungrouped:
        print(
            "Gate discovery failed: these declare no GATE_GROUP, so no target "
            "runs them:",
            file=sys.stderr,
        )
        for name in ungrouped:
            print(f'  {name} — add GATE_GROUP = "<group>" near its imports', file=sys.stderr)
        return 1

    gates = groups.get(wanted, [])

    # An empty group means the name is wrong or every marker was lost. Either
    # way the target would pass having run nothing, which is the failure this
    # repo keeps finding, so it is fatal.
    if not gates:
        known = ", ".join(sorted(groups)) or "(none)"
        print(
            f"Gate discovery failed: no gate declares GATE_GROUP = "
            f'"{wanted}". Known groups: {known}.',
            file=sys.stderr,
        )
        return 1

    for path in gates:
        relative = path.relative_to(ROOT).as_posix()
        # Echoed so the output still says which gate produced which line, the
        # way `make` echoing one command per gate used to.
        print(f"{sys.executable} {relative}", flush=True)
        result = subprocess.run([sys.executable, relative], cwd=ROOT, check=False)
        if result.returncode != 0:
            return result.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
