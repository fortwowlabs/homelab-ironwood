#!/usr/bin/env python3
"""Fail when a security-relevant Open WebUI setting has drifted from the repo.

Since ENABLE_PERSISTENT_CONFIG became true on 2026-08-10, most of Open WebUI's
config is deliberately allowed to drift: the agreed model is seeded from git,
modified in the UI, captured by the backup. This gate is about the small set
where drift is a security change rather than a preference.

`chat.fortwow.dev` is publicly reachable and deliberately not behind Authelia,
so Open WebUI's own login is the whole front door. `ui.enable_signup` flipped
in the admin UI now SURVIVES restarts and `make infra` will not turn it back
off -- the environment is ignored once a database row exists. Nothing else in
this repo would notice.

THREE STATES, NOT TWO. A missing export is "could not look", not "clean". It
is reported loudly and does not pass silently, but it does not fail the build
either: on a fresh clone nobody has run `make owui-export` yet, and a gate that
blocks all work until someone finds an admin token would just be disabled. The
state that fails is an export that EXISTS and disagrees with the repo.

Refresh the export with `make owui-export`. Until it has been run once against
the live instance, this gate is not protecting anything -- which it says.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

GATE_GROUP = "catalog"

ROOT = Path(__file__).resolve().parents[1]
EXPORT_PATH = ROOT / "inventory/group_vars/all/openwebui-config.yml"
CATALOG_PATH = ROOT / "inventory/group_vars/all/infra-apps.yml"

# Keys whose drift is a security change. Everything absent from this map is
# free to drift by design -- that is the whole point of seed-then-modify.
#
# (config key in the export) -> (env var in infra-apps.yml, coercion)
ENFORCED = {
    "ui.enable_signup": ("ENABLE_SIGNUP", "bool"),
    "ui.default_user_role": ("DEFAULT_USER_ROLE", "str"),
}

REDACTED = "<redacted>"


def coerce(value: object, kind: str) -> object:
    """Normalise an infra-apps.yml env string to the export's native type."""
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() == "true"
    return str(value).strip()


def compare(declared: dict, live: dict) -> tuple[list[str], list[str]]:
    """Return (failures, notes) for the enforced keys."""
    failures: list[str] = []
    notes: list[str] = []
    for config_key, (env_var, kind) in sorted(ENFORCED.items()):
        if env_var not in declared:
            failures.append(
                f"{env_var} is not declared in infra-apps.yml, so there is "
                f"nothing to compare {config_key} against. An enforced setting "
                "with no declaration cannot be checked at all")
            continue
        want = coerce(declared[env_var], kind)

        if config_key not in live:
            notes.append(
                f"{config_key}: no row in the export -- the environment value "
                f"({want!r}) is therefore still in effect")
            continue
        got = live[config_key]
        if got == REDACTED:
            failures.append(
                f"{config_key} is redacted in the export, so drift in a "
                "security-relevant setting cannot be seen. Add it to "
                "SAFE_PREFIXES in scripts/owui_config_export.py")
            continue
        if coerce(got, kind) != want:
            failures.append(
                f"{config_key} is {got!r} live but the repo declares {env_var}="
                f"{declared[env_var]!r}. The database wins, so `make infra` will "
                "NOT correct this -- change it in the admin UI, or accept it and "
                "update infra-apps.yml so the two agree")
    return failures, notes


# A gate against silent drift may not fail silently itself. Each case is
# (description, declared env, live export, must_fail).
SELF_CHECK_CASES = (
    ("agreement passes",
     {"ENABLE_SIGNUP": "false", "DEFAULT_USER_ROLE": "pending"},
     {"ui.enable_signup": False, "ui.default_user_role": "pending"}, False),
    ("signup turned on in the UI is caught",
     {"ENABLE_SIGNUP": "false", "DEFAULT_USER_ROLE": "pending"},
     {"ui.enable_signup": True, "ui.default_user_role": "pending"}, True),
    ("a changed default role is caught",
     {"ENABLE_SIGNUP": "false", "DEFAULT_USER_ROLE": "pending"},
     {"ui.enable_signup": False, "ui.default_user_role": "user"}, True),
    ("a redacted enforced key is caught, not skipped",
     {"ENABLE_SIGNUP": "false", "DEFAULT_USER_ROLE": "pending"},
     {"ui.enable_signup": REDACTED, "ui.default_user_role": "pending"}, True),
    ("an undeclared enforced key is caught",
     {"DEFAULT_USER_ROLE": "pending"},
     {"ui.enable_signup": False, "ui.default_user_role": "pending"}, True),
    ("no row means the environment still applies, which is not drift",
     {"ENABLE_SIGNUP": "false", "DEFAULT_USER_ROLE": "pending"},
     {}, False),
)


def self_check() -> list[str]:
    problems: list[str] = []
    for description, declared, live, must_fail in SELF_CHECK_CASES:
        failed = bool(compare(declared, live)[0])
        if failed != must_fail:
            verb = "did not flag" if must_fail else "wrongly flagged"
            problems.append(
                f"self-check {description!r}: the comparison {verb} it, so this "
                "gate can no longer detect the drift it exists for")
    return problems


def main() -> int:
    failures = self_check()
    if failures:
        print("Open WebUI drift gate is broken:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    catalog = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    declared = catalog["infra_secret_apps"]["open-webui"]["env"]

    if not EXPORT_PATH.exists():
        print("Open WebUI config drift: INCONCLUSIVE -- no export to compare "
              "against.")
        print(f"  {EXPORT_PATH.relative_to(ROOT).as_posix()} does not exist, so "
              "nothing here is being checked.")
        print("  Run `make owui-export` once against the live instance (needs "
              "OWUI_ADMIN_TOKEN) to make this gate mean something.")
        print(f"  Would enforce: {', '.join(sorted(ENFORCED))}")
        return 0

    document = yaml.safe_load(EXPORT_PATH.read_text(encoding="utf-8")) or {}
    live = document.get("openwebui_live_config")
    if not isinstance(live, dict) or not live:
        print(f"{EXPORT_PATH.name} has no openwebui_live_config mapping; it is "
              "corrupt or was hand-edited. Re-run `make owui-export`.",
              file=sys.stderr)
        return 1

    failures, notes = compare(declared, live)
    for note in notes:
        print(f"  note: {note}")
    if failures:
        print("Open WebUI config drift detected:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"Open WebUI config drift: OK ({len(ENFORCED)} enforced keys checked "
          f"against {len(live)} exported)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
