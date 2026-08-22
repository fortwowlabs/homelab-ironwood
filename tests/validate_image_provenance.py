#!/usr/bin/env python3
"""Require a changed image digest to record the digest it replaced.

Every image here is pinned by immutable digest, so a bump is a one-line edit
that erases the only record of what was running before. docs/security.md has
claimed since it was written that this repo records rollback digests; until this
gate existed, nothing did, and the documented recovery story was fiction.

The convention is a comment directly above the changed line:

    # was 2026-07-31: sha256:8b8128748339583ca951af03dfe02a9a4d7363f61a216226fc28030731a5a61f
    image: "ghcr.io/corentinth/it-tools@sha256:<new>"

The important part of this gate is not that a comment exists. It is that **the
digest in the comment is the one git says was actually there before**. A gate
that only checked for the presence of a `# was` line could be satisfied by
pasting anything, which would produce a rollback record that reads convincingly
and points nowhere — worse than no record at all, because it would be trusted.

Comparison is against the point this branch left `main`, so it covers a whole
branch whether the change is committed or still in the working tree. On a clean
`main` there is nothing to compare and the gate passes trivially, which is
correct: the check belongs at the moment of the bump.

The merge base, not `main`'s tip, and that distinction is the difference
between a gate and a decoration. CLAUDE.md sends parallel work to worktrees, so
`main` moves while a branch is open. Compare against the tip and a digest that
another branch has already merged reads as "no change here" — `digests -
previously` comes out empty, the bump on THIS branch is never examined, and the
gate reports OK for a rollback record that was never written.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "catalog"


ROOT = Path(__file__).resolve().parents[1]
CATALOGS = (
    "inventory/group_vars/all/apps.yml",
    "inventory/group_vars/all/infra-apps.yml",
    "inventory/group_vars/all/main.yml",
    "inventory/group_vars/all/minecraft.yml",
)
BASE = "main"

# name@sha256:digest — the name is stable across a bump, which is what lets an
# old and a new digest be paired without parsing the YAML structure.
PINNED_RE = re.compile(r"([A-Za-z0-9._/-]+)@(sha256:[0-9a-f]{64})")
WAS_RE = re.compile(r"#\s*was\b[^\n]*?(sha256:[0-9a-f]{64})", re.IGNORECASE)

def record_above(lines: list[str], index: int) -> set[str]:
    """Digests recorded in the contiguous comment block directly above a pin.

    Bounded by the comment block rather than a fixed number of lines, and that
    matters more here than it looks. The multi-container image maps in apps.yml
    put pins on consecutive lines, so a fixed lookback lets one image's record
    be credited to its neighbour — which means a bump carrying NO record would
    pass this gate because the line above happened to belong to something else.
    A gate that fails open is worse than no gate, since it is trusted.
    """
    found: set[str] = set()
    back = index - 1
    while back >= 0 and lines[back].lstrip().startswith("#"):
        found.update(WAS_RE.findall(lines[back]))
        back -= 1
    return found


def git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def base_rev() -> str | None:
    """The commit this branch left BASE at, or None if there is no BASE.

    Falls back to BASE itself when no merge base exists — an orphan branch, or
    a repository whose HEAD is unborn. That is the behaviour this gate had
    before the merge base was introduced, so falling back can only be as weak
    as the old check, never weaker, and it keeps the gate running rather than
    quietly skipping.
    """
    if git("rev-parse", "--verify", BASE) is None:
        return None
    merge_base = git("merge-base", BASE, "HEAD")
    if merge_base is None or not merge_base.strip():
        return BASE
    return merge_base.strip()


def pins(text: str) -> dict[str, set[str]]:
    """Map each image name to every digest it is pinned at in this text."""
    found: dict[str, set[str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        # A digest quoted inside a comment is documentation or a recorded
        # rollback target, not a live pin.
        if stripped.startswith("#"):
            continue
        for name, digest in PINNED_RE.findall(line):
            found.setdefault(name, set()).add(digest)
    return found


def main() -> int:
    base = base_rev()
    if base is None:
        print(f"Image provenance: skipped (no {BASE} to compare against)")
        return 0
    # How the comparison point is described in output. Naming the short hash
    # when it is not simply `main` matters: "vs main" while actually comparing
    # against a three-week-old merge base is the kind of quietly wrong summary
    # that gets believed.
    against = BASE if base == BASE else f"{BASE}…HEAD merge-base {base[:9]}"

    failures: list[str] = []
    checked = 0

    for relative in CATALOGS:
        path = ROOT / relative
        if not path.exists():
            continue
        new_text = path.read_text(encoding="utf-8")
        old_text = git("show", f"{base}:{relative}")
        if old_text is None:
            continue  # new file on this branch; nothing it could have replaced

        old_pins = pins(old_text)
        new_pins = pins(new_text)
        new_lines = new_text.splitlines()

        for name, digests in sorted(new_pins.items()):
            previously = old_pins.get(name, set())
            if not previously:
                continue  # newly introduced image; nothing to record
            for digest in sorted(digests - previously):
                checked += 1
                index = next(
                    (
                        i
                        for i, line in enumerate(new_lines)
                        if f"{name}@{digest}" in line and not line.strip().startswith("#")
                    ),
                    None,
                )
                if index is None:
                    continue
                recorded = record_above(new_lines, index)
                short = f"{name}@{digest[:19]}…"
                if not recorded:
                    failures.append(
                        f"{relative}:{index + 1} {short} changed with no rollback "
                        f"record. Add a comment above it:\n"
                        f"      # was <date>: {sorted(previously)[0]}"
                    )
                elif not (recorded & previously):
                    failures.append(
                        f"{relative}:{index + 1} {short} records "
                        f"{sorted(recorded)[0][:26]}… as its previous digest, but "
                        f"{against} has {sorted(previously)[0][:26]}…. A rollback "
                        f"record that points at the wrong image is worse than "
                        f"none, because it will be believed."
                    )

    if failures:
        print("Image provenance validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"Image provenance: OK ({checked} digest change(s) vs {against})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
