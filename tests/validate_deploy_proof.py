#!/usr/bin/env python3
"""Exercise deploy_proof's verdict logic, especially the ways it must FAIL.

scripts/deploy_proof.py decides whether a deploy proved that what is running
equals what is committed. On a healthy estate its answer is always "clean",
which makes it exactly the kind of check that can stop working without anyone
noticing — the failure this repo keeps writing down.

So the interesting cases are exercised here, offline, against recorded output:

  clean         nothing changed anywhere                            -> 0
  infra-sync    only the three known .deployed-rev sync tasks       -> 0
  drift         the sync trio PLUS a real change                    -> 1
  partial-sync  two of the three sync tasks and nothing else        -> 1
  truncated     the deploy never reached PLAY RECAP                 -> 2

`drift` is the one that earns this file. CLAUDE.md warns against papering over
a genuine diff by deploying twice and quoting the second number; a tool that
allowlisted the trio by COUNT rather than by name would do precisely that, and
would pass the infra-sync fixture while waving the drift one through.

`partial-sync` is the subtler half of the same idea. Two of the three sync
tasks is not "less drift" — the block is gated on one condition and either runs
whole or not at all, so a subset means something happened that nobody has
reasoned about. It must fail rather than be treated as a smaller version of a
known-good state.

`truncated` is the third: a deploy that died has proved nothing, and must not
be reported as the same thing as a deploy that changed nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from deploy_proof import NoRecapError, parse_changed, verdict  # noqa: E402

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "ci"

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/deploy-proof"
INFRA_HOST = "svc-infra"

# fixture -> (expected exit code, a fragment the message must mention)
CASES = (
    ("clean.log", 0, "clean"),
    ("infra-sync.log", 0, "runner checkout"),
    ("drift.log", 1, "Authelia"),
    ("partial-sync.log", 1, "Record the deployed revision"),
    ("truncated.log", 2, "PLAY RECAP"),
)


def main() -> int:
    problems: list[str] = []

    # Positive control. Every check under this repo's gates has failed once by
    # returning a clean result it had not earned; an empty fixture directory
    # would make this gate report OK having exercised nothing.
    present = sorted(path.name for path in FIXTURES.glob("*.log"))
    expected = sorted(name for name, _, _ in CASES)
    if present != expected:
        print(
            f"fixture mismatch: {FIXTURES.relative_to(ROOT)} holds {present}, "
            f"this gate expects {expected}. A fixture that is missing is a case "
            f"nobody runs.",
            file=sys.stderr,
        )
        return 1

    for name, expected_code, expected_fragment in CASES:
        text = (FIXTURES / name).read_text(encoding="utf-8")
        try:
            changed = parse_changed(text)
        except NoRecapError as error:
            code, message = 2, str(error)
        else:
            code, message = verdict(changed, INFRA_HOST)

        if code != expected_code:
            problems.append(
                f"{name}: expected exit {expected_code}, got {code} — {message}"
            )
        elif expected_fragment.lower() not in message.lower():
            problems.append(
                f"{name}: exit {code} was right but the message did not mention "
                f"{expected_fragment!r}: {message}"
            )

    if problems:
        print("deploy_proof verdict validation failed:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    failing = sum(1 for _, code, _ in CASES if code != 0)
    print(f"deploy_proof: OK ({len(CASES)} cases, {failing} of them must fail)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
