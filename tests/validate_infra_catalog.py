#!/usr/bin/env python3
"""Validate the svc-infra application catalogs' public data contract.

The download catalog has had validate_catalog.py since it was written; the
infra catalogs (infra_apps + infra_secret_apps) grew to a dozen services with
no equivalent gate, so a duplicate host port or a missing field only surfaced
as a podman bind failure mid-deploy. This is that gate.

Two infra-specific rules beyond the download catalog's schema:
  * The two catalogs are merged with combine() (last-wins) all over
    roles/svc_infra, so a name in both would silently shadow the infra_apps
    entry everywhere. They must be disjoint.
  * infra_apps is rendered by a task that is deliberately NOT no_log (so the
    ordinary catalog keeps a readable diff), therefore a vault reference in a
    plain entry's env would print the secret in Ansible output. Anything
    carrying a vault_* value belongs in infra_secret_apps.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path, PurePosixPath

import yaml


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "inventory/group_vars/all/infra-apps.yml"
TEMPLATE_DIR = ROOT / "roles/svc_infra/templates"
DEFAULTS_PATH = ROOT / "roles/svc_infra/defaults/main.yml"
FILES_TASKS_PATH = ROOT / "roles/svc_infra/tasks/files.yml"
VERIFY_TASKS_PATH = ROOT / "roles/svc_infra/tasks/verify.yml"
APPDATA_ROOT = "/opt/homelab/appdata"

REQUIRED_FIELDS = {"image", "ui_port", "volumes", "backup_paths"}
OPTIONAL_FIELDS = {
    "env",
    "extra_ports",
    "network_mode",
    "container_port",
    "user",
    "shm_size",
    "require_vault",
}
IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
VAULT_REF_RE = re.compile(r"vault_[a-z0-9_]+")
# Bespoke (non-catalog) services publish fixed host ports in their own
# Quadlets. Parsed rather than hardcoded so a new bespoke service is covered
# the day it lands: "PublishPort={{ ansible_host }}:8000:8000".
BESPOKE_PORT_RE = re.compile(r"^PublishPort=\{\{ ansible_host \}\}:(\d+):", re.MULTILINE)


def bespoke_published_ports() -> dict[int, str]:
    """Map each literal host port published by a bespoke Quadlet to its file."""
    ports: dict[int, str] = {}
    for template in sorted(TEMPLATE_DIR.glob("*.container.j2")):
        for match in BESPOKE_PORT_RE.finditer(template.read_text(encoding="utf-8")):
            ports.setdefault(int(match.group(1)), template.name)
    return ports


def check_app(
    prefix: str,
    app: object,
    *,
    secret_catalog: bool,
    claim_port,
) -> list[str]:
    """Validate one catalog entry; return its failures."""
    failures: list[str] = []
    if not isinstance(app, dict):
        return [f"{prefix}: value must be a mapping"]

    missing = REQUIRED_FIELDS - app.keys()
    if missing:
        return [f"{prefix}: missing {', '.join(sorted(missing))}"]

    unknown = app.keys() - REQUIRED_FIELDS - OPTIONAL_FIELDS
    if unknown:
        failures.append(f"{prefix}: unknown field(s) {', '.join(sorted(unknown))}")

    image = app["image"]
    if not isinstance(image, str) or IMAGE_RE.fullmatch(image) is None:
        failures.append(f"{prefix}.image: require an OCI sha256 digest reference")

    port = app["ui_port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        failures.append(f"{prefix}.ui_port: require an integer from 1 through 65535")
    else:
        failures.extend(claim_port(port, "tcp", f"{prefix}.ui_port"))

    for field in ("container_port",):
        if field in app:
            value = app[field]
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
                failures.append(f"{prefix}.{field}: require an integer from 1 through 65535")

    if "extra_ports" in app:
        extra_ports = app["extra_ports"]
        if not isinstance(extra_ports, list) or not extra_ports:
            failures.append(f"{prefix}.extra_ports: require a non-empty list")
        else:
            for index, entry in enumerate(extra_ports):
                where = f"{prefix}.extra_ports[{index}]"
                if not isinstance(entry, dict) or "port" not in entry or "protocol" not in entry:
                    failures.append(f"{where}: require a mapping with port and protocol")
                    continue
                protocol = entry["protocol"]
                if protocol not in ("tcp", "udp"):
                    failures.append(f"{where}.protocol: require tcp or udp")
                value = entry["port"]
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or not 1 <= value <= 65535
                ):
                    failures.append(f"{where}.port: require an integer from 1 through 65535")
                elif protocol in ("tcp", "udp"):
                    # Same port number on tcp and udp is a distinct binding
                    # (syncthing publishes 22000 on both), so claims are keyed
                    # by protocol too.
                    failures.extend(claim_port(value, protocol, where))

    if "network_mode" in app and app["network_mode"] != "host":
        failures.append(f"{prefix}.network_mode: only 'host' is supported")

    for field in ("user", "shm_size"):
        if field in app and (not isinstance(app[field], str) or not app[field].strip()):
            failures.append(f"{prefix}.{field}: require a non-empty string")

    volumes = app["volumes"]
    if not isinstance(volumes, list) or not all(
        isinstance(volume, str) and ":" in volume for volume in volumes
    ):
        failures.append(f"{prefix}.volumes: require a list of source:target strings")
        volume_sources: list[str] = []
    else:
        volume_sources = [volume.split(":", 1)[0] for volume in volumes]

    backup_paths = app["backup_paths"]
    if not isinstance(backup_paths, list) or not all(
        isinstance(path, str)
        and path.strip()
        and not PurePosixPath(path).is_absolute()
        and path != "."
        and not path.startswith("-")
        and all(part not in ("", ".", "..") for part in PurePosixPath(path).parts)
        for path in backup_paths
    ):
        failures.append(f"{prefix}.backup_paths: require normalized safe relative paths")
    elif any(
        not any(
            source == f"{APPDATA_ROOT}/{path}" or source.startswith(f"{APPDATA_ROOT}/{path}/")
            for source in volume_sources
        )
        for path in backup_paths
    ):
        failures.append(
            f"{prefix}.backup_paths: every path must have a matching {APPDATA_ROOT} volume"
        )

    env = app.get("env", {})
    if "env" in app and not isinstance(env, dict):
        failures.append(f"{prefix}.env: require a mapping")
        env = {}
    vault_refs = sorted(
        {
            ref
            for value in env.values()
            for ref in VAULT_REF_RE.findall(str(value))
        }
    )

    if secret_catalog:
        require_vault = app.get("require_vault")
        if not isinstance(require_vault, list) or not require_vault or not all(
            isinstance(name, str) and name.startswith("vault_") for name in require_vault
        ):
            failures.append(
                f"{prefix}.require_vault: require a non-empty list of vault_* variable names"
            )
        else:
            undeclared = sorted(set(vault_refs) - set(require_vault))
            if undeclared:
                failures.append(
                    f"{prefix}.require_vault: does not cover {', '.join(undeclared)} "
                    "(the preflight assert would not catch an empty value)"
                )
    else:
        if "require_vault" in app:
            failures.append(
                f"{prefix}.require_vault: only infra_secret_apps entries may declare this"
            )
        if vault_refs:
            failures.append(
                f"{prefix}.env: references {', '.join(vault_refs)} — a vault-bearing service "
                "must live in infra_secret_apps (the infra_apps render is not no_log)"
            )

    return failures


def main() -> int:
    if not CATALOG_PATH.is_file():
        print(f"missing catalog: {CATALOG_PATH.relative_to(ROOT)}", file=sys.stderr)
        return 1

    document = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        print("infra-apps.yml must be a mapping", file=sys.stderr)
        return 1

    plain = document.get("infra_apps")
    secret = document.get("infra_secret_apps") or {}
    if not isinstance(plain, dict) or not plain:
        print("infra_apps must be a non-empty mapping", file=sys.stderr)
        return 1
    if not isinstance(secret, dict):
        print("infra_secret_apps must be a mapping", file=sys.stderr)
        return 1

    failures: list[str] = []
    claimed: dict[tuple[int, str], str] = {
        (port, "tcp"): f"bespoke Quadlet {name}"
        for port, name in bespoke_published_ports().items()
    }

    def claim_port(port: int, protocol: str, where: str) -> list[str]:
        """Reserve a host port/protocol, reporting who already holds it."""
        key = (port, protocol)
        if key in claimed:
            return [
                f"{where}: host port {port}/{protocol} already published by {claimed[key]}"
            ]
        claimed[key] = where
        return []

    duplicates = sorted(plain.keys() & secret.keys())
    if duplicates:
        failures.append(
            "infra_apps and infra_secret_apps both declare "
            f"{', '.join(duplicates)} — combine() would silently shadow the infra_apps entry"
        )

    defaults = yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8"))
    namespaced_appdata = defaults.get("infra_namespaced_appdata", [])
    expected_namespaced_paths = {
        "/opt/homelab/appdata/netbox-redis",
        "/opt/homelab/appdata/immich-db",
    }
    if {item.get("path") for item in namespaced_appdata} != expected_namespaced_paths:
        failures.append("infra namespaced appdata: required bind mounts are not catalogued")
    if any(not item.get("restart_services") for item in namespaced_appdata):
        failures.append("infra namespaced appdata: every bind mount needs restart consumers")

    file_tasks = FILES_TASKS_PATH.read_text(encoding="utf-8")
    verify_tasks = VERIFY_TASKS_PATH.read_text(encoding="utf-8")
    if "infra_namespaced_appdata_chowns.results" not in file_tasks:
        failures.append("infra namespaced appdata: ownership repairs do not drive restarts")
    if "Verify namespaced infra appdata ownership" not in verify_tasks:
        failures.append("infra namespaced appdata: ownership is not verified")

    for catalog_name, catalog in (("infra_apps", plain), ("infra_secret_apps", secret)):
        for app_name, app in catalog.items():
            prefix = f"{catalog_name}.{app_name}"
            if not re.fullmatch(r"[a-z][a-z0-9-]*", str(app_name)):
                failures.append(f"{prefix}: key must be a lowercase service name")
            failures.extend(
                check_app(
                    prefix,
                    app,
                    secret_catalog=catalog_name == "infra_secret_apps",
                    claim_port=claim_port,
                )
            )

    if failures:
        print("Infra application catalog validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(
        f"Infra application catalogs: OK ({len(plain)} plain, {len(secret)} secret-bearing)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
