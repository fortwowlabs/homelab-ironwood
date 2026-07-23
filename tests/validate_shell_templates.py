#!/usr/bin/env python3
"""Render executable Jinja templates and run ShellCheck over their output."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from validate_generated_catalog import AttrDict, as_attr, comment, dict2items, split_url


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    "roles/svc_download/templates/backup-dl-appdata.sh.j2",
    "roles/svc_download/templates/leak-canary.sh.j2",
    "roles/svc_media/templates/backup-media.sh.j2",
    "roles/svc_infra/templates/backup-infra-appdata.sh.j2",
    "roles/mon/templates/disk-alert.sh.j2",
    "roles/service_vm/templates/maintenance-egress.sh.j2",
)


def main() -> int:
    shellcheck = os.environ.get("SHELLCHECK", "shellcheck")
    apps_document = yaml.safe_load(
        (ROOT / "inventory/group_vars/all/apps.yml").read_text(encoding="utf-8")
    )
    apps = as_attr(apps_document["download_apps"])
    infra_apps_document = yaml.safe_load(
        (ROOT / "inventory/group_vars/all/infra-apps.yml").read_text(encoding="utf-8")
    )
    # infra_extra_backup_paths / infra_db_backups are plain YAML lists in the
    # role defaults (no Jinja), so load them straight from there — no drift.
    infra_defaults = yaml.safe_load(
        (ROOT / "roles/svc_infra/defaults/main.yml").read_text(encoding="utf-8")
    )
    environment = Environment(
        loader=FileSystemLoader(str(ROOT)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    environment.filters.update(
        comment=comment,
        dict2items=dict2items,
        quote=shlex.quote,
        urlsplit=split_url,
    )
    context = {
        "ansible_managed": "fixture managed",
        "backup_retention_days": 14,
        "disk_alert_threshold": 85,
        "download_apps": apps,
        "lan_dns": "192.0.2.1",
        # backup-infra-appdata.sh.j2 iterates these; loaded from the real
        # catalog/defaults above so the fixture can't drift from production.
        "infra_apps": as_attr(infra_apps_document["infra_apps"]),
        "infra_extra_backup_paths": infra_defaults["infra_extra_backup_paths"],
        "infra_db_backups": infra_defaults["infra_db_backups"],
        # Only the backup templates consume this; its value doesn't matter here
        # (nothing in TEMPLATES branches on which service VM it is), it just
        # needs to be defined.
        "inventory_hostname": "fixture-host",
    }

    failures: list[str] = []
    for template_name in TEMPLATES:
        rendered = environment.get_template(template_name).render(**context)
        result = subprocess.run(
            [shellcheck, "--shell=bash", "-"],
            input=rendered,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            failures.append(f"{template_name}:\n{result.stdout}{result.stderr}")

    if failures:
        print("Rendered shell template validation failed:", file=sys.stderr)
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print(f"Rendered shell templates: ShellCheck OK ({len(TEMPLATES)} templates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
