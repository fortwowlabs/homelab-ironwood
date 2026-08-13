#!/usr/bin/env python3
"""Prove the admin SSH key list renders every key, not just the first.

admin_ssh_pubkey used to be a scalar. It became a list on 2026-08-12 so more
than one key could be expressed, but its reach did not change: cloud-init, at
provision time, is still the only consumer. Nothing reads it on a running VM,
so this file proves one narrow thing — that every key in the list reaches the
rendered cloud-init, one key per line.

That is easy to get subtly wrong, and wrong looks fine until someone needs the
second key:

  - the template renders `{{ admin_ssh_pubkeys }}` (a Python list repr) or
    only `[0]`, so the second key silently never lands;
  - a `join(' ')` puts every key on one line, which cloud-init accepts without
    complaint as a single malformed key — authorising nobody, on a
    provisioning run that reports success.

So this asserts a two-key list renders two keys AND a one-key list renders
exactly one. The second case is the positive control: a template that emitted
every key it could find regardless of input would pass the first check alone.

It also fails if the scalar name survives anywhere under the deployed paths,
because a leftover reference reads as working code and silently uses an
undefined variable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "roles/pve_vm/templates/user-data.yaml.j2"

KEY_A = "ssh-ed25519 AAAAKEYAAA workstation@example"
KEY_B = "ssh-ed25519 AAAAKEYBBB mac-control@example"

# Every other variable the cloud-init template reads. They are listed by hand
# rather than discovered with jinja2.meta on purpose: auto-filling whatever the
# template happens to reference would also fill a typo'd `admin_ssh_pubkey`,
# which is precisely the failure this file exists to catch. If the template
# gains a variable this list goes stale and the test fails loudly, which is the
# correct direction to fail in.
TEMPLATE_FIXTURE_VARS = {
    "admin_user": "fixture",
    "pve_snippet_dir": "/var/lib/vz/snippets",
    "search_domain": "example.invalid",
    "svc_gid": 10001,
    "svc_uid": 10001,
    "timezone": "UTC",
    "vm_name": "fixture",
}

# Where a surviving reference to the old scalar would actually be deployed.
# docs/ is excluded because the design records and the implementation plan
# quote the old name while explaining why it changed — rewriting history to
# please a grep would destroy the explanation. This file is excluded because it
# has to name the old variable in order to search for it.
STALE_GREP_PATHSPEC = [":!docs", ":!tests/validate_admin_keys.py"]


def render(keys: list[str]) -> str:
    """Render the cloud-init template with Jinja we control."""
    import jinja2

    text = TEMPLATE.read_text(encoding="utf-8")
    # trim_blocks matches ansible.builtin.template's own default. Without it a
    # `{% for %}` on its own line renders a stray blank line here that the real
    # deploy never produces, so the fixture would not be showing what lands on
    # the VM.
    env = jinja2.Environment(
        undefined=jinja2.StrictUndefined, keep_trailing_newline=True, trim_blocks=True)
    return env.from_string(text).render(admin_ssh_pubkeys=keys, **TEMPLATE_FIXTURE_VARS)


def main() -> int:
    failures = []

    for label, keys in (("two keys", [KEY_A, KEY_B]), ("one key", [KEY_A])):
        try:
            rendered = render(keys)
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures.append(f"{label}: template failed to render: {exc}")
            continue
        lines = [line.strip() for line in rendered.splitlines()]
        for key in keys:
            if key not in rendered:
                failures.append(f"{label}: {key!r} missing from rendered cloud-init")
            # Present is not enough: cloud-init needs one key per list item.
            # A `join(' ')` or a list repr puts them all on one line, which it
            # accepts as a single malformed key and then grants nobody access.
            elif f"- {key}" not in lines:
                failures.append(
                    f"{label}: {key!r} is in the output but not alone on its own "
                    "`- ` list item — cloud-init reads that as one malformed key")
        absent = KEY_B if len(keys) == 1 else None
        if absent and absent in rendered:
            failures.append(
                f"{label}: {absent!r} appeared though it was not in the list — "
                "the template is not driven by its input")
        # A bare `{{ admin_ssh_pubkeys }}` renders Python's list repr. Matched
        # against the exact repr of the input rather than a loose "[" scan,
        # because the template legitimately contains flow sequences of its own
        # (`devices: ["/"]`) and a loose scan fails on those forever.
        if str(keys) in rendered:
            failures.append(
                f"{label}: rendered a Python list repr rather than one key per "
                "line — the template needs a for loop")

    stale = subprocess.run(
        ["git", "grep", "-n", "admin_ssh_pubkey\\b", "--", *STALE_GREP_PATHSPEC],
        cwd=ROOT, capture_output=True, text=True,
    )
    if stale.returncode == 0 and stale.stdout.strip():
        failures.append(
            "the singular admin_ssh_pubkey still appears:\n      "
            + "\n      ".join(stale.stdout.strip().splitlines()))

    if failures:
        print("admin SSH key regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print("admin ssh keys: OK (2 cases, including that a one-key list renders one key)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
