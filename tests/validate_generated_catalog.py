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
    """Dictionary with attribute access matching Ansible's templating data.

    Missing attributes raise AttributeError (not KeyError) so Jinja's normal
    getattr-then-getitem fallback produces Undefined, matching how Ansible
    renders `item.value.optional_field | default(...)` against a plain dict.
    """

    def __getattr__(self, name: str) -> object:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


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
    caddy_services = as_attr(
        {
            "home": {
                "backend": "192.0.2.32:3000",
                "group": "Infra",
                "host_header": "localhost:3000",
                "icon": "homepage",
                "scheme": "https",
                "tls_skip_verify": True,
            },
            "jellyfin": {
                "backend": "192.0.2.30:8096",
                "group": "Media",
                "icon": "jellyfin",
            },
        }
    )
    common = {
        "ansible_managed": "fixture managed",
        "ansible_host": "192.0.2.31",
        "caddy_services": caddy_services,
        "download_apps": apps,
        "hostvars": {
            "svc-download": AttrDict(ansible_host="192.0.2.31"),
            "svc-media": AttrDict(ansible_host="192.0.2.30"),
            "svc-infra": AttrDict(ansible_host="192.0.2.32"),
            "thurgadin": AttrDict(pve_api_host="192.0.2.10"),
        },
        "download_host": "svc-download",
        "media_host": "svc-media",
        "infra_host": "svc-infra",
        "beszel_agent_enabled": False,
        "inventory_hostname": "svc-download",
        "lan_cidr": "192.0.2.0/24",
        "mullvad_dns": "10.64.0.1",
        "ntfy_topic": "fixture",
        "ntfy_url": "http://192.0.2.30:8080",
        "svc_gid": 10001,
        "svc_uid": 10001,
        "service_domain": "fixture.invalid",
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

    # Prometheus (on svc-infra) scrapes this host's node_exporter, so the
    # backstop has to admit exactly that one source on 9100 — the rule is
    # single-source by design, not lan_cidr, and nothing else asserted it.
    infra_host_ip = common["hostvars"]["svc-infra"]["ansible_host"]
    if not re.search(
        rf"^define INFRA_HOST = {re.escape(infra_host_ip)}\s*(#.*)?$", backstop, re.MULTILINE
    ):
        failures.append("firewall: INFRA_HOST was not defined from the svc-infra inventory address")
    if not re.search(r"^\s*ip saddr \$INFRA_HOST tcp dport 9100 accept$", backstop, re.MULTILINE):
        failures.append("firewall: node_exporter scrape rule was not rendered for INFRA_HOST")

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
    catalog_file_tasks = (
        ROOT / "roles/svc_download/tasks/files.yml"
    ).read_text(encoding="utf-8")
    download_main_tasks = (
        ROOT / "roles/svc_download/tasks/main.yml"
    ).read_text(encoding="utf-8")
    image_tasks = (
        ROOT / "roles/svc_download/tasks/images.yml"
    ).read_text(encoding="utf-8")
    backup_tasks = (
        ROOT / "roles/svc_download/tasks/backup.yml"
    ).read_text(encoding="utf-8")
    catalog_handlers = (
        ROOT / "roles/svc_download/handlers/main.yml"
    ).read_text(encoding="utf-8")
    catalog_app_tasks = (
        ROOT / "roles/svc_download/tasks/apps.yml"
    ).read_text(encoding="utf-8")
    download_package_tasks = (
        ROOT / "roles/svc_download/tasks/packages.yml"
    ).read_text(encoding="utf-8")
    if "download_apps | dict2items | selectattr('value.proxy') | list" not in verify_tasks:
        failures.append("verify: UI probes are not driven by proxy-eligible catalog entries")
    if disruptive_tasks.count("download_apps | dict2items") < 2:
        failures.append("disruptive verify: capture and stop assertions are not catalog-driven")
    for stale_fact in (
        "download_stale_quadlet_paths",
        "download_stale_proxy_socket_paths",
        "download_stale_proxy_service_paths",
    ):
        if stale_fact not in catalog_file_tasks:
            failures.append(f"removal convergence: missing {stale_fact}")
    if download_main_tasks.index("images.yml") > download_main_tasks.index("jail.yml"):
        failures.append("image acquisition must precede the jail handler flush")
    if "not ansible_check_mode" not in image_tasks:
        failures.append("image acquisition can mutate during check mode")
    if "Restore strict backstop after image pulls" not in image_tasks:
        failures.append("image acquisition lacks an always-close backstop path")
    if "value.backup_paths" not in backup_tasks:
        failures.append("initial backup artifact gates are not catalog-driven")
    if catalog_handlers.count("when: not ansible_check_mode") < 3:
        failures.append("catalog restart handlers are not guarded during check mode")
    if catalog_app_tasks.count("when: not (ansible_check_mode and item.changed)") < 2:
        failures.append("new catalog units are not skipped safely during check mode")
    if "masked: true" not in download_package_tasks or "dnf-makecache.timer" not in download_package_tasks:
        failures.append("download packages: automatic DNF refresh is not masked")
    if "reset-failed" not in download_package_tasks or download_package_tasks.count("dnf-makecache.timer") < 2:
        failures.append("download packages: stale automatic DNF timer failures are not cleared")
    if "Verify automatic DNF metadata refresh remains masked" not in verify_tasks:
        failures.append("download verify: automatic DNF refresh masking is not checked")

    caddyfile = environment.get_template(
        "roles/svc_media/templates/Caddyfile.j2"
    ).render(**common)
    dnsmasq = environment.get_template(
        "roles/svc_media/templates/dnsmasq-services.conf.j2"
    ).render(**common)
    expected_proxy_names = [*caddy_services, *eligible]
    for name in expected_proxy_names:
        hostname = f"{name}.{common['service_domain']}"
        if caddyfile.count(f"@{name} host {hostname}") != 1:
            failures.append(f"{name}: expected one generated Caddy host matcher")
        if caddyfile.count(f"handle @{name} {{") != 1:
            failures.append(f"{name}: expected one generated Caddy handle")
        if dnsmasq.count(f"address=/{hostname}/{common['ansible_host']}") != 1:
            failures.append(f"{name}: expected one generated dnsmasq address")
    if "local_certs" in caddyfile:
        failures.append("caddy: obsolete internal-CA configuration was rendered")
    if (
        "tls /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem"
        not in caddyfile
    ):
        failures.append("caddy: file-backed wildcard certificate was not rendered")
    if "reverse_proxy https://192.0.2.32:3000" not in caddyfile:
        failures.append("caddy: per-service upstream scheme was not preserved")
    if "tls_insecure_skip_verify" not in caddyfile:
        failures.append("caddy: per-service TLS transport option was not preserved")
    if "header_up Host localhost:3000" not in caddyfile:
        failures.append("caddy: per-service Host header override was not preserved")
    if re.search(r"reverse_proxy [^\n]+ \{\n\s*\}", caddyfile):
        failures.append("caddy: simple upstreams rendered unnecessary empty blocks")
    if "certificate with a Cloudflare DNS-01 challenge.\n{" not in caddyfile:
        failures.append("caddy: global block is separated from its header by a formatter diff")

    media_defaults = yaml.safe_load(
        (ROOT / "roles/svc_media/defaults/main.yml").read_text(encoding="utf-8")
    )
    media_catalog = media_defaults.get("media_quadlet_catalog", [])
    media_catalog_names = [item.get("name") for item in media_catalog]
    media_catalog_sources = [item.get("src") for item in media_catalog]
    if len(media_catalog_names) != len(set(media_catalog_names)):
        failures.append("media catalog: service names are not unique")
    if not media_catalog_names or any(not source for source in media_catalog_sources):
        failures.append("media catalog: every default entry needs a name and template source")
    media_file_tasks = (
        ROOT / "roles/svc_media/tasks/files.yml"
    ).read_text(encoding="utf-8")
    media_verify_tasks = (
        ROOT / "roles/svc_media/tasks/verify.yml"
    ).read_text(encoding="utf-8")
    if "media_quadlet_catalog:" in media_file_tasks:
        failures.append("media catalog: runtime task fact breaks standalone verification")
    if "media_quadlet_catalog | map(attribute='name')" not in media_verify_tasks:
        failures.append("media catalog: verification is not driven by the role default")
    if "/opt/homelab/appdata/romm/mysql" not in media_file_tasks:
        failures.append("media storage: RomM database directory is not managed")
    if "media_romm_db_owner.stdout | trim" not in media_file_tasks:
        failures.append("media storage: RomM database ownership is not idempotently gated")
    if "media_romm_db_owner_gate.stdout | trim" not in media_verify_tasks:
        failures.append("media storage: RomM database ownership is not verified")

    homepage = environment.get_template(
        "roles/svc_media/templates/homepage/services.yaml.j2"
    ).render(**common)
    homepage_groups = yaml.safe_load(homepage)
    homepage_names = [
        service_name
        for group in homepage_groups
        for services in group.values()
        for service in services
        for service_name in service
    ]
    for name in eligible:
        if homepage_names.count(name) != 1:
            failures.append(f"{name}: expected one generated dashboard entry")

    if failures:
        print("Generated catalog validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"Generated catalog consumers: OK ({len(apps)} applications)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
