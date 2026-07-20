#!/usr/bin/env python3
"""Render catalog consumers and prove every eligible app appears once."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined


ROOT = Path(__file__).resolve().parents[1]


class AttrDict(dict):
    """Dictionary with attribute access matching Ansible's templating data."""

    __getattr__ = dict.__getitem__


def as_attr(value: object) -> object:
    if isinstance(value, dict):
        return AttrDict({key: as_attr(item) for key, item in value.items()})
    if isinstance(value, list):
        return [as_attr(item) for item in value]
    return value


def dict2items(value: dict[str, object]) -> list[AttrDict]:
    return [AttrDict(key=key, value=item) for key, item in value.items()]


def comment(value: object) -> str:
    return "\n".join(f"# {line}" for line in str(value).splitlines())


def split_url(value: str, component: str) -> object:
    return getattr(urlsplit(value), component)


def main() -> int:
    document = yaml.safe_load(
        (ROOT / "inventory/group_vars/all/apps.yml").read_text(encoding="utf-8")
    )
    apps = as_attr(document["download_apps"])
    assert isinstance(apps, AttrDict)
    eligible = {name: app for name, app in apps.items() if app.proxy}

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
    common = {
        "ansible_managed": "fixture managed",
        "ansible_host": "192.0.2.31",
        "download_apps": apps,
        "hostvars": {"svc-media": AttrDict(ansible_host="192.0.2.30")},
        "lan_cidr": "192.0.2.0/24",
        "mullvad_dns": "10.64.0.1",
        "ntfy_topic": "fixture",
        "ntfy_url": "http://192.0.2.30:8080",
        "svc_gid": 10001,
        "svc_uid": 10001,
        "timezone": "Etc/UTC",
        "truenas_ip": "192.0.2.20",
        "backup_retention_days": 14,
    }

    failures: list[str] = []
    quadlet_template = environment.get_template(
        "roles/svc_download/templates/download.container.j2"
    )
    socket_template = environment.get_template(
        "roles/svc_download/templates/proxy.socket.j2"
    )
    service_template = environment.get_template(
        "roles/svc_download/templates/proxy.service.j2"
    )
    for name, app in apps.items():
        item = AttrDict(key=name, value=app)
        quadlet = quadlet_template.render(**common, item=item)
        if quadlet.count(f"ContainerName={name}") != 1:
            failures.append(f"{name}: expected exactly one generated Quadlet identity")
        if quadlet.count(f"Image={app.image}") != 1:
            failures.append(f"{name}: immutable image was not rendered exactly once")
        if bool("RequiresMountsFor=/srv/media" in quadlet) != bool(app.media_mount):
            failures.append(f"{name}: media-mount requirement did not follow the catalog")
        if app.proxy:
            socket = socket_template.render(**common, item=item)
            service = service_template.render(**common, item=item)
            if socket.count(f":{app.ui_port}") != 1:
                failures.append(f"{name}: expected one generated proxy listener")
            if service.count(f"10.77.0.2:{app.ui_port}") != 1:
                failures.append(f"{name}: expected one generated proxy target")

    backstop = environment.get_template(
        "roles/svc_download/templates/host-backstop.nft.j2"
    ).render(**common)
    firewall_match = re.search(
        r"ip saddr \$LAN_ADMIN tcp dport \{\s*([^}]+)\s*\} accept",
        backstop,
    )
    if firewall_match is None:
        failures.append("firewall: catalog port set was not rendered")
    else:
        rendered_ports = [int(port) for port in re.findall(r"\d+", firewall_match.group(1))]
        expected_ports = [app.ui_port for app in eligible.values()] + [9090]
        if sorted(rendered_ports) != sorted(expected_ports):
            failures.append("firewall: rendered ports do not match proxy-eligible apps")

    backup = environment.get_template(
        "roles/svc_download/templates/backup-dl-appdata.sh.j2"
    ).render(**common)
    backup_match = re.search(r"^for app in(.*); do$", backup, re.MULTILINE)
    expected_backup_paths = [
        path for app in apps.values() for path in app.backup_paths
    ]
    if backup_match is None or shlex.split(backup_match.group(1)) != expected_backup_paths:
        failures.append("backup: rendered membership does not match catalog backup paths")

    canary = environment.get_template(
        "roles/svc_download/templates/leak-canary.sh.j2"
    ).render(**common)
    canary_match = re.search(r"^expected=\((.*)\)$", canary, re.MULTILINE)
    if canary_match is None or shlex.split(canary_match.group(1)) != list(apps):
        failures.append("canary: rendered membership does not match the application catalog")

    verify_tasks = (ROOT / "roles/svc_download/tasks/verify.yml").read_text(encoding="utf-8")
    disruptive_tasks = (
        ROOT / "roles/svc_download/tasks/verify_disruptive.yml"
    ).read_text(encoding="utf-8")
    if "download_apps | dict2items | selectattr('value.proxy') | list" not in verify_tasks:
        failures.append("verify: UI probes are not driven by proxy-eligible catalog entries")
    if disruptive_tasks.count("download_apps | dict2items") < 2:
        failures.append("disruptive verify: capture and stop assertions are not catalog-driven")

    if failures:
        print("Generated catalog validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"Generated catalog consumers: OK ({len(apps)} applications)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
