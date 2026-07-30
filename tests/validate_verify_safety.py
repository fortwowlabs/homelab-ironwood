#!/usr/bin/env python3
"""Keep safe verification restart-free and cleanup paths explicit."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# Every verify entry point reachable from verify.yml belongs here. svc_infra and
# pve_mon were missing, which left the gate one file short of its own premise:
# site.yml's comment argues that a failed play against the hypervisor takes every
# guest with it, and the hypervisor's verify path was the one not checked for
# restarts. A `state: restarted` added there would have sailed through
# `make validate` and then bounced a service on the machine hosting all three VMs
# during a routine `make verify`.
SAFE_TASKS = [
    ROOT / "roles/service_vm/tasks/verify.yml",
    ROOT / "roles/svc_download/tasks/verify.yml",
    ROOT / "roles/svc_media/tasks/verify.yml",
    ROOT / "roles/svc_infra/tasks/verify.yml",
    ROOT / "roles/mon/tasks/verify.yml",
    ROOT / "roles/pve_mon/tasks/verify.yml",
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
    download = (ROOT / "roles/svc_download/tasks/verify.yml").read_text(encoding="utf-8")
    media = (ROOT / "roles/svc_media/tasks/verify.yml").read_text(encoding="utf-8")
    disruptive = (
        ROOT / "roles/svc_download/tasks/verify_disruptive.yml"
    ).read_text(encoding="utf-8")
    if (
        "always:" not in shared
        or shared.count("service_nfs_mounts") < 2
        or "rm" not in shared
        or ".homelab-verify-{{ inventory_hostname }}" not in shared
    ):
        failures.append("shared NFS verification lacks catalogued always-cleanup paths")
    if "https://ifconfig.me" in combined:
        failures.append("download host fencing still depends on DNS resolution")
    if "https://1.1.1.1/" not in combined or "policy drop" not in combined:
        failures.append("download host fencing lacks numeric egress and nftables policy gates")
    if media.count("trap 'rm -f $p' EXIT") < 2:
        failures.append("container NFS write probes lack trap-based cleanup")
    for required in (
        "/api/health",
        "--user\n      - appuser\n      - shelfmark",
        "books_probe=/books/.homelab-verify",
        "audio_probe=/data/audiobooks/.homelab-verify",
        """trap 'rm -f "$config_probe" "$books_probe" "$audio_probe"' EXIT""",
        "podman exec shelfmark curl",
    ):
        if required not in download:
            failures.append(f"Shelfmark verification lacks {required!r}")
    for required in (
        "always:",
        "Capture VPN namespace state before the drill",
        "Capture catalog proxy socket state before the fail-closed drill",
        "Capture Recyclarr state before the fail-closed drill",
        "Pause active catalog proxy sockets during the intentional outage",
        "Attempt to restore the VPN namespace when it was previously active",
        "Attempt to restore workloads that were previously active",
        "Attempt to restore Recyclarr when it was previously active",
        "Attempt to restore proxy sockets that were previously active",
        "Attempt to restore the leak-canary timer when it was previously active",
        "Wait for any in-flight canary check",
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
