#!/usr/bin/env python3
"""Prove preflight's address-uniqueness assert actually covers control_nodes.

inventory/hosts.yml's `ansible_host` is the single source of truth for every
machine's address, and preflight.yml exists to catch a missing or duplicated
one before it surfaces as a confusing failure deep inside cloud-init rendering
or Caddy templating.

That assert was written when `service_vms` was the only group with addresses.
Adding `control_nodes` without extending it would leave the new machine
uncovered — and the failure mode is silent: the gate still passes, it just
stops looking at half the estate.

So this runs the real playbook against a synthetic inventory, twice:

  distinct addresses      rc 0        the gate does not false-positive
  duplicate in             non-zero    the gate actually looks at the new group
  control_nodes

The second case is the point. The first is its positive control: without it, a
gate that had degraded to `assert: true` would pass this test.

Only preflight's localhost play runs (`--limit localhost`), so nothing here
touches the homelab or needs a vault.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "preflight.yml"
VENV_ANSIBLE = ROOT / ".venv/bin/ansible-playbook"


def ansible_playbook() -> str:
    return str(VENV_ANSIBLE) if VENV_ANSIBLE.exists() else "ansible-playbook"


def inventory(control_address: str) -> dict:
    """A minimal inventory with three service VMs and one control node.

    `localhost` is listed explicitly under `all.hosts` rather than relied on
    implicitly: this ansible-core does not inject the implicit localhost into
    `--limit` matching once the inventory already contains other real hosts,
    so `--limit localhost` would otherwise match nothing and the run would
    fail before either assert gets a chance to execute.
    """
    return {
        "all": {
            "hosts": {"localhost": {"ansible_connection": "local"}},
            "children": {
                "service_vms": {
                    "children": {
                        "download_vms": {
                            "hosts": {"svc-download": {"ansible_host": "127.0.0.2"}}
                        },
                        "media_vms": {
                            "hosts": {"svc-media": {"ansible_host": "127.0.0.1"}}
                        },
                        "infra_vms": {
                            "hosts": {"svc-infra": {"ansible_host": "127.0.0.3"}}
                        },
                    }
                },
                "control_nodes": {
                    "hosts": {"mac-control": {"ansible_host": control_address}}
                },
            }
        }
    }


CASES = [
    ("distinct addresses", "127.0.0.4", 0),
    ("control_nodes duplicates a service VM", "127.0.0.2", 1),
]


def run(control_address: str) -> int:
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as handle:
        yaml.safe_dump(inventory(control_address), handle)
        path = handle.name
    try:
        completed = subprocess.run(
            [
                ansible_playbook(),
                str(PLAYBOOK),
                "--inventory",
                path,
                "--limit",
                "localhost",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        return completed.returncode
    finally:
        Path(path).unlink(missing_ok=True)


def main() -> int:
    failures = []
    for name, address, expected_nonzero in CASES:
        rc = run(address)
        got_nonzero = 1 if rc != 0 else 0
        if got_nonzero != expected_nonzero:
            failures.append(
                f"{name}: expected {'failure' if expected_nonzero else 'success'}, "
                f"got exit {rc}. preflight.yml must collect addresses from "
                "control_nodes as well as service_vms."
            )

    if failures:
        print("preflight addressing regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        return 1

    print(f"preflight addressing: OK ({len(CASES)} cases, "
          "including that a duplicate in control_nodes still fails)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
