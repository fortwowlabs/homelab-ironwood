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
    # leak-canary.service was exempt here until 2026-08-04, on the grounds that
    # it "alerts natively with jail-specific context and a generic drop-in
    # would only double the noise". That reasoning covers the canary FINDING
    # something. It does not cover the canary FAILING TO RUN — which is what
    # an OnFailure drop-in is for, and which produced no alert at all: the
    # script never reached its own alert() call, and until TimeoutStartSec was
    # added a hang left it in `activating`, which is not `failed`, so nothing
    # anywhere noticed the estate's safety canary had stopped.
    #
    # The repo already made the correct version of this argument for the scan,
    # in inventory/host_vars/svc-infra.yml: "scan.yml publishes its own
    # findings, so this drop-in is specifically for the scan failing to RUN —
    # which is the failure that would otherwise look exactly like a clean
    # estate." Same unit shape, same reasoning, opposite conclusion.
    #
    # The cost is real and accepted: a genuine leak now notifies twice, once
    # natively and once from the drop-in. A leak is rare and catastrophic, and
    # two messages about one is not what turns a channel into noise — a nightly
    # message about a decision already made is (see the Calibre-Web escalation,
    # 2026-07-31 to 2026-08-03).
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
    name = (
        match.group(1)
        if match
        else timer.name.removesuffix(".j2").removesuffix(".timer") + ".service"
    )
    # A template unit's drop-in directory is named for the template, not for
    # any one instance: homelab-verify@%i.service is configured through
    # homelab-verify@.service.d/, and the drop-in applies to every instance.
    return re.sub(r"@[^.]*\.", "@.", name)


# Which hosts actually run each role, and therefore which host's
# onfailure_units_extra can legitimately cover a timer shipped by that role.
# roles/service_vm and roles/mon run on every service VM; the svc_* roles run on
# exactly one; pve_mon runs on the hypervisor, which is not a service VM at all
# and keeps its list in the role rather than in host_vars.
PVE = "__pve__"
SERVICE_HOSTS = frozenset({"svc-download", "svc-media", "svc-infra"})
ROLE_HOSTS: dict[str, frozenset[str]] = {
    "service_vm": SERVICE_HOSTS,
    "mon": SERVICE_HOSTS,
    "svc_download": frozenset({"svc-download"}),
    "svc_media": frozenset({"svc-media"}),
    "svc_infra": frozenset({"svc-infra"}),
    "pve_mon": frozenset({PVE}),
}


def role_of(unit: Path) -> str:
    """roles/<role>/files/x.timer -> <role>."""
    return unit.relative_to(ROOT).parts[1]


def coverage_by_host() -> tuple[set[str], dict[str, set[str]]]:
    """Units alerted everywhere, and the per-host additions kept separate.

    Keeping these apart is the whole point. Flattening every host's
    onfailure_units_extra into one set meant the gate could not tell whether a
    unit was listed on the host that actually runs it: moving
    homelab-certwatch.service from svc-media.yml to svc-infra.yml — a plausible
    edit when reorganising host_vars — still reported OK, while the drop-in was
    created on a host where the unit does not exist and svc-media's certwatch
    silently lost its alert entirely.
    """
    main = yaml.safe_load(
        (ROOT / "inventory/group_vars/all/main.yml").read_text(encoding="utf-8")
    )
    base: set[str] = set(main.get("onfailure_units_base") or [])

    per_host: dict[str, set[str]] = {}
    for host_vars in sorted((ROOT / "inventory/host_vars").glob("*.yml")):
        document = yaml.safe_load(host_vars.read_text(encoding="utf-8")) or {}
        per_host[host_vars.stem] = set(document.get("onfailure_units_extra") or [])

    pve_defaults = yaml.safe_load(
        (ROOT / "roles/pve_mon/defaults/main.yml").read_text(encoding="utf-8")
    )
    per_host[PVE] = set(pve_defaults.get("pve_onfailure_units") or [])
    return base, per_host


def alerter_copies_match() -> str | None:
    """The PVE host runs Debian and none of service_vm, so its copy of the
    alerter unit is duplicated. Duplicated files drift; this notices."""
    paths = [
        ROOT / "roles/service_vm/files/notify-failure@.service",
        ROOT / "roles/pve_mon/files/notify-failure@.service",
    ]
    contents = {path: path.read_text(encoding="utf-8") for path in paths}
    if len(set(contents.values())) != 1:
        return (
            "roles/service_vm/files/notify-failure@.service and "
            "roles/pve_mon/files/notify-failure@.service have diverged; they "
            "are the same unit deployed to two OS families and must stay "
            "byte-identical"
        )
    return None


def main() -> int:
    base, per_host = coverage_by_host()
    failures: list[str] = []
    checked = 0

    drift = alerter_copies_match()
    if drift:
        failures.append(drift)

    # Forward direction: every timer must be covered on a host that runs it.
    for timer in unit_files():
        service = triggered_service(timer)
        checked += 1
        if service in EXEMPT:
            continue
        role = role_of(timer)
        hosts = ROLE_HOSTS.get(role)
        if hosts is None:
            failures.append(
                f"{timer.relative_to(ROOT)} lives in roles/{role}, which is not "
                f"in ROLE_HOSTS in {Path(__file__).name} — add it so coverage "
                f"can be checked against the hosts that actually run it."
            )
            continue
        uncovered = sorted(h for h in hosts if service not in base | per_host.get(h, set()))
        if uncovered:
            failures.append(
                f"{timer.relative_to(ROOT)} triggers {service}, which has no "
                f"OnFailure alert on {', '.join(uncovered)}: add it to "
                f"onfailure_units_base (inventory/group_vars/all/main.yml) or to "
                f"onfailure_units_extra on that host, or exempt it in "
                f"{Path(__file__).name} with a reason."
            )

    # Reverse direction: an entry that names a unit the host does not run
    # renders a drop-in for a unit that does not exist, which looks like
    # coverage and is not.
    triggered_on: dict[str, set[str]] = {}
    for timer in unit_files():
        for host in ROLE_HOSTS.get(role_of(timer), frozenset()):
            triggered_on.setdefault(host, set()).add(triggered_service(timer))

    # Units not driven by a timer in this repo but legitimately covered:
    # distro-provided units and units started by other units.
    external = {
        "certbot-renew.service",   # distro timer, not a file in this repo
        "vpn-netns.service",       # oneshot, pulled in by the download stack
    }
    all_triggered = set().union(*triggered_on.values()) if triggered_on else set()
    for unit in sorted(base):
        if unit not in all_triggered and unit not in external:
            failures.append(
                f"onfailure_units_base names {unit}, but no timer in this repo "
                f"triggers it and it is not listed as external in "
                f"{Path(__file__).name} — a drop-in for a unit that does not "
                f"exist looks like coverage without being coverage."
            )
    for host, units in sorted(per_host.items()):
        for unit in sorted(units):
            if unit in external:
                continue
            if unit not in triggered_on.get(host, set()):
                failures.append(
                    f"{host} lists {unit} in its onfailure units, but nothing "
                    f"that host runs triggers it. The drop-in would be created "
                    f"where the unit does not exist, while the host that does "
                    f"run it goes unalerted."
                )

    if failures:
        print("OnFailure coverage validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    alerted = len(base | set().union(*per_host.values()) if per_host else base)
    print(
        f"OnFailure coverage: OK ({checked} timers, {alerted} units alerted, "
        f"{len(EXEMPT)} exempt, host-keyed)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
