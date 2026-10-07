#!/usr/bin/env python3
"""Run the usage relay's unit tests as a validation gate.

The relay (roles/svc_infra/files/usage_relay/) is a long-running Python service,
and most of what it does is a judgement that passes silently when wrong: a
duplicate counted twice, a resume pushed as a new play, a year of history
replayed on first deploy. The tests under tests/usage_relay/ pin those
judgements; this gate is what runs them.

Positive control: zero discovered tests fails the gate. A discovery pattern
that stopped matching would otherwise report OK having run nothing.

Pass a substring to run a subset while iterating:
    .venv/bin/python tests/validate_usage_relay.py jellyfin
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Python behaviour tests, grouped with homelab-metric-write's gate: scripts
# whose refusal paths are the point and which succeed every time when healthy.
#
# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "shell"

ROOT = Path(__file__).resolve().parents[1]
SUITE_DIR = ROOT / "tests/usage_relay"


def main() -> int:
    # No __pycache__ under roles/svc_infra/files/usage_relay/ or tests/: it is
    # gitignored, but it is clutter in a directory Ansible copies from.
    sys.dont_write_bytecode = True
    loader = unittest.TestLoader()
    if len(sys.argv) > 1:
        loader.testNamePatterns = [f"*{sys.argv[1]}*"]
    suite = loader.discover(str(SUITE_DIR), pattern="test_*.py", top_level_dir=str(SUITE_DIR))
    count = suite.countTestCases()
    if count == 0:
        print("Usage relay: discovered zero tests — discovery is broken, not the suite empty",
              file=sys.stderr)
        return 1
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        return 1
    print(f"Usage relay: OK ({count} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
