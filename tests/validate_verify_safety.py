#!/usr/bin/env python3
"""Keep safe verification restart-free and cleanup paths explicit."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAFE_TASKS = [
    ROOT / "roles/service_vm/tasks/verify.yml",
    ROOT / "roles/svc_download/tasks/verify.yml",
    ROOT / "roles/svc_media/tasks/verify.yml",
    ROOT / "roles/mon/tasks/verify.yml",
]
RESTART_RE = re.compile(r"\bsystemctl\s+(?:start|restart|try-restart)\b|\bstate:\s*restarted\b")


def main() -> int:
    failures: list[str] = []
    combined = ""
    for task_file in SAFE_TASKS:
        text = task_file.read_text(encoding="utf-8")
        combined += text
        if RESTART_RE.search(text):
            failures.append(f"{task_file.relative_to(ROOT)} can start or restart a service")

    shared = (ROOT / "roles/service_vm/tasks/verify.yml").read_text(encoding="utf-8")
    media = (ROOT / "roles/svc_media/tasks/verify.yml").read_text(encoding="utf-8")
    disruptive = (
        ROOT / "roles/svc_download/tasks/verify_disruptive.yml"
    ).read_text(encoding="utf-8")
    if "always:" not in shared or "rm -f /srv/media/.homelab-verify" not in shared:
        failures.append("shared NFS verification lacks an always cleanup path")
    if media.count("trap 'rm -f $p' EXIT") < 2:
        failures.append("container NFS write probes lack trap-based cleanup")
    for required in (
        "always:",
        "Capture VPN namespace state before the drill",
        "Attempt to restore the VPN namespace when it was previously active",
        "Attempt to restore workloads that were previously active",
        "Attempt to restore the leak-canary timer when it was previously active",
        "Stop any in-flight canary check",
        "Assert every previously active service was restored",
        "verify_disruptive_inject_failure",
    ):
        if required not in disruptive:
            failures.append(f"disruptive verification lacks {required!r}")

    if failures:
        print("Verification safety validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("Verification safety: restart-free safe checks and explicit recovery OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
