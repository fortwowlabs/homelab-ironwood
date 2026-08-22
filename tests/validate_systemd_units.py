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

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "systemd"


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "roles/svc_infra/files/homelab-verify@.service"
TIMER = ROOT / "roles/svc_infra/files/homelab-verify@.timer"

# Units are discovered rather than listed: a new oneshot added under roles/ must
# be covered without anyone remembering to register it here. Both plain unit
# files and Jinja unit templates count — backup-media.service.j2 is a template
# and had exactly this defect.
UNIT_GLOBS = ("roles/**/*.service", "roles/**/*.service.j2")

# The oneshot sweep above only needs services. This second set is wider on
# purpose: .socket and .timer files were covered by NOTHING here until
# 2026-08-21. chat-proxy-relay.socket matched no glob in this file at all, and
# a report claimed both new units had been "parsed by a real systemd" when
# neither had been.
ANALYZE_GLOBS = (
    "roles/**/*.service",
    "roles/**/*.socket",
    "roles/**/*.timer",
)

# The one class of defect worth gating on off-host. `systemd-analyze verify`
# EXITS 0 even when it refuses a unit outright — measured: a socket with no
# Listen setting prints "Refusing" and still returns 0 — so the exit code
# carries no signal and the OUTPUT has to be read instead.
#
# Missing dependencies and missing executables are inherent to verifying a
# unit away from the host it runs on, so they are noise here. An unknown
# directive is not: systemd ignores it silently, so the setting a unit appears
# to carry does nothing whatsoever. This estate shipped exactly that with
# LogRetention=, which is not a systemd directive, and it was caught by hand
# rather than by this gate.
UNKNOWN_KEY_RE = re.compile(r"Unknown key name '([^']+)' in section '([^']+)'")

# This gate's own positive control. If injecting a directive that cannot exist
# stops being reported, systemd-analyze changed its output or is not really
# reading these files, and every clean result below means nothing.
CONTROL_DIRECTIVE = "ThisDirectiveDoesNotExist=1"

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



def check_unknown_directives(systemd_analyze: str) -> tuple[list[str], int]:
    """Fail on directives systemd does not recognise and silently ignores.

    Returns (failures, number of unit files inspected). Jinja unit TEMPLATES
    are deliberately NOT covered: verifying one needs a render with fixture
    variables, which lives in validate_shell_templates.py. That gap is real,
    and chat-egress.service.j2 sits inside it.
    """
    failures: list[str] = []
    paths = sorted(
        {path for glob in ANALYZE_GLOBS for path in ROOT.glob(glob) if path.is_file()}
    )

    control_lines = [
        "[Unit]",
        "Description=unknown-key gate control",
        "[Service]",
        "Type=oneshot",
        "TimeoutStartSec=30",
        "ExecStart=/bin/true",
        CONTROL_DIRECTIVE,
        "",
    ]

    def unknown_keys(unit_path: Path, cwd: Path) -> list[str]:
        result = subprocess.run(
            [systemd_analyze, "verify", str(unit_path)],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        blob = result.stdout + "\n" + result.stderr
        return [
            match.group(1) + " in [" + match.group(2) + "]"
            for match in UNKNOWN_KEY_RE.finditer(blob)
        ]

    with tempfile.TemporaryDirectory(prefix="homelab-unitkeys-") as directory:
        staged = Path(directory)
        # Stage every unit side by side so cross-unit references resolve as far
        # as they can off-host, then verify one file at a time.
        for path in paths:
            (staged / path.name).write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )

        control = staged / "zzz-gate-control.service"
        control.write_text("\n".join(control_lines), encoding="utf-8")

        if not unknown_keys(control, staged):
            failures.append(
                "the unknown-directive control was NOT reported by "
                "systemd-analyze. Either its output format changed or it is "
                "not really inspecting these files, so every clean result "
                "from this check is meaningless until that is fixed."
            )

        for path in paths:
            text = path.read_text(encoding="utf-8")
            for key in unknown_keys(staged / path.name, staged):
                name = key.split(" in [", 1)[0]
                # Verifying a .timer makes systemd load its sibling .service,
                # so an unknown key in the service is reported again under the
                # timer. Attribute each key to the file that actually sets it;
                # its real owner is in `paths` too and reports it there.
                if not re.search(
                    r"^[ 	]*" + re.escape(name) + r"[ 	]*=", text, re.MULTILINE
                ):
                    continue
                failures.append(
                    path.relative_to(ROOT).as_posix()
                    + " sets " + key + ", which systemd does not recognise and "
                    "IGNORES. The unit looks like it carries that setting and "
                    "does not."
                )

    return failures, len(paths)


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
    unknown_count = 0
    if systemd_analyze:
        unknown_failures, unknown_count = check_unknown_directives(systemd_analyze)
        failures.extend(unknown_failures)
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
        + (f"; {unknown_count} unit files carry no unknown directives"
           if systemd_analyze else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
