#!/usr/bin/env python3
"""Exercise the deploy lock, especially the case where it must REFUSE.

Two control nodes now deploy to one estate. A second `make infra` starting
while the first is mid-play is how this repo gets a genuinely confusing
failure — half-converged units, a Quadlet rewritten under a running restart.

A lock only earns its place if it says no. On a healthy single-operator day it
succeeds every time, so the refusal is never exercised in production; left
alone, a lock that had degraded to `exit 0` would look identical to a working
one and this whole mechanism would be decoration.

Four cases, all against a temp directory:

  acquire on a free lock      exit 0     the normal path
  acquire while held          NON-ZERO   the case worth having
  the refusal names the holder           so the operator can act on it
  release then re-acquire     exit 0     the lock does not wedge itself

The third matters more than it looks: a refusal that does not say who holds it
sends the operator hunting, and at 2am they will delete the file instead.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy-lock.sh"


def run(action: str, lock: Path, holder: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), action, str(lock), holder],
        capture_output=True, text=True,
    )


def main() -> int:
    failures = []

    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "deploy.lock"

        first = run("acquire", lock, "workstation")
        if first.returncode != 0:
            failures.append(
                f"acquire on a free lock exited {first.returncode}: {first.stderr.strip()}")

        second = run("acquire", lock, "mac-control")
        if second.returncode == 0:
            failures.append(
                "acquire succeeded while the lock was held — the lock does not lock")
        combined = second.stdout + second.stderr
        if "workstation" not in combined:
            failures.append(
                "the refusal did not name the holder; got: " + combined.strip())

        released = run("release", lock, "workstation")
        if released.returncode != 0:
            failures.append(
                f"release exited {released.returncode}: {released.stderr.strip()}")

        third = run("acquire", lock, "mac-control")
        if third.returncode != 0:
            failures.append(
                f"acquire after release exited {third.returncode} — the lock wedged")

    if failures:
        print("deploy lock regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print("deploy lock: OK (4 cases, including that a held lock refuses and says who holds it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
