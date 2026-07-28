#!/usr/bin/env python3
"""Assert every timer-triggered unit has a failure alert (or a stated reason not to).

A systemd timer whose service fails is silent by default. That silence is
survivable when someone runs `make verify` weekly; it is not survivable when
the environment is left alone for months. This gate makes coverage a build-time
property: add a timer to this repo without an OnFailure drop-in or an entry
below, and validation fails.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]

# Units that deliberately have no OnFailure drop-in, with the reason. An entry
# here is a decision on the record, not a TODO.
EXEMPT = {
    "leak-canary.service": (
        "alerts natively with jail-specific context (which container, which "
        "check) and runs every 15 minutes; a generic drop-in would only "
        "double the noise"
    ),
    "notify-failure@.service": (
        "is the alerter itself — an OnFailure here would be a loop, and it "
        "always exits 0 by design"
    ),
}


def unit_files() -> list[Path]:
    found: list[Path] = []
    for pattern in ("roles/*/files/*.timer", "roles/*/templates/*.timer.j2"):
        found.extend(sorted(ROOT.glob(pattern)))
    return found


def triggered_service(timer: Path) -> str:
    """The service a timer starts: an explicit Unit=, else the timer's own stem."""
    text = timer.read_text(encoding="utf-8")
    match = re.search(r"^\s*Unit\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    if match:
        return match.group(1)
    return timer.name.removesuffix(".j2").removesuffix(".timer") + ".service"


def covered_units() -> set[str]:
    """Union of onfailure_units_base and every host's onfailure_units_extra."""
    units: set[str] = set()
    main = yaml.safe_load(
        (ROOT / "inventory/group_vars/all/main.yml").read_text(encoding="utf-8")
    )
    units.update(main.get("onfailure_units_base") or [])
    for host_vars in sorted((ROOT / "inventory/host_vars").glob("*.yml")):
        document = yaml.safe_load(host_vars.read_text(encoding="utf-8")) or {}
        units.update(document.get("onfailure_units_extra") or [])
    return units


def main() -> int:
    covered = covered_units()
    failures: list[str] = []
    checked = 0

    for timer in unit_files():
        service = triggered_service(timer)
        checked += 1
        if service in EXEMPT or service in covered:
            continue
        failures.append(
            f"{timer.relative_to(ROOT)} triggers {service}, which has no "
            f"OnFailure alert: add it to onfailure_units_base "
            f"(inventory/group_vars/all/main.yml) or onfailure_units_extra "
            f"(inventory/host_vars/<host>.yml), or exempt it in {Path(__file__).name} "
            f"with a reason."
        )

    # A stale entry is its own bug: it renders a drop-in for a unit that does
    # not exist, which looks like coverage and is not.
    known = {triggered_service(timer) for timer in unit_files()}
    # Units not driven by a timer in this repo but legitimately covered:
    # distro-provided units and units started by other units.
    external = {
        "certbot-renew.service",   # distro timer, not a file in this repo
        "vpn-netns.service",       # oneshot, pulled in by the download stack
    }
    for unit in sorted(covered):
        if unit not in known and unit not in external:
            failures.append(
                f"onfailure list names {unit}, but no timer in this repo "
                f"triggers it and it is not listed as external in "
                f"{Path(__file__).name} — a drop-in for a unit that does not "
                f"exist looks like coverage without being coverage."
            )

    if failures:
        print("OnFailure coverage validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(
        f"OnFailure coverage: OK ({checked} timers, {len(covered)} units alerted, "
        f"{len(EXEMPT)} exempt)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
