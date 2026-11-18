#!/usr/bin/env python3
"""Assert every failed-units exemption still has alerting of its own.

`service_vm_failed_units_exempt` lists units that `make verify` leaves out of
its "Check for failed system services" gate, so that one application failing
is not read as the estate drifting. An exemption silences the gate for that
unit, so each pattern must prove three things or it is a hole:

  (a) it is not a blanket (`*`, `*.service`) that would disable the gate;
  (b) it matches a unit this repo actually ships, so a typo or a deleted unit
      does not leave a dead exemption that looks like policy;
  (c) that unit has a timer and failure alerting of its own: an OnFailure
      drop-in on the host that sets the variable, by the same notion of
      "covered" tests/validate_onfailure.py enforces.

The gate carries a positive control (the svc-infra value must exist and be
non-empty) so that deleting the variable cannot make it pass vacuously, and
in-file negative fixtures that must be rejected.
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_onfailure as onfailure  # noqa: E402

GATE_GROUP = "systemd"

ROOT = onfailure.ROOT
VAR = "service_vm_failed_units_exempt"
BLANKETS = {"*", "*.service", "*.*", "?*"}


def declared() -> dict[str, list[str]]:
    """Every value of the variable, keyed by the file that sets it."""
    found: dict[str, list[str]] = {}
    paths = [ROOT / "roles/service_vm/defaults/main.yml"]
    paths += sorted((ROOT / "inventory/host_vars").glob("*.yml"))
    paths += sorted((ROOT / "inventory/group_vars").rglob("*.yml"))
    for path in paths:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(document, dict) and VAR in document:
            found[str(path.relative_to(ROOT))] = list(document[VAR] or [])
    return found


def shipped_services() -> list[Path]:
    found: list[Path] = []
    for pattern in ("roles/*/files/*.service", "roles/*/templates/*.service.j2"):
        found.extend(sorted(ROOT.glob(pattern)))
    return found


def unit_name(path: Path) -> str:
    return path.name.removesuffix(".j2")


def matches(pattern: str, unit: str) -> bool:
    """Template `foo@.service` is matched by `foo@*.service` (any instance)."""
    return fnmatch.fnmatchcase(unit.replace("@.", "@x."), pattern)


def timer_services() -> dict[str, Path]:
    return {onfailure.triggered_service(t): t for t in onfailure.unit_files()}


def check(source: str, patterns: list[str], hosts: set[str] | None) -> list[str]:
    """Problems with one value. `hosts` is the host the value applies to, or
    None when it applies to every service VM."""
    base, per_host = onfailure.coverage_by_host()
    timers = timer_services()
    problems: list[str] = []
    for pattern in patterns:
        if pattern in BLANKETS or not pattern.replace("*", "").replace(".service", ""):
            problems.append(f"{source}: {pattern!r} is a blanket and would disable the gate")
            continue
        units = [p for p in shipped_services() if matches(pattern, unit_name(p))]
        if not units:
            problems.append(f"{source}: {pattern!r} matches no unit file under roles/*")
            continue
        for path in units:
            name = unit_name(path)
            if name in onfailure.EXEMPT:
                problems.append(f"{source}: {name} is exempt from OnFailure, so it has no alert of its own")
            elif name not in timers:
                problems.append(f"{source}: {name} has no timer in this repo")
            else:
                for host in sorted(hosts or onfailure.SERVICE_HOSTS):
                    if name not in base | per_host.get(host, set()):
                        problems.append(
                            f"{source}: {name} has no OnFailure alert on {host}; "
                            f"exempting it would leave a failure with no signal at all"
                        )
    return problems


def main() -> int:
    failures: list[str] = []

    # Negative fixtures: these must be rejected, or the checks above prove nothing.
    for fixture in (["*"], ["homelab-no-such-unit@*.service"]):
        if not check("fixture", fixture, {"svc-infra"}):
            failures.append(f"negative fixture {fixture} was accepted")

    values = declared()
    # Positive control: the gate must have something real to look at.
    infra = values.get("inventory/host_vars/svc-infra.yml")
    if not infra:
        failures.append(
            f"inventory/host_vars/svc-infra.yml does not set a non-empty {VAR}; "
            f"the app-check units would fail verify again"
        )
    for source, patterns in values.items():
        stem = Path(source).stem
        hosts = {stem} if source.startswith("inventory/host_vars/") else None
        failures.extend(check(source, patterns, hosts))

    if failures:
        print("Failed-units exemption validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"Failed-units exemptions: OK ({sum(map(len, values.values()))} patterns, each alerted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
