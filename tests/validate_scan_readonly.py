#!/usr/bin/env python3
"""Assert the security scan can never change the thing it is measuring.

scan.yml runs unattended at 05:30, as root, against every service VM at once.
That is precisely the shape where a remediation flag is most tempting to add
and most dangerous to have: the blast radius is the whole estate, the operator
is asleep, and the evidence is a journal nobody reads until something breaks.

The specific hazards on these hosts are not hypothetical. Applying a benchmark's
remediation would fight the custom SELinux policy module in roles/svc_download,
rootless podman's subuid mappings, and the NFS automounts — and an unattended
package upgrade would undo the whole point of digest-pinning every image and
holding packages at install-on-demand (see docs/unattended.md).

So report-only is enforced here rather than remembered. A scan path that grows
`--remediate`, an upgrade invocation, or `state: latest` fails the build.

The scan is allowed to WRITE — it produces reports and a baseline state file.
What it may not do is change the system it is reporting on.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Every file that can EXECUTE as part of a scan. Kept explicit rather than
# globbed on a name pattern: a new scan task file that nobody adds here would be
# silently ungated, so the list growing is part of adding one.
#
# The report templates (scan-report.txt.j2, scan-report.html.j2) are
# deliberately absent. They are output documents — Jinja renders them to text
# and nothing in them is ever executed — so a command string there is prose, not
# an action. Including them would mean this gate could not tell a warning to the
# reader ("applying errata is dnf's security-only upgrade path") apart from a
# task that actually does it, and the predictable result of a linter that cries
# wolf about documentation is that someone loosens the linter.
SCAN_PATHS = (
    "scan.yml",
    "roles/service_vm/tasks/scan.yml",
    "roles/svc_infra/templates/scan-run.sh.j2",
)

# `#` to end of line, when the `#` starts a token. Comments are stripped before
# matching for the same reason the report templates are out of scope: a comment
# explaining why an operation is forbidden must not itself trip the gate. A real
# invocation would sit to the LEFT of any trailing `#`, so it still gets caught.
# This covers YAML and shell, which is every file above.
COMMENT_RE = re.compile(r"(?:^|\s)#.*$", re.MULTILINE)
# Jinja comment blocks, which shell templates use for the managed-file header.
JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)

# Each pattern is paired with what it would actually do if it ever landed.
FORBIDDEN: tuple[tuple[str, str, str], ...] = (
    (
        "oscap-remediate",
        r"--remediate\b",
        "OpenSCAP would apply the benchmark's fixes, which on these hosts means "
        "fighting the custom SELinux policy module and rootless podman's subuid "
        "mappings",
    ),
    (
        "oscap-generate-fix",
        r"\bgenerate\s+fix\b",
        "OpenSCAP would emit a remediation script, which exists only to be run",
    ),
    (
        "dnf-upgrade",
        # `dnf update` but NOT `dnf updateinfo` — the trailing \b does that work,
        # since "updateinfo" has no word boundary after "update".
        r"\bdnf\b[^\n]*\b(?:upgrade|update)\b",
        "packages would be upgraded unattended, undoing install-on-demand",
    ),
    (
        "apt-upgrade",
        r"\bapt(?:-get)?\b[^\n]*\bupgrade\b",
        "packages would be upgraded unattended on the hypervisor",
    ),
    (
        "ansible-state-latest",
        r"state:\s*latest\b",
        "Ansible would upgrade a package or image to whatever is newest, against "
        "this repo's digest-pinning rule",
    ),
    (
        "podman-pull",
        # Image scanning reads FROM a registry by reference; it never needs a
        # local pull, and a pull would mutate the host's image store.
        r"\bpodman\s+pull\b",
        "the host's image store would be mutated by a job that only reads",
    ),
)


def main() -> int:
    failures: list[str] = []
    checked = 0

    for relative in SCAN_PATHS:
        path = ROOT / relative
        if not path.exists():
            failures.append(
                f"{relative} is listed in {Path(__file__).name} but does not exist — "
                f"remove it from SCAN_PATHS or restore the file; a gate that "
                f"silently skips its subject is not a gate."
            )
            continue
        checked += 1
        raw = path.read_text(encoding="utf-8")
        # Blank comments to spaces rather than deleting them, so byte offsets —
        # and therefore reported line numbers — still point at the real file.
        text = JINJA_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), raw)
        text = COMMENT_RE.sub(lambda m: " " * len(m.group(0)), text)
        for label, pattern, consequence in FORBIDDEN:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                line = text[: match.start()].count("\n") + 1
                failures.append(
                    f"{relative}:{line} matches {label} ({match.group(0)!r}): "
                    f"{consequence}. The scan is report-only by design."
                )

    if failures:
        print("Scan read-only validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(
        f"Scan read-only: OK ({checked} scan paths, {len(FORBIDDEN)} forbidden "
        f"operations)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
