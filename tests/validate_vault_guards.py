#!/usr/bin/env python3
"""Require every REPLACE_* placeholder to be rejected, not merely counted.

`inventory/group_vars/all_vault.yml.example` ships every secret this estate
needs as a `REPLACE_*` string, and that file is committed. A vault copied from
it and only partly filled in therefore holds live credentials whose plaintext
is published in git — and a `| length > 0` check waves every one of them
through, because a placeholder is not empty. The service starts, the deploy
reports success, and the account is open to anyone who has read the repo.
`vault_netbox_superuser_password` is the sharpest example: NetBox creates that
superuser for itself on first start, on a service the whole LAN can reach.

So each assert that requires a secret also asserts it is not still a
placeholder, and this gate makes that pairing structural rather than
remembered: add a `require the secret` assert without the matching
`is match('REPLACE_')` clause and validation fails.

Length checks are not a substitute and must not be mistaken for one. Two vars
in this repo — `vault_netbox_secret_key` (>= 50) and
`vault_romm_auth_secret_key` (>= 32) — happen to reject their placeholders
only because "REPLACE_openssl_rand_hex_32" is 27 characters long. Rename the
placeholder and both stop working, silently. That is luck, not a control, so
this gate demands the explicit guard regardless of any length bound.

WHAT THIS GATE CANNOT SEE, stated plainly, because a checker whose blind spots
are undocumented gets trusted for things it never checked:

  * It reads `that:` clauses in `roles/*/tasks/*.yml` only. A guard applied
    anywhere else — in a template, a `when:`, a `set_fact` — is invisible, and
    a var guarded only that way is reported as unguarded rather than assumed
    fine.
  * It recognises placeholders by the `REPLACE_` prefix. The example file also
    ships two Proxmox values in other forms ("automation@pve!ansible" and an
    "xxxxxxxx-xxxx-…" token). Those are placeholders to a reader and not to
    this gate, and no assert here guards them.
  * It matches variables by name in the assert text. A var reached by
    indirection — `lookup('vars', ...)`, or a name held in a defaults string
    the way `authelia_hashes_var` is — carries no literal `vault_` token, so
    the reference does not exist as far as this gate is concerned. The one
    case that matters, the generic `infra_secret_apps` loop, is detected
    explicitly below; any future one has to be added the same way.
  * A var that no assert requires at all is not a failure here. It is counted
    and named in the summary as unexamined, because "nothing requires it" and
    "something requires it unguarded" are different problems and only the
    second one is this gate's.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "secrets"


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "inventory/group_vars/all_vault.yml.example"
INFRA_CATALOG = ROOT / "inventory/group_vars/all/infra-apps.yml"
ROLES = ROOT / "roles"

PLACEHOLDER = "REPLACE_"
VAULT_RE = re.compile(r"\bvault_[A-Za-z0-9_]+\b")
# `is match('REPLACE_')` in either quoting style. Deliberately narrow: the
# guard has to be the REPLACE_ test, not any mention of the string.
GUARD_RE = re.compile(r"match\(\s*['\"]" + PLACEHOLDER)
# The indirect form the generic infra_secret_apps assert uses.
LOOKUP_RE = re.compile(r"lookup\(\s*['\"]vars['\"]")

CHILD_BLOCKS = {"always", "block", "handlers", "post_tasks", "pre_tasks", "rescue", "tasks"}
ASSERT_KEYS = ("ansible.builtin.assert", "assert")


def placeholder_vars() -> dict[str, str]:
    """Every vault_ key in the example whose value is (or contains) a placeholder.

    Dict-valued secrets count if any member is a placeholder:
    vault_authelia_password_hashes is one key holding six of them.
    """
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")) or {}
    found: dict[str, str] = {}
    for key, value in document.items():
        if not isinstance(key, str) or not key.startswith("vault_"):
            continue
        for candidate in _scalars(value):
            if candidate.startswith(PLACEHOLDER):
                found[key] = candidate
                break
    return found


def _scalars(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _scalars(item)
    elif isinstance(value, list):
        for item in value:
            yield from _scalars(item)


def catalog_require_vault() -> dict[str, str]:
    """vault var -> the infra_secret_apps service that declares it."""
    document = yaml.safe_load(INFRA_CATALOG.read_text(encoding="utf-8")) or {}
    owners: dict[str, str] = {}
    for service, entry in (document.get("infra_secret_apps") or {}).items():
        for variable in (entry or {}).get("require_vault") or []:
            owners.setdefault(variable, service)
    return owners


def assert_tasks(node: Any, source: Path) -> Iterator[tuple[Path, str, list[str], str]]:
    """Yield (file, task name, `that` clauses as text, `loop` as text)."""
    if isinstance(node, list):
        for item in node:
            yield from assert_tasks(item, source)
        return
    if not isinstance(node, dict):
        return

    for key in ASSERT_KEYS:
        arguments = node.get(key)
        if isinstance(arguments, dict):
            clauses = arguments.get("that")
            if isinstance(clauses, str):
                clauses = [clauses]
            elif not isinstance(clauses, list):
                clauses = []
            yield (
                source,
                str(node.get("name", "unnamed assert")),
                [str(clause) for clause in clauses],
                str(node.get("loop", "")),
            )
            break

    for child in CHILD_BLOCKS:
        if child in node:
            yield from assert_tasks(node[child], source)


def main() -> int:
    placeholders = placeholder_vars()
    catalog = catalog_require_vault()
    failures: list[str] = []

    # Positive controls. Every check under this repo's gates has failed once by
    # returning a clean result it had not earned, so this one refuses to pass
    # on an empty read: no placeholders parsed, or no asserts found, means the
    # gate could not look, not that everything is guarded.
    if not placeholders:
        failures.append(
            f"parsed no REPLACE_* placeholders out of "
            f"{EXAMPLE.relative_to(ROOT)}. Either the file moved, its "
            f"placeholder convention changed, or this gate is reading the "
            f"wrong thing — it cannot report OK without having looked."
        )
    if not catalog:
        failures.append(
            f"parsed no require_vault entries out of "
            f"{INFRA_CATALOG.relative_to(ROOT)}; the generic infra_secret_apps "
            f"assert covers a set this gate can no longer resolve."
        )

    required: dict[str, list[str]] = {}   # var -> where it is required
    guarded: set[str] = set()
    generic_loop_seen = False
    generic_loop_guarded = False
    asserts_seen = 0

    for task_file in sorted(ROLES.glob("*/tasks/*.yml")):
        try:
            document = yaml.safe_load(task_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            failures.append(f"{task_file.relative_to(ROOT)}: cannot parse YAML: {error}")
            continue

        for source, name, clauses, loop in assert_tasks(document, task_file):
            asserts_seen += 1
            where = f"{source.relative_to(ROOT)}: {name!r}"

            # The generic loop guards its vars indirectly: it never names one,
            # it reads item.1 out of infra_secret_apps[*].require_vault. Detect
            # it by that shape and resolve the covered set from the catalog,
            # rather than allowlisting eleven variable names that would then
            # have to be maintained by hand.
            indirect = any(LOOKUP_RE.search(clause) for clause in clauses)
            if indirect and "infra_secret_apps" in loop and "require_vault" in loop:
                generic_loop_seen = True
                if any(
                    LOOKUP_RE.search(clause) and GUARD_RE.search(clause)
                    for clause in clauses
                ):
                    generic_loop_guarded = True
                    guarded |= set(catalog)
                for variable in catalog:
                    required.setdefault(variable, []).append(where)
                continue

            for clause in clauses:
                names = set(VAULT_RE.findall(clause)) & set(placeholders)
                if not names:
                    continue
                if GUARD_RE.search(clause):
                    guarded |= names
                else:
                    for variable in names:
                        required.setdefault(variable, []).append(where)

    if not asserts_seen:
        failures.append(
            f"found no assert tasks under {ROLES.relative_to(ROOT)}/*/tasks/*.yml. "
            f"Nothing was examined, so OK would be a statement about nothing."
        )

    if generic_loop_seen and not generic_loop_guarded:
        failures.append(
            "the generic infra_secret_apps require_vault assert "
            "(roles/svc_infra/tasks/files.yml) checks its vars are non-empty "
            f"but no longer rejects REPLACE_* placeholders, so all "
            f"{len(catalog)} catalog secrets are unguarded at once."
        )

    for variable in sorted(set(required) - guarded):
        sites = ", ".join(sorted(set(required[variable])))
        failures.append(
            f"{variable} is required but never checked against its "
            f"REPLACE_* placeholder: {sites}. Its example value is "
            f"{placeholders[variable]!r}, which is non-empty, so the "
            f"requirement passes with the published placeholder deployed as "
            f"the live credential. Add "
            f"`- not (({variable} | default('')) is match('REPLACE_'))` to the "
            f"same assert — this gate only sees guards in the assert that "
            f"states the requirement."
        )

    unexamined = sorted(set(placeholders) - set(required))
    if failures:
        print("Vault placeholder guard validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        if unexamined:
            print(
                f"  (for context, {len(unexamined)} placeholder var(s) are "
                f"required by no assert this gate can see, so they were not "
                f"examined either way: {', '.join(unexamined)})",
                file=sys.stderr,
            )
        return 1

    print(
        f"Vault placeholder guards: OK ({len(guarded)} of {len(placeholders)} "
        f"placeholder vars guarded across {asserts_seen} asserts, "
        f"{len(catalog)} via the infra_secret_apps loop, "
        f"{len(unexamined)} required by no visible assert)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
