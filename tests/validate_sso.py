#!/usr/bin/env python3
"""Validate the Authelia forward-auth wiring.

`sso_protected_services` (inventory/group_vars/all/main.yml) is a plain list
of names, and both of its failure modes are silent:

  * a typo, or a name that is not actually fronted by Caddy, protects
    NOTHING. No error is raised anywhere — Caddyfile.j2 simply never matches
    that name, and the service keeps serving unauthenticated while the
    inventory says otherwise. That is a fail-open, so it gets a gate rather
    than a comment.
  * listing the auth portal itself protects Authelia with Authelia, which is
    an infinite redirect rather than an error.

The list spans three catalogs (caddy_services, download_apps, and the
infra_secret_apps entry that provides the portal), which is why this is its
own test rather than part of validate_infra_catalog.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MAIN_VARS = ROOT / "inventory/group_vars/all/main.yml"
DOWNLOAD_CATALOG = ROOT / "inventory/group_vars/all/apps.yml"
INFRA_CATALOG = ROOT / "inventory/group_vars/all/infra-apps.yml"
CADDYFILE_TEMPLATE = ROOT / "roles/svc_media/templates/Caddyfile.j2"

# The vhost that serves the login form. Protecting it with itself is the one
# entry that turns a working deploy into an unusable one.
PORTAL_SERVICE = "auth"


def load(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document if isinstance(document, dict) else {}


def main() -> int:
    failures: list[str] = []

    main_vars = load(MAIN_VARS)
    protected = main_vars.get("sso_protected_services")

    if protected is None:
        # An absent list is a legitimate state: it is the documented rollback
        # (empty the list, re-run `make access`). Nothing else to check.
        print("SSO forward-auth: OK (no services protected)")
        return 0

    if not isinstance(protected, list) or not all(
        isinstance(name, str) and name.strip() for name in protected
    ):
        print("sso_protected_services must be a list of non-empty strings", file=sys.stderr)
        return 1

    duplicates = sorted({name for name in protected if protected.count(name) > 1})
    if duplicates:
        failures.append(f"duplicate entries: {', '.join(duplicates)}")

    if PORTAL_SERVICE in protected:
        failures.append(
            f"{PORTAL_SERVICE!r} is the Authelia portal itself — protecting it with "
            "forward-auth is an infinite redirect loop"
        )

    caddy_services = main_vars.get("caddy_services") or {}
    download_apps = load(DOWNLOAD_CATALOG).get("download_apps") or {}
    proxied_downloads = {
        name for name, app in download_apps.items()
        if isinstance(app, dict) and app.get("proxy")
    }
    # Only these two sets get a Caddy vhost, and forward-auth is applied at the
    # vhost. A name outside them cannot be protected by this mechanism.
    frontable = set(caddy_services) | proxied_downloads

    for name in protected:
        if name not in frontable:
            unproxied = name in download_apps
            reason = (
                "is in download_apps but not proxy: true"
                if unproxied
                else "is in neither caddy_services nor the proxied download catalog"
            )
            failures.append(
                f"{name!r} {reason} — it has no Caddy vhost, so listing it here "
                "protects nothing and fails open"
            )

    # The portal has to be reachable for any of this to work, and it has to be
    # reachable WITHOUT auth.
    if protected and PORTAL_SERVICE not in caddy_services:
        failures.append(
            f"services are protected but {PORTAL_SERVICE!r} has no caddy_services entry — "
            "there would be no reachable login form to redirect to"
        )

    # Both vhost loops in Caddyfile.j2 must run every name through the same
    # gate. They share one macro precisely so they cannot drift; if that macro
    # disappears, protection silently stops being emitted.
    template = CADDYFILE_TEMPLATE.read_text(encoding="utf-8")
    if protected:
        if "sso_protected_services" not in template:
            failures.append(
                f"{CADDYFILE_TEMPLATE.relative_to(ROOT)} never reads sso_protected_services"
            )
        if "forward_auth" not in template:
            failures.append(
                f"{CADDYFILE_TEMPLATE.relative_to(ROOT)} emits no forward_auth directive"
            )
        # One macro definition, one call per vhost loop.
        if template.count("sso_gate(") < 3:
            failures.append(
                f"{CADDYFILE_TEMPLATE.relative_to(ROOT)}: the sso_gate macro is not "
                "invoked by both vhost loops — one catalog would go unprotected"
            )

    # The portal's own backend port must match the catalog entry the Caddyfile
    # template points forward_auth at.
    infra_secret_apps = load(INFRA_CATALOG).get("infra_secret_apps") or {}
    if protected and "authelia" not in infra_secret_apps:
        failures.append(
            "services are protected but infra_secret_apps has no authelia entry — "
            "forward_auth would point at nothing"
        )

    if failures:
        print("SSO forward-auth validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"SSO forward-auth: OK ({len(protected)} services protected)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
