#!/usr/bin/env python3
"""Validate the download application catalog's public data contract."""

from __future__ import annotations

import re
import sys
from pathlib import Path, PurePosixPath

import yaml


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "inventory/group_vars/all/apps.yml"
REQUIRED_FIELDS = {
    "image",
    "ui_port",
    "volumes",
    "media_mount",
    "backup_paths",
    "dashboard",
    "proxy",
}
IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


def main() -> int:
    if not CATALOG_PATH.is_file():
        print(f"missing catalog: {CATALOG_PATH.relative_to(ROOT)}", file=sys.stderr)
        return 1

    document = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog = document.get("download_apps") if isinstance(document, dict) else None
    if not isinstance(catalog, dict) or not catalog:
        print("download_apps must be a non-empty mapping", file=sys.stderr)
        return 1

    failures: list[str] = []
    proxy_ports: dict[int, str] = {}
    for app_name, app in catalog.items():
        prefix = f"download_apps.{app_name}"
        if not re.fullmatch(r"[a-z][a-z0-9-]*", str(app_name)):
            failures.append(f"{prefix}: key must be a lowercase service name")
        if not isinstance(app, dict):
            failures.append(f"{prefix}: value must be a mapping")
            continue

        missing = REQUIRED_FIELDS - app.keys()
        if missing:
            failures.append(f"{prefix}: missing {', '.join(sorted(missing))}")
            continue

        image = app["image"]
        if not isinstance(image, str) or IMAGE_RE.fullmatch(image) is None:
            failures.append(f"{prefix}.image: require an OCI sha256 digest reference")

        port = app["ui_port"]
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            failures.append(f"{prefix}.ui_port: require an integer from 1 through 65535")
        elif app["proxy"] is True:
            if port in proxy_ports:
                failures.append(
                    f"{prefix}.ui_port: duplicates proxy port used by {proxy_ports[port]}"
                )
            proxy_ports[port] = str(app_name)

        volumes = app["volumes"]
        if not isinstance(volumes, list) or not volumes or not all(
            isinstance(volume, str) and ":" in volume for volume in volumes
        ):
            failures.append(f"{prefix}.volumes: require a non-empty list of source:target strings")
            volume_sources: list[str] = []
        else:
            volume_sources = [volume.split(":", 1)[0] for volume in volumes]

        if not isinstance(app["media_mount"], bool):
            failures.append(f"{prefix}.media_mount: require a boolean")
        elif app["media_mount"] != any(
            source == "/srv/media" or source.startswith("/srv/media/")
            for source in volume_sources
        ):
            failures.append(
                f"{prefix}.media_mount: must match whether a /srv/media source is mounted"
            )
        if not isinstance(app["proxy"], bool):
            failures.append(f"{prefix}.proxy: require a boolean")

        backup_paths = app["backup_paths"]
        if not isinstance(backup_paths, list) or not backup_paths or not all(
            isinstance(path, str)
            and path.strip()
            and not PurePosixPath(path).is_absolute()
            and path != "."
            and not path.startswith("-")
            and all(part not in ("", ".", "..") for part in PurePosixPath(path).parts)
            for path in backup_paths
        ):
            failures.append(f"{prefix}.backup_paths: require normalized safe relative paths")
        elif any(f"/srv/appdata/{path}" not in volume_sources for path in backup_paths):
            failures.append(
                f"{prefix}.backup_paths: every path must have a matching /srv/appdata volume"
            )

        dashboard = app["dashboard"]
        if not isinstance(dashboard, dict):
            failures.append(f"{prefix}.dashboard: require a mapping")
        else:
            for field in ("group", "icon"):
                if not isinstance(dashboard.get(field), str) or not dashboard[field].strip():
                    failures.append(f"{prefix}.dashboard.{field}: require a non-empty string")

    if failures:
        print("Download application catalog validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"Download application catalog: OK ({len(catalog)} applications)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
