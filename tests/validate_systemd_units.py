#!/usr/bin/env python3
"""Validate instantiated nightly units, using systemd-analyze when available.

Also enforces that every Type=oneshot unit in the repo sets TimeoutStartSec.

systemd disables the start timeout for Type=oneshot by default, on the theory
that a oneshot may legitimately be a long batch job. On these hosts that default
is a silent-failure generator, because a unit stuck in `activating` is NOT
`failed`:

  - OnFailure= never fires, so notify-failure@ never publishes;
  - OnUnitActiveSec= cannot retrigger a unit that has not finished, so a
    15-minute watcher stops watching after its first hang;
  - `systemctl --failed` stays empty, so the failed-unit sweep and `make verify`
    both report the host clean.

Every one of these units hangs on something real and remote — df and tar over a
`hard` NFS mount, zpool against a degraded pool, podman inspect against a wedged
socket. The timeout is what converts those hangs into a failure, which is the
only state anything in this estate alerts on.

The check is deliberately static text rather than systemd-analyze: this repo is
developed on macOS, where systemd-analyze does not exist and the block below
downgrades to static checks. A gate that only runs on the CI that has never run
(see CLAUDE.md) is not a gate.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "roles/svc_infra/files/homelab-verify@.service"
TIMER = ROOT / "roles/svc_infra/files/homelab-verify@.timer"

# Units are discovered rather than listed: a new oneshot added under roles/ must
# be covered without anyone remembering to register it here. Both plain unit
# files and Jinja unit templates count — backup-media.service.j2 is a template
# and had exactly this defect.
UNIT_GLOBS = ("roles/**/*.service", "roles/**/*.service.j2")

# Anchored at line start so a directive named inside a comment does not satisfy
# the check. Several of these units explain in prose why the timeout is there.
ONESHOT_RE = re.compile(r"^[ \t]*Type=oneshot[ \t]*$", re.MULTILINE)
TIMEOUT_RE = re.compile(r"^[ \t]*TimeoutStart(?:Sec)?=", re.MULTILINE)

# The positive control: the unit that carries the canonical explanation. If the
# discovery above stops finding it, discovery is broken and every "no oneshot
# units are missing a timeout" result below is meaningless.
CONTROL_UNIT = "roles/mon/files/homelab-diskalert.service"


def check_oneshot_timeouts() -> tuple[list[str], int]:
    """Return (failures, number of Type=oneshot units inspected)."""
    failures: list[str] = []
    oneshot: list[str] = []

    paths = sorted(
        {path for glob in UNIT_GLOBS for path in ROOT.glob(glob) if path.is_file()}
    )
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if not ONESHOT_RE.search(text):
            continue
        oneshot.append(relative)
        if not TIMEOUT_RE.search(text):
            failures.append(
                f"{relative} is Type=oneshot with no TimeoutStartSec. systemd "
                f"disables the start timeout for oneshot units, so a hang leaves "
                f"this stuck in `activating` — which is not `failed`, so "
                f"OnFailure= never fires, the timer cannot retrigger it, and "
                f"`systemctl --failed` stays empty. Add TimeoutStartSec with a "
                f"comment naming what can hang; see "
                f"roles/mon/files/homelab-diskalert.service."
            )

    if CONTROL_UNIT not in oneshot:
        failures.append(
            f"the oneshot sweep did not find {CONTROL_UNIT}, which is Type=oneshot "
            f"and must be. Unit discovery is broken, so a clean result here proves "
            f"nothing — fix UNIT_GLOBS/ONESHOT_RE before trusting this gate."
        )

    return failures, len(oneshot)


def main() -> int:
    service = SERVICE.read_text(encoding="utf-8")
    timer = TIMER.read_text(encoding="utf-8")
    failures: list[str] = []

    # NOT ConditionPathIsDirectory=/opt/homelab-iac/.venv, which is what this
    # tuple required until 2026-08-03. That directive was deliberately REMOVED
    # in the same window this gate was written: systemd treats an unmet
    # condition as SUCCESS, so a missing virtualenv meant ExecStart never ran,
    # Result=success, OnFailure= never fired and hc-ping.sh never sent its fail
    # — nightly verification would have ended silently with every indicator
    # green. ExecStartPre fails instead, which is a real failure.
    #
    # The gate did not notice the directive was gone, because
    # `"ConditionPathIsDirectory=..." in service` is a substring test and the
    # string still occurs — inside the comment on line 14 of the unit that
    # explains why it was removed. So this asserted the presence of something
    # that had been deleted, and passed on the prose describing the deletion.
    # A gate that cannot fail is the exact defect CLAUDE.md catalogues, and it
    # sat in the file whose job is checking these units.
    #
    # Require the replacement, and require the original to be absent as an
    # ACTIVE directive — a comment mentioning it is fine and is checked for
    # separately below, on line-anchored matching rather than substring.
    required_service = (
        "User=%i",
        "WorkingDirectory=/opt/homelab-iac",
        "ExecStart=/usr/local/sbin/homelab-verify-run.sh",
        "ExecStartPre=/usr/bin/test -d /opt/homelab-iac/.venv",
    )
    for unit_name, text in (("service", service), ("timer", timer)):
        for line in text.splitlines():
            if line.strip().startswith("ConditionPath"):
                failures.append(
                    f"nightly {unit_name} sets {line.strip()!r} as an active "
                    f"directive. An unmet systemd condition is reported as "
                    f"SUCCESS, so it cannot gate anything that must alert on "
                    f"failure — use ExecStartPre, which fails."
                )
    required_timer = (
        "Unit=homelab-verify@%i.service",
        "Persistent=true",
        "WantedBy=timers.target",
    )
    for fragment in required_service:
        if fragment not in service:
            failures.append(f"nightly service is missing {fragment!r}")
    for fragment in required_timer:
        if fragment not in timer:
            failures.append(f"nightly timer is missing {fragment!r}")
    if "ConditionOS=" in service + timer:
        failures.append("nightly units use unsupported ConditionOS")

    oneshot_failures, oneshot_count = check_oneshot_timeouts()
    failures.extend(oneshot_failures)

    systemd_analyze = shutil.which("systemd-analyze")
    if systemd_analyze:
        with tempfile.TemporaryDirectory(prefix="homelab-systemd-") as directory:
            fixture_dir = Path(directory)
            fixture_service = fixture_dir / "homelab-verify@fixture.service"
            fixture_timer = fixture_dir / "homelab-verify@fixture.timer"
            fixture_service.write_text(
                service.replace("User=%i", "User=root")
                .replace("WorkingDirectory=/opt/homelab-iac", "WorkingDirectory=/")
                .replace(
                    "ExecStartPre=/usr/bin/test -d /opt/homelab-iac/.venv",
                    "ExecStartPre=/usr/bin/test -d /",
                )
                .replace(
                    "ExecStart=/usr/local/sbin/homelab-verify-run.sh",
                    "ExecStart=/bin/true",
                )
                .replace(
                    "EnvironmentFile=/etc/homelab-healthchecks.env",
                    "",
                ),
                encoding="utf-8",
            )
            fixture_timer.write_text(timer, encoding="utf-8")
            result = subprocess.run(
                [systemd_analyze, "verify", str(fixture_service), str(fixture_timer)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                failures.append(
                    "systemd-analyze rejected nightly units: "
                    + (result.stderr.strip() or result.stdout.strip())
                )

    if failures:
        print("Systemd unit validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    suffix = " + systemd-analyze" if systemd_analyze else " (static; non-Linux host)"
    print(
        f"Instantiated nightly systemd units: OK{suffix}; "
        f"{oneshot_count} Type=oneshot units all set TimeoutStartSec"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
