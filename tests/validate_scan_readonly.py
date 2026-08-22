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

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "ci"


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
    "roles/service_vm/tasks/scan-benchmark.yml",
    "roles/service_vm/tasks/scan-exposure.yml",
    "roles/service_vm/tasks/scan-credentials.yml",
    "roles/service_vm/templates/credential-canary.sh.j2",
    "roles/svc_infra/templates/scan-run.sh.j2",
    "roles/svc_infra/templates/scan-images.sh.j2",
    "roles/svc_infra/tasks/scan.yml",
    # The weekly release report is report-only by exactly the same construction,
    # and the temptation to make it "helpful" is stronger here than anywhere
    # else in the repo: it already knows which images are behind and what the
    # new digest would be. Bumping one is a decision about what a service
    # persists, not a mechanical edit. See docs/plans/release-report.md.
    "release.yml",
    "scripts/release_check.py",
    "scripts/release-check.sh",
    "scripts/image-release.sh",
    "roles/svc_infra/templates/release-run.sh.j2",
    # Invoked from roles/svc_infra/tasks/scan.yml, so it executes under a scan
    # path and belongs inside this gate rather than beside it. It only ever
    # writes a metrics file, but that is a claim this gate should be the one to
    # confirm.
    "roles/svc_infra/files/homelab-metric-write",
)

# Jinja comment blocks, which shell templates use for the managed-file header.
JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)


def blank(match: re.Match[str]) -> str:
    """Replace a match with spaces, keeping its newlines so line numbers hold."""
    return "".join("\n" if character == "\n" else " " for character in match.group(0))


def strip_comments(text: str) -> str:
    """Blank `#` comments, but never a `#` inside a quoted string.

    Comments are stripped for the same reason the report templates are out of
    scope: a comment explaining why an operation is forbidden must not itself
    trip the gate.

    The quote tracking is the part that matters. A plain "`#` to end of line"
    rule blanks from the first `#` it sees, wherever it sees it — including
    inside a YAML or shell string. So this, in a scan task, was silently
    erased before any pattern ran:

        ansible.builtin.command: sh -c 'oscap ... #--remediate'

    That is a fail-open in a gate whose entire premise is that it cannot fail
    open, and it is invisible: the file looks scanned and reports OK.

    Where a line has an unbalanced quote — an apostrophe in prose, most often —
    everything after it is treated as still inside a string and therefore NOT
    blanked. That direction is deliberate: the worst case is a comment that
    gets scanned and a false positive a human resolves, rather than a real
    invocation that goes unread.

    Character counts and newlines are preserved so reported line numbers keep
    pointing at the real file.
    """
    output: list[str] = []
    for line in text.split("\n"):
        quote = ""
        index = 0
        cut = None
        while index < len(line):
            character = line[index]
            if quote:
                # Backslash escapes exist in double quotes only; YAML single
                # quotes and shell single quotes both take '' / no escape.
                if character == "\\" and quote == '"':
                    index += 2
                    continue
                if character == quote:
                    quote = ""
            elif character in "\"'":
                quote = character
            elif character == "#" and (index == 0 or line[index - 1] in " \t"):
                cut = index
                break
            index += 1
        output.append(line if cut is None else line[:cut] + " " * (len(line) - cut))
    return "\n".join(output)

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
        text = strip_comments(JINJA_COMMENT_RE.sub(blank, raw))
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
