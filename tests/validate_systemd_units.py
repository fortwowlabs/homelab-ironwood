#!/usr/bin/env python3
"""Validate instantiated nightly units, using systemd-analyze when available."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "contrib/systemd/homelab-verify@.service"
TIMER = ROOT / "contrib/systemd/homelab-verify@.timer"


def main() -> int:
    service = SERVICE.read_text(encoding="utf-8")
    timer = TIMER.read_text(encoding="utf-8")
    failures: list[str] = []

    required_service = (
        "User=%i",
        "WorkingDirectory=/opt/homelab-iac",
        "ExecStart=/opt/homelab-iac/.venv/bin/ansible-playbook verify.yml",
        "ConditionPathIsDirectory=/opt/homelab-iac/.venv",
    )
    required_timer = (
        "Unit=homelab-verify@%i.service",
        "Persistent=true",
        "WantedBy=timers.target",
    )
    for fragment in required_service:
        if fragment not in service:
            failures.append(f"nightly service is missing {fragment!r}")
    for fragment in required_timer:
        if fragment not in timer:
            failures.append(f"nightly timer is missing {fragment!r}")
    if "ConditionOS=" in service + timer:
        failures.append("nightly units use unsupported ConditionOS")

    systemd_analyze = shutil.which("systemd-analyze")
    if systemd_analyze:
        with tempfile.TemporaryDirectory(prefix="homelab-systemd-") as directory:
            fixture_dir = Path(directory)
            fixture_service = fixture_dir / "homelab-verify@fixture.service"
            fixture_timer = fixture_dir / "homelab-verify@fixture.timer"
            fixture_service.write_text(
                service.replace("User=%i", "User=root")
                .replace("WorkingDirectory=/opt/homelab-iac", "WorkingDirectory=/")
                .replace(
                    "ConditionPathIsDirectory=/opt/homelab-iac/.venv",
                    "ConditionPathIsDirectory=/",
                )
                .replace(
                    "ExecStart=/opt/homelab-iac/.venv/bin/ansible-playbook verify.yml "
                    "--vault-password-file .vault_pass --extra-vars notify_on_success=false",
                    "ExecStart=/bin/true",
                ),
                encoding="utf-8",
            )
            fixture_timer.write_text(timer, encoding="utf-8")
            result = subprocess.run(
                [systemd_analyze, "verify", str(fixture_service), str(fixture_timer)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                failures.append(
                    "systemd-analyze rejected nightly units: "
                    + (result.stderr.strip() or result.stdout.strip())
                )

    if failures:
        print("Systemd unit validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    suffix = " + systemd-analyze" if systemd_analyze else " (static; non-Linux host)"
    print(f"Instantiated nightly systemd units: OK{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
