#!/usr/bin/env python3
"""Exercise all three verdicts of the Ollama binding check.

The check answers one question: is Ollama reachable on the tailnet, where it
would be unauthenticated to every peer? The naive form — "curl the tailnet
address, pass if it does not answer" — passes for the wrong reason far more
often than the right one. A stopped Ollama passes it. An unplugged USB-C
adapter passes it. TERRA switched off passes it, and TERRA is off half the
time.

So the LAN address must answer FIRST. Only against a service that is
demonstrably alive does a silent tailnet address mean anything.

Three cases, against a stub curl:

  LAN answers, tailnet silent    verdict=ok             correctly scoped
  LAN answers, tailnet answers   verdict=exposed        the finding
  LAN silent                     verdict=inconclusive   could not look

The third is the one this exists for. Before the credential probes got a
three-state verdict in 057e1e4, a connection refused rendered as the word
`ok`, and a check that cannot tell "clean" from "I could not look" is a check
nobody can tell is broken.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "roles/mac_control/templates/ollama-binding-check.sh.j2"

LAN = "192.168.1.41"
TAILNET = "100.64.0.41"


def render() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("{{ ansible_managed | comment }}", "# (rendered for tests)")
    text = re.sub(r"\{\{\s*mac_control_ollama_port\s*\}\}", "11434", text)
    leftover = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", text)
    if leftover:
        raise SystemExit(
            "ollama-binding-check.sh.j2 grew Jinja this test does not render: "
            + ", ".join(sorted(set(leftover))))
    return text


def stub_curl(directory: Path, answering: set[str]) -> None:
    """A curl that succeeds only for addresses in `answering`."""
    hosts = " ".join(sorted(answering))
    script = f"""#!/usr/bin/env bash
for arg in "$@"; do
  for host in {hosts}; do
    case "$arg" in
      *"$host"*) echo '{{"models":[]}}'; exit 0 ;;
    esac
  done
done
exit 7
"""
    path = directory / "curl"
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


CASES = [
    ("LAN answers, tailnet silent", {LAN}, "ok", 0),
    ("LAN answers, tailnet answers", {LAN, TAILNET}, "exposed", 1),
    ("LAN silent", set(), "inconclusive", 2),
]


def main() -> int:
    rendered = render()
    failures = []

    for name, answering, want_verdict, want_rc in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            stub_curl(tmpdir, answering)
            script = tmpdir / "check.sh"
            script.write_text(rendered, encoding="utf-8")

            env = dict(os.environ, PATH=f"{tmpdir}:{os.environ['PATH']}")
            completed = subprocess.run(
                ["bash", str(script), LAN, TAILNET],
                capture_output=True, text=True, env=env,
            )
            out = completed.stdout + completed.stderr
            if f"verdict={want_verdict}" not in out:
                failures.append(
                    f"{name}: expected verdict={want_verdict}, got: {out.strip()!r}")
            if completed.returncode != want_rc:
                failures.append(
                    f"{name}: expected exit {want_rc}, got {completed.returncode}")

    if failures:
        print("ollama binding check regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print("ollama binding check: OK (3 cases, including that a dead service "
          "reads as inconclusive rather than ok)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
