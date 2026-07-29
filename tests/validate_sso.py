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

The second half checks the identity side: the `authelia_users` roster and the
`access_control` rules that decide what those accounts reach. Both fail
quietly in their own way — a group with no rule denies everything (visible,
recoverable), while no rule for `admins` locks every account out of every
protected service at once (visible only after the deploy, and only fixable
from a direct IP:port path). The password hashes are NOT checked here: they
live in the gitignored vault, so that gate is a deploy-time assert in
roles/svc_infra/tasks/files.yml instead.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MAIN_VARS = ROOT / "inventory/group_vars/all/main.yml"
DOWNLOAD_CATALOG = ROOT / "inventory/group_vars/all/apps.yml"
INFRA_CATALOG = ROOT / "inventory/group_vars/all/infra-apps.yml"
CADDYFILE_TEMPLATE = ROOT / "roles/svc_media/templates/Caddyfile.j2"
INFRA_DEFAULTS = ROOT / "roles/svc_infra/defaults/main.yml"
AUTHELIA_CONFIG_TEMPLATE = ROOT / "roles/svc_infra/templates/authelia-configuration.yml.j2"
AUTHELIA_USERS_TEMPLATE = ROOT / "roles/svc_infra/templates/authelia-users.yml.j2"

# The vhost that serves the login form. Protecting it with itself is the one
# entry that turns a working deploy into an unusable one.
PORTAL_SERVICE = "auth"

# The group that must always retain access. Losing this is the lockout.
ADMIN_GROUP = "admins"

# access_control.rules subjects are 'group:<name>' / 'user:<name>' strings.
GROUP_SUBJECT_RE = re.compile(r"^group:(.+)$")

# Jinja that actually renders, as opposed to prose mentioning the same name.
VAULT_EXPRESSION_RE = re.compile(r"\{\{[^}]*\bvault_[A-Za-z0-9_]+")
ROSTER_LOOP_RE = re.compile(r"\{%-?\s*for\s+\w+\s+in\s+authelia_users\b")


def load(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document if isinstance(document, dict) else {}


def load_template(path: Path) -> dict | None:
    """Parse a Jinja template as YAML by neutralising its expressions.

    The Authelia config is templated but structurally static — the only
    substitutions are scalars like {{ service_domain }} inside quoted values.
    Replacing them with a placeholder yields parseable YAML, which is what
    lets the rules be checked as data rather than by grepping for strings.

    Returns None if the result will not parse. That case MUST be reported
    rather than treated as an empty config: an unparseable template silently
    satisfies every check below, which is the failure mode these gates exist
    to catch. The first draft of this function returned {} and two of the
    four gates passed against deliberately broken input.
    """
    text = path.read_text(encoding="utf-8")
    # A Jinja expression alone on its line is the `ansible_managed | comment`
    # header, which renders as comment lines rather than as a YAML value.
    text = re.sub(r"^\s*\{\{.*?\}\}\s*$", "#", text, flags=re.MULTILINE)
    text = re.sub(r"^.*\{%.*?%\}.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\{\{.*?\}\}", "JINJA", text)
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return document if isinstance(document, dict) else None


def check_identity(failures: list[str]) -> int:
    """Validate the account roster against the authorization rules."""
    users = load(INFRA_DEFAULTS).get("authelia_users")
    if users is None:
        # Nothing to check; the single-account shape predates this list.
        return 0

    if not isinstance(users, list) or not users:
        failures.append("authelia_users must be a non-empty list")
        return 0

    names: list[str] = []
    assigned_groups: set[str] = set()
    for index, user in enumerate(users):
        if not isinstance(user, dict):
            failures.append(f"authelia_users[{index}] is not a mapping")
            continue
        name = user.get("name")
        if not isinstance(name, str) or not name.strip():
            failures.append(f"authelia_users[{index}] has no usable 'name'")
            continue
        names.append(name)
        groups = user.get("groups")
        if not isinstance(groups, list) or not groups:
            # Deny-by-default means a group is the ONLY way to be granted
            # anything. A user without one can authenticate and reach nothing,
            # which looks like a broken account rather than a config mistake.
            failures.append(f"authelia_users entry {name!r} has no 'groups'")
            continue
        assigned_groups.update(groups)

    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        # Rendered as YAML keys, so a duplicate silently overwrites the
        # earlier entry rather than erroring — including its group list.
        failures.append(f"duplicate authelia_users names: {', '.join(duplicates)}")

    config = load_template(AUTHELIA_CONFIG_TEMPLATE)
    if config is None:
        failures.append(
            f"{AUTHELIA_CONFIG_TEMPLATE.relative_to(ROOT)} does not parse as YAML once "
            "its Jinja expressions are stubbed — the authorization rules below "
            "cannot be checked, so this is a failure rather than a pass"
        )
        return len(names)

    access_control = config.get("access_control")
    if not isinstance(access_control, dict):
        failures.append(
            f"{AUTHELIA_CONFIG_TEMPLATE.relative_to(ROOT)} has no access_control block "
            "— Authelia would fall back to its own default policy"
        )
        return len(names)

    default_policy = access_control.get("default_policy")
    rules = access_control.get("rules") or []

    ruled_groups: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        subjects = rule.get("subject") or []
        if isinstance(subjects, str):
            subjects = [subjects]
        for subject in subjects:
            if isinstance(subject, str):
                match = GROUP_SUBJECT_RE.match(subject)
                if match:
                    ruled_groups.add(match.group(1))

    if default_policy == "deny" and ADMIN_GROUP not in ruled_groups:
        failures.append(
            f"access_control denies by default but no rule grants {ADMIN_GROUP!r} — "
            "every account would be locked out of every protected service"
        )

    if ADMIN_GROUP not in assigned_groups:
        failures.append(
            f"no authelia_users entry is in {ADMIN_GROUP!r} — "
            "nobody could reach a protected service"
        )

    # A group named in a rule but held by nobody is dead configuration; the
    # reverse (a group with no rule) is intentional under deny-by-default and
    # is reported as information, not a failure.
    orphaned = sorted(ruled_groups - assigned_groups)
    if orphaned:
        failures.append(
            "access_control rules grant groups no user belongs to: "
            f"{', '.join(orphaned)}"
        )

    # The roster is public config; the hashes are not. Both halves have to be
    # rendered by real Jinja, not merely mentioned — every name checked here
    # also appears in that template's own comments, so a substring search
    # passes even after the expression using it is gone.
    users_template = AUTHELIA_USERS_TEMPLATE.read_text(encoding="utf-8")
    if not VAULT_EXPRESSION_RE.search(users_template):
        failures.append(
            f"{AUTHELIA_USERS_TEMPLATE.relative_to(ROOT)} renders no vault_ variable "
            "— the accounts would have no password"
        )
    if not ROSTER_LOOP_RE.search(users_template):
        failures.append(
            f"{AUTHELIA_USERS_TEMPLATE.relative_to(ROOT)} never loops over "
            "authelia_users — the roster would not be rendered"
        )

    return len(names)


def report(failures: list[str]) -> int:
    print("SSO validation failed:", file=sys.stderr)
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1


def main() -> int:
    failures: list[str] = []

    main_vars = load(MAIN_VARS)
    protected = main_vars.get("sso_protected_services")

    # Runs regardless of the list below: the accounts and their authorization
    # are wrong or right independently of how many vhosts are gated today.
    account_count = check_identity(failures)

    if protected is None:
        # An absent list is a legitimate state: it is the documented rollback
        # (empty the list, re-run `make access`). Nothing else to check.
        if failures:
            return report(failures)
        print(f"SSO forward-auth: OK (no services protected, {account_count} accounts)")
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
        return report(failures)

    print(
        f"SSO forward-auth: OK ({len(protected)} services protected, "
        f"{account_count} accounts)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
