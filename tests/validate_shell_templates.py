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
    "roles/mon/templates/failed-units-watch.sh.j2",
    "roles/service_vm/templates/maintenance-egress.sh.j2",
    "roles/service_vm/templates/notify-failure.sh.j2",
    "roles/service_vm/templates/hc-ping.sh.j2",
    "roles/service_vm/templates/dnf-makecache-retry.sh.j2",
    "roles/svc_infra/templates/backups-fresh.sh.j2",
    "roles/svc_infra/templates/verify-run.sh.j2",
    "roles/svc_infra/templates/scan-run.sh.j2",
    "roles/svc_infra/templates/scan-images.sh.j2",
    "roles/service_vm/templates/credential-canary.sh.j2",
    "roles/pve_mon/templates/diskguard.sh.j2",
    "roles/pve_mon/templates/pve-health.sh.j2",
    "roles/pve_mon/templates/smartd-ntfy.sh.j2",
    "roles/pve_mon/templates/zed-ntfy.sh.j2",
    "roles/svc_media/templates/certwatch.sh.j2",
    "roles/svc_media/templates/heartbeat.sh.j2",
    "roles/svc_infra/templates/alert-canary.sh.j2",
    "roles/svc_infra/templates/chat-egress-probe.sh.j2",
)

# Templates that render a DIFFERENT script depending on the host, rendered a
# second time with the other context so ShellCheck sees every line that ships.
#
# credential-canary.sh.j2 is the one that matters: the base context above is a
# media host, so only the Calibre-Web probe renders and the Grafana and Mealie
# probes — a third of the file — were never checked by anything. Each override
# is merged over the base context.
EXTRA_RENDERS: tuple[tuple[str, dict], ...] = (
    (
        "roles/service_vm/templates/credential-canary.sh.j2",
        {
            "group_names": ["service_vms", "infra_vms"],
            # Documentation-range address; the infra probes curl it by IP.
            "ansible_host": "192.0.2.32",
        },
    ),
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
    minecraft_document = yaml.safe_load(
        (ROOT / "inventory/group_vars/all/minecraft.yml").read_text(encoding="utf-8")
    )
    main_vars = yaml.safe_load(
        (ROOT / "inventory/group_vars/all/main.yml").read_text(encoding="utf-8")
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
        # Ansible's `combine` (non-recursive default): shallow top-level merge,
        # right operand wins on key collisions.
        combine=lambda left, right: as_attr({**(left or {}), **(right or {})}),
    )
    context = {
        "ansible_managed": "fixture managed",
        "alert_realert_hours": 6,
        "backup_retention_days": 14,
        "cert_expiry_days": 21,
        # Read from main.yml rather than hardcoded: these two are consumed by
        # scan-images.sh.j2, and a fixture value that drifted from the real one
        # would still render and still ShellCheck clean, so the gate would pass
        # while proving nothing about the script that actually ships.
        "trivy_image": main_vars["trivy_image"],
        "trivy_cache_dir": main_vars["trivy_cache_dir"],
        # chat-egress-probe.sh.j2 publishes through the textfile collector, and
        # both of these are paths it writes to or executes. Read from main.yml
        # for the same reason as the two above: a fixture path that had drifted
        # from the real one would still render and still ShellCheck clean.
        "infra_textfile_dir": main_vars["infra_textfile_dir"],
        "infra_metric_write_bin": main_vars["infra_metric_write_bin"],
        # chat-egress-probe.sh.j2 derives the container name from the same
        # inventory fact the nft cgroup path is built from, and quotes the
        # proxy's journal namespace in its alert body.
        "chat_egress_unit": main_vars["chat_egress_unit"],
        "chat_proxy_log_namespace": main_vars["chat_proxy_log_namespace"],
        "disk_alert_threshold": 85,
        "disk_alert_nfs_threshold": 90,
        # failed-units-watch.sh.j2 branches on group membership to decide
        # whether to sweep the rootless user manager. Render the media/infra
        # arm — it is the superset, so ShellCheck sees every line.
        "group_names": ["service_vms", "media_vms"],
        # backups-fresh.sh.j2 bakes the host list in at render time.
        "groups": {"service_vms": ["svc-media", "svc-download", "svc-infra"]},
        "backup_max_age_hours": 26,
        "pve_disk_threshold": 80,
        "verify_runner_root": "/opt/homelab-iac",
        "verify_runner_secret_file": "/opt/homelab-iac/.vault_pass",
        "verify_runner_user": "svcops",
        "pve_scrub_max_age_days": 45,
        "service_domain": "example.test",
        "svc_uid": 10001,
        "download_apps": apps,
        "lan_dns": "192.0.2.1",
        # backup-infra-appdata.sh.j2 iterates these; loaded from the real
        # catalog/defaults above so the fixture can't drift from production.
        "infra_apps": as_attr(infra_apps_document["infra_apps"]),
        "infra_secret_apps": as_attr(infra_apps_document.get("infra_secret_apps", {})),
        "infra_extra_backup_paths": infra_defaults["infra_extra_backup_paths"],
        "infra_db_backups": infra_defaults["infra_db_backups"],
        # backup-media.sh.j2 iterates this; loaded from the real catalog
        # above so the fixture can't drift from production.
        "minecraft_servers": as_attr(minecraft_document["minecraft_servers"]),
        # Only the backup templates consume this; its value doesn't matter here
        # (nothing in TEMPLATES branches on which service VM it is), it just
        # needs to be defined.
        "inventory_hostname": "fixture-host",
    }

    renders = [(name, context) for name in TEMPLATES]
    renders += [
        (name, {**context, **override}) for name, override in EXTRA_RENDERS
    ]

    failures: list[str] = []
    for template_name, render_context in renders:
        rendered = environment.get_template(template_name).render(**render_context)
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
    print(
        f"Rendered shell templates: ShellCheck OK ({len(TEMPLATES)} templates, "
        f"{len(renders)} renders)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
