#!/usr/bin/env python3
"""Render executable Jinja templates and run ShellCheck over their output."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, UndefinedError

from validate_generated_catalog import as_attr, comment, dict2items, split_url

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "shell"


ROOT = Path(__file__).resolve().parents[1]


def discover_templates() -> list[str]:
    """Every shell template in the repo, as repo-relative POSIX paths.

    Globbed rather than listed. The list this replaces had fallen four
    templates behind — including certbot-deploy-caddy.sh.j2, which nothing
    else checked at all despite running unattended in the cert renewal path —
    and nothing could report that, because a gate that silently checks a
    subset looks exactly like a gate that passes.
    """
    return sorted(
        path.relative_to(ROOT).as_posix()
        for path in ROOT.glob("roles/*/templates/*.sh.j2")
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
    # nfsguard.sh.j2's port and connect timeout live in the role defaults as
    # plain YAML (no Jinja), so load them straight from there — a hardcoded
    # fixture would still render and still ShellCheck clean if the real value
    # changed, which is the failure this file's other real-value loads exist
    # to prevent.
    pve_mon_defaults = yaml.safe_load(
        (ROOT / "roles/pve_mon/defaults/main.yml").read_text(encoding="utf-8")
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
        # str() first, matching Ansible's own filter — it is
        # `shlex.quote(to_text(a))`, not bare shlex.quote. Without the
        # coercion an int reaching `| quote` raises TypeError here while
        # working perfectly on a real deploy, which makes this gate fail on
        # templates that are correct. container-drift.sh.j2 does exactly that
        # with `{{ svc_uid | quote }}`; it went unnoticed only because no
        # hand-listed template passed a non-string through the filter.
        quote=lambda value: shlex.quote(str("" if value is None else value)),
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
        # nfsguard.sh.j2 watches the TrueNAS export and can bounce that VM by
        # id, so both identities are read from the real inventory rather than
        # invented here — same reasoning as trivy_image and infra_textfile_dir
        # above.
        "truenas_ip": main_vars["truenas_ip"],
        "truenas_vmid": main_vars["truenas_vmid"],
        "nfsguard_port": pve_mon_defaults["nfsguard_port"],
        "nfsguard_connect_timeout": pve_mon_defaults["nfsguard_connect_timeout"],
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
        # (no discovered template branches on which service VM it is), it just
        # needs to be defined.
        "inventory_hostname": "fixture-host",
    }

    templates = discover_templates()
    if not templates:
        print(
            "Shell template discovery found nothing under roles/*/templates/"
            "*.sh.j2. A gate that checks zero templates passes while proving "
            "nothing; refusing to report success.",
            file=sys.stderr,
        )
        return 1

    renders = [(name, context) for name in templates]
    renders += [
        (name, {**context, **override}) for name, override in EXTRA_RENDERS
    ]

    failures: list[str] = []
    for template_name, render_context in renders:
        try:
            rendered = environment.get_template(template_name).render(**render_context)
        except UndefinedError as error:
            # The coverage control. A newly added template whose variables are
            # not in the context above lands here rather than being skipped, so
            # adding one without wiring it fails the build. That is the whole
            # point of discovering templates instead of listing them.
            failures.append(
                f"{template_name}: render failed — {error}.\n"
                "    This template is discovered by glob but its variables are "
                "not in the fixture context.\n"
                "    Add them to `context` in this file (reading real values "
                "from group_vars or role defaults),\n"
                "    rather than adding a `| default(...)` to the template."
            )
            continue
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
        f"Rendered shell templates: ShellCheck OK ({len(templates)} discovered, "
        f"{len(renders)} renders)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
