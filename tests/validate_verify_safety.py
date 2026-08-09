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
    # The scan path is held to the same restart-free rule as the verify path,
    # for a stronger reason: a scan runs unattended at 05:30 against every host
    # at once, so a stray restart there would be both invisible and estate-wide.
    ROOT / "roles/service_vm/tasks/scan.yml",
    ROOT / "roles/service_vm/tasks/scan-benchmark.yml",
    ROOT / "roles/service_vm/tasks/scan-exposure.yml",
    ROOT / "roles/service_vm/tasks/scan-credentials.yml",
    # Verify-reachable via tasks_from: container-drift from all three VM roles'
    # verify.yml. This file's own header records having been "one file short
    # of its own premise" once already (the SAFE_TASKS gap this list exists to
    # close) — it belongs here for the same reason the rest of this list does.
    ROOT / "roles/service_vm/tasks/container-drift.yml",
]
RESTART_RE = re.compile(r"\bsystemctl\s+(?:start|restart|try-restart)\b|\bstate:\s*restarted\b")


def drift_rc_gate_problems(text: str) -> list[str]:
    """Two invariants the container-drift metrics rework depends on.

    The rc gate used to be `failed_when:` welded straight onto the drift
    `command` task, so a drifted host failed the play immediately. It is now a
    separate `assert` after the metrics-publish task instead — deliberately,
    so the chart gets a value even on the run where drift is what it needs to
    show. That restructuring only keeps its old strictness if both of these
    stay true, and a healthy host (rc 0 forever) will not make either one
    fail on its own — which is exactly the "check that reports clean because
    it never actually ran" shape this repo keeps writing down:

    1. The assert is unconditional. A `when:` added later to "soften" it
       would make the assert unable to fail the play at all, silently.
    2. The assert still comes after the publish task. Moving it back above
       (or ahead of) the publish task would re-block the metrics the moment
       drift appears — the original bug this restructuring exists to fix.
    """
    problems: list[str] = []
    publish = re.search(r"- name:\s*Publish the container drift counts as metrics", text)
    assertion = re.search(r"- name:\s*Assert no container has drifted from its Quadlet unit", text)
    if not publish or not assertion:
        problems.append(
            "container-drift.yml: could not find the publish and/or assert "
            "task by name — the invariant checks below cannot run"
        )
        return problems

    if assertion.start() <= publish.start():
        problems.append(
            "container-drift.yml: the rc assert appears at or before the "
            "publish task — a drifted run would abort before its metrics "
            "escape, the exact bug the publish-then-assert split fixed"
        )

    # The assert's own block: from its `- name:` line to the next top-level
    # `- name:` (or end of file), so a `when:` on some OTHER task cannot be
    # mistaken for one on this one.
    rest = text[assertion.start():]
    following = re.search(r"\n- name:", rest[1:])
    block = rest[: following.start() + 1] if following else rest

    if re.search(r"^\s*when:", block, re.M):
        problems.append(
            "container-drift.yml: the rc assert has a `when:` — it must be "
            "unconditional, or a healthy host (rc 0 forever) makes it "
            "impossible for this gate to ever fail the play"
        )
    if "service_vm_container_drift.rc == 0" not in block:
        problems.append(
            "container-drift.yml: the rc assert no longer checks "
            "service_vm_container_drift.rc == 0"
        )
    return problems


def main() -> int:
    failures: list[str] = []
    combined = ""
    drift_text = ""
    for task_file in SAFE_TASKS:
        text = task_file.read_text(encoding="utf-8")
        combined += text
        if task_file.name == "container-drift.yml":
            drift_text = text
        if RESTART_RE.search(text):
            failures.append(f"{task_file.relative_to(ROOT)} can start or restart a service")

    failures.extend(drift_rc_gate_problems(drift_text))

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
