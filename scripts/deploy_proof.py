#!/usr/bin/env python3
"""Run a deploy and decide whether it proved deployed state equals HEAD.

WHY THIS EXISTS

Step 6 of the change workflow in CLAUDE.md is the single most valuable line in
it: a deploy that reports changed=0 against a clean tree is proof that what is
running equals what is committed. Until now that proof was a human reading a
recap, with a documented exception — svc-infra reports changed=3 right after a
commit, because the nightly runner's git archive is rebuilt — and an explicit
warning not to paper over a real diff by deploying twice and quoting the
second number.

A rule that says "check WHICH tasks changed" is a rule that gets skipped at
11pm. This checks.

THE ALLOWLIST IS BY NAME, NOT BY COUNT

Matching "svc-infra may report 3" would pass a deploy where the runner sync
changed and something real changed too, as long as the total happened to be
three. The three names are matched exactly, on the infra host only, and only
as the complete set — a subset means the block did something other than what
it does after a commit, which is a situation nobody has reasoned about.

EXIT CODES

  0  proved: nothing changed, or only the runner checkout sync
  1  drift: something changed that is not explained
  2  could not look: the deploy failed, or never reached PLAY RECAP

2 is distinct from 1 on purpose. A run that died has proved nothing, and
reporting that as "no drift found" is the failure mode this repo keeps
finding in its own checks.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# The three tasks in roles/svc_infra/tasks/verify-runner.yml's "Synchronise the
# runner checkout" block. They change on the first deploy after every commit,
# because .deployed-rev still names the previous revision. The fourth task in
# that block, "Remove the local archive", is changed_when: false and never
# appears here.
SYNC_TASKS = (
    "Build a tracked-files archive of the committed tree",
    "Unpack the archive onto the runner",
    "Record the deployed revision",
)

# `TASK [role_name : Task name] ****...`
TASK_RE = re.compile(r"^TASK \[(?:[^:\]]+ : )?(.+?)\] \*+\s*$")
# `changed: [host]` or, for a delegated task, `changed: [host -> localhost]`.
# The host before the arrow is the inventory host the change is attributed to.
CHANGED_RE = re.compile(r"^changed: \[([^\]\s]+)(?: -> [^\]]+)?\]")
RECAP_RE = re.compile(r"^PLAY RECAP \*+\s*$", re.MULTILINE)


class NoRecapError(Exception):
    """The output contains no PLAY RECAP, so the deploy proved nothing."""


def parse_changed(text: str) -> dict[str, list[str]]:
    """Map each host to the task names that reported `changed` for it.

    Each name appears once, in the order it was first seen. Ansible prints one
    `changed:` line per ITEM of a looping task, so a task whose loop changed
    three items emits three lines while PLAY RECAP counts it once; listing it
    three times would ask the operator to explain three things where there is
    one. Ordering is preserved rather than sorted so the report reads in the
    order the deploy ran.

    Nothing downstream counts these — the sync-trio allowlist compares sets —
    so collapsing duplicates loses no signal. How MANY items a task touched is
    still in the deploy output above the verdict, which is streamed in full.
    """
    if not RECAP_RE.search(text):
        raise NoRecapError(
            "no PLAY RECAP in the deploy output — the run did not finish, so "
            "it has proved nothing about deployed state. This is not the same "
            "as changed=0; read the output above for the failure."
        )

    changed: dict[str, list[str]] = {}
    current_task = "<before the first task>"
    for line in text.splitlines():
        task_match = TASK_RE.match(line)
        if task_match:
            current_task = task_match.group(1)
            continue
        changed_match = CHANGED_RE.match(line)
        if changed_match:
            host = changed_match.group(1)
            tasks = changed.setdefault(host, [])
            if current_task not in tasks:
                tasks.append(current_task)
    return changed


def verdict(changed: dict[str, list[str]], infra_host: str) -> tuple[int, str]:
    """Return (exit code, message) for a parsed set of changed tasks."""
    unexplained: list[str] = []
    sync_seen = False
    sync_alongside_drift = False

    for host, tasks in sorted(changed.items()):
        if host == infra_host and set(tasks) == set(SYNC_TASKS):
            sync_seen = True
            continue
        # The trio is complete AND something else changed too. Name only the
        # something else: reporting all four would bury the one line that
        # matters under three the operator already knows about. The verdict is
        # unchanged — this still fails.
        #
        # A PARTIAL trio does not take this path and stays fully unexplained.
        # The sync block is gated on one condition and runs whole or not at
        # all, so two of three is not "less drift", it is a state nobody has
        # reasoned about.
        if host == infra_host and set(SYNC_TASKS) < set(tasks):
            sync_alongside_drift = True
            unexplained.extend(
                f"{host}: {task}" for task in tasks if task not in SYNC_TASKS
            )
            continue
        unexplained.extend(f"{host}: {task}" for task in tasks)

    if unexplained:
        lines = "\n".join(f"    {entry}" for entry in unexplained)
        note = ""
        if sync_alongside_drift:
            note = (
                f"\n    (The three runner checkout sync tasks on {infra_host} "
                "also changed, which is expected after a commit. They are "
                "omitted above; the lines listed are the ones that are not "
                "explained.)"
            )
        return 1, (
            "Deployed state does not match the commit. These tasks reported "
            f"changed and are not the complete runner checkout sync:\n{lines}"
            f"{note}\n"
            "    Explain each one before merging. Do not re-run the deploy and "
            "quote the second number."
        )

    if sync_seen:
        return 0, (
            f"Only the runner checkout sync changed (the three expected tasks "
            f"on {infra_host}). That is the documented "
            "first-deploy-after-commit state. Run this once more; the second "
            "run must be fully clean."
        )

    return 0, "clean — nothing changed; deployed state matches the commit."


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assert a deploy proved deployed state equals HEAD."
    )
    parser.add_argument(
        "--infra-host",
        default="svc-infra",
        help="inventory host whose runner-checkout sync is allowlisted",
    )
    parser.add_argument(
        "--log",
        help="parse a recorded deploy log instead of running one (for tests)",
    )
    parser.add_argument(
        "deploy_command",
        nargs=argparse.REMAINDER,
        help="the deploy to run, after --, e.g. -- make infra USE_VAULT_FILE=1",
    )
    arguments = parser.parse_args()

    if arguments.log:
        text = Path(arguments.log).read_text(encoding="utf-8")
    else:
        command = [word for word in arguments.deploy_command if word != "--"]
        if not command:
            parser.error("give a deploy command after --, or use --log")
        # Streamed line by line rather than captured: an operator watching a
        # deploy needs to see it happen. tee-ing into memory keeps both.
        captured: list[str] = []
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            captured.append(line)
        process.wait()
        text = "".join(captured)
        if process.returncode != 0:
            print(
                f"\nCOULD NOT LOOK: the deploy failed (exit "
                f"{process.returncode}). No proof is claimed.",
                file=sys.stderr,
            )
            return 2

    try:
        changed = parse_changed(text)
    except NoRecapError as error:
        print(f"\nCOULD NOT LOOK: {error}", file=sys.stderr)
        return 2

    code, message = verdict(changed, arguments.infra_host)
    stream = sys.stdout if code == 0 else sys.stderr
    print(f"\n{'PROOF OK' if code == 0 else 'PROOF FAILED'}: {message}", file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
