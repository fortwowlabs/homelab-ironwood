#!/usr/bin/env python3
"""Exercise fail-closed Proxmox state decisions with non-secret fixtures."""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/pve-states.yml"
ROLE = ROOT / "roles/pve_vm/tasks/main.yml"
SIZE_MULTIPLIERS = {
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
}


def size_bytes(device: str) -> int:
    matches = re.findall(
        r"(?:^|(?<=,))size=([0-9]+(?:\.[0-9]+)?)([KMGT])(?=,|$)",
        device,
        re.IGNORECASE,
    )
    if len(matches) != 1:
        raise ValueError("disk must expose exactly one supported size")
    number, unit = matches[0]
    return int(float(number) * SIZE_MULTIPLIERS[unit.upper()])


def storage_matches(device: str, storage: str) -> bool:
    return device.startswith(f"{storage}:")


def api_result(case: dict[str, Any], defaults: dict[str, Any]) -> Any:
    """Build compact existing-VM fixtures without hiding API edge cases."""
    if "api_result" in case:
        return case["api_result"]
    if not case.get("existing"):
        raise ValueError("case needs api_result, api_error, or existing")

    vm = copy.deepcopy(defaults["existing_vm"])
    vm.update(case.get("vm_overrides", {}))
    config = vm["config"]
    config.update(case.get("config_overrides", {}))
    for key in case.get("remove_config", []):
        config.pop(key, None)
    return [vm]


def check_mode_result(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    if case.get("check_mode"):
        result["check_changed"] = bool(result["operations"])
        result["postconditions_deferred"] = result["outcome"] in {"create", "resume"}
    return result


def classify(case: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    if case.get("api_error"):
        return {"outcome": "fatal_api", "ssh_probe": False, "operations": []}

    result = api_result(case, defaults)
    if not isinstance(result, list) or len(result) > 1:
        return {"outcome": "fatal_api", "ssh_probe": False, "operations": []}
    if not result:
        qm = case.get("qm", {})
        if qm.get("rc") == 0:
            return {"outcome": "fatal_hidden", "ssh_probe": True, "operations": []}
        output = f"{qm.get('stdout', '')} {qm.get('stderr', '')}"
        vmid = re.escape(str(defaults["vmid"]))
        absent = re.search(
            rf"(?i)(configuration file .*qemu-server/{vmid}\.conf.*does not exist|"
            rf"unable to find configuration file .*qemu-server/{vmid}\.conf)",
            output,
        )
        if absent:
            return check_mode_result(
                case,
                {"outcome": "create", "ssh_probe": True, "operations": ["create"]},
            )
        return {"outcome": "fatal_qm", "ssh_probe": True, "operations": []}

    vm = result[0]
    config = vm.get("config")
    if (
        vm.get("type") != "qemu"
        or vm.get("vmid") != defaults["vmid"]
        or vm.get("name") != defaults["name"]
        or vm.get("node") != defaults["node"]
        or vm.get("pool") != defaults["pool"]
        or not isinstance(config, dict)
        or config.get("name", vm.get("name")) != defaults["name"]
    ):
        return {"outcome": "fatal_identity", "ssh_probe": False, "operations": []}

    ide2 = config.get("ide2", "")
    net0 = config.get("net0", "")
    if not storage_matches(ide2, defaults["storage"]):
        return {"outcome": "fatal_storage", "ssh_probe": False, "operations": []}
    if (
        "cloudinit" not in ide2
        or re.search(r"(?:^|,)media=cdrom(?:,|$)", ide2) is None
        or re.search(
            rf"(?:^|,)bridge={re.escape(defaults['bridge'])}(?:,|$)", net0
        )
        is None
    ):
        return {"outcome": "fatal_identity", "ssh_probe": False, "operations": []}

    for disk in ("efidisk0", "scsi0"):
        if disk in config and not storage_matches(config[disk], defaults["storage"]):
            return {"outcome": "fatal_storage", "ssh_probe": False, "operations": []}

    missing_disk = "efidisk0" not in config or "scsi0" not in config
    if missing_disk and any(re.fullmatch(r"unused[0-9]+", key) for key in config):
        return {"outcome": "fatal_unused", "ssh_probe": False, "operations": []}

    operations: list[str] = []
    if "efidisk0" not in config:
        operations.append("attach_efi")
    if "scsi0" not in config:
        operations.append("import_scsi0")
    if re.match(r"^(?:order=)?scsi0(?:[;,]|$)", config.get("boot", "")) is None:
        operations.append("set_boot")
    if "scsi0" in config:
        try:
            current = size_bytes(config["scsi0"])
        except (TypeError, ValueError):
            return {"outcome": "fatal_size", "ssh_probe": False, "operations": []}
        requested = int(defaults["requested_gib"]) * 1024**3
        if current > requested:
            return {"outcome": "fatal_size", "ssh_probe": False, "operations": []}
        if current < requested:
            operations.append("grow_scsi0")

    return check_mode_result(
        case,
        {
            "outcome": "resume" if operations else "ready",
            "ssh_probe": False,
            "operations": operations,
        },
    )


def task_by_name(document: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((task for task in document if task.get("name") == name), None)


def validate_role_contract(failures: list[str]) -> None:
    text = ROLE.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    info_tasks = [
        task
        for task in document
        if isinstance(task, dict) and "community.proxmox.proxmox_vm_info" in task
    ]
    if not info_tasks:
        failures.append("role: no proxmox_vm_info discovery tasks")
    for task in info_tasks:
        arguments = task["community.proxmox.proxmox_vm_info"]
        if arguments.get("config") != "pending":
            failures.append(f"role: {task.get('name')} must inspect pending configuration")
        if "node" in arguments:
            failures.append(f"role: {task.get('name')} must discover cluster-wide")
        if task.get("no_log") is not True or task.get("diff") is not False:
            failures.append(f"role: {task.get('name')} must suppress token output")

    api_query = task_by_name(document, "Query VM {{ vm_id }} through the scoped Proxmox API")
    if not api_query or "failed_when" in api_query or "ignore_errors" in api_query:
        failures.append("role: initial API query must propagate every module failure")

    direct_probe = task_by_name(document, "Check PVE directly for API-hidden VMID {{ vm_id }}")
    if not direct_probe or direct_probe.get("check_mode") is not False:
        failures.append("role: hidden-VM probe must execute read-only in check mode")

    task_names = [task.get("name", "") for task in document]
    for write_name in (
        "Download or revalidate the pinned Rocky image before disk import",
        "Render cloud-init snippet for validated VM {{ vm_name }}",
    ):
        if task_names.index(write_name) < task_names.index(
            "Query VM {{ vm_id }} through the scoped Proxmox API"
        ):
            failures.append(f"role: {write_name!r} occurs before fail-closed discovery")

    for name in (
        "Report absent VM creation during check mode",
        "Report partial disk completion during check mode",
        "Report boot disk growth during check mode",
    ):
        task = task_by_name(document, name)
        if not task or task.get("changed_when") is not True:
            failures.append(f"role: {name!r} must predict a check-mode change")

    for task in document:
        if "community.proxmox.proxmox_kvm" in task:
            if task.get("no_log") is not True or task.get("diff") is not False:
                failures.append(f"role: {task.get('name')} must suppress token output")

    required_fragments = (
        "proxmox_vms | type_debug == 'list'",
        "Check PVE directly for API-hidden VMID",
        "qemu-server/",
        "^unused[0-9]+$",
        "media=cdrom",
        "match('^(order=)?scsi0([;,]|$)')",
        "[KkMmGgTt]",
        "Read final VM",
        "Assert final scsi0 size matches inventory",
        "rocky_image_sha256",
        "checksum: \"sha256:{{ rocky_image_sha256 | lower }}\"",
        "hostvars[vm_name].ansible_host",
        "Attach missing EFI disk",
        "Import missing scsi0 boot disk",
        "Set resumable scsi0 boot order",
        "Grow VM {{ vm_id }} scsi0",
    )
    for fragment in required_fragments:
        if fragment not in text:
            failures.append(f"role: missing fail-closed contract fragment {fragment!r}")

    for forbidden in (
        "config: current",
        "default('root@192.168.1.10')",
        "pve_image_present",
        "vm_ip",
        "update_unsafe",
        "ignore_errors:",
        "move_disk",
        "--delete",
    ):
        if forbidden in text:
            failures.append(f"role: forbidden provisioning behavior {forbidden!r}")


def main() -> int:
    fixture = yaml.safe_load(FIXTURES.read_text(encoding="utf-8"))
    defaults = fixture["defaults"]
    failures: list[str] = []
    for case in fixture["cases"]:
        try:
            actual = classify(case, defaults)
        except (KeyError, TypeError, ValueError) as error:
            failures.append(f"{case['name']}: classifier failed: {error}")
            continue
        if actual != case["expect"]:
            failures.append(f"{case['name']}: expected {case['expect']}, got {actual}")

    validate_role_contract(failures)
    if failures:
        print("Proxmox fixture validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"Proxmox fixture states: OK ({len(fixture['cases'])} scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
