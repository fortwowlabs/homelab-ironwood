#!/usr/bin/env python3
"""Prove no_log/diff suppression hides rendered sentinels in all modes."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "tests/fixtures/secret-output.yml"
SENTINEL = "CODEX_SECRET_SENTINEL_9f4c7d2a"
MODES = {
    "normal": [],
    "verbose": ["-vvv"],
    "check": ["--check"],
    "diff": ["--check", "--diff"],
}


def main() -> int:
    ansible = ROOT / ".venv/bin/ansible-playbook"
    if not ansible.is_file():
        print("missing .venv/bin/ansible-playbook; run make deps-dev", file=sys.stderr)
        return 127

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="homelab-secret-output-") as directory:
        output_dir = Path(directory)
        environment = os.environ.copy()
        environment["ANSIBLE_DISPLAY_ARGS_TO_STDOUT"] = "False"
        local_temp = output_dir / "ansible-tmp"
        local_temp.mkdir()
        environment["ANSIBLE_LOCAL_TEMP"] = str(local_temp)
        for mode, arguments in MODES.items():
            rendered = output_dir / "secret.env"
            rendered.unlink(missing_ok=True)
            command = [
                str(ansible),
                "--inventory",
                "localhost,",
                str(PLAYBOOK),
                "--extra-vars",
                f"fixture_output_dir={output_dir}",
                *arguments,
            ]
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            combined_output = result.stdout + result.stderr
            if result.returncode != 0:
                failures.append(f"{mode}: fixture play exited {result.returncode}")
            if SENTINEL in combined_output:
                failures.append(f"{mode}: sentinel appeared in Ansible output")
            if mode in {"normal", "verbose"}:
                if not rendered.is_file() or SENTINEL not in rendered.read_text(encoding="utf-8"):
                    failures.append(f"{mode}: fixture did not actually render the sentinel")

    if failures:
        print("Secret output validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("Secret output suppression: normal, verbose, check, and diff OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
