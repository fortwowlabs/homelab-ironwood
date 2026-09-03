# Deploy Proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn step 6 of the change workflow — "the deploy must report `changed=0`, and if svc-infra reports exactly the three sync tasks, deploy again" — from a rule a human reads off a recap into a command that passes or fails.

**Architecture:** A Python wrapper runs the deploy, streams its output through unchanged, and parses which *tasks* changed on which *hosts*. It exits 0 only when nothing changed, or when the sole changes are the three known `.deployed-rev` sync tasks on the infra host. It exits 2 — not 1 — when it could not find a `PLAY RECAP` at all, so "the deploy did not finish" is a distinct state from "the deploy showed drift". The parser is separated from the runner so it can be exercised offline against fixture logs, including the failure cases a live estate will almost never produce.

**Tech Stack:** Python 3.13, `ansible-playbook` default callback with `callback_result_format = yaml`, GNU Make.

**Spec:** The architecture review of 2026-09-03 (this repo, conversation record). Finding #9.

## Global Constraints

- The wrapper must **stream the deploy's output live**. An operator watching a deploy needs to see it happen; capturing silently and printing at the end is not acceptable.
- The wrapper must never re-run the deploy on its own. `CLAUDE.md` warns explicitly against papering over a genuine diff by deploying twice and quoting the second number — an automatic retry would build that mistake into a tool.
- "Could not look" is exit **2**, distinct from "found drift" at exit **1**. This mirrors `container-drift.sh` and `homelab-metric-write`, and exists for the same reason: a check that cannot distinguish those two states is one nobody can tell is broken.
- The allowlisted sync tasks are matched by **exact name on the infra host only**, and only as the complete set of three. A subset is not "less drift", it is a different situation that has not been reasoned about — fail it.
- No `homelab-metric-write` emission from this tool. It runs on the workstation during a deploy, not on svc-infra, and has nowhere to publish to.

---

## Background: why exactly three, and which three

`roles/svc_infra/tasks/verify-runner.yml:48-75` keeps a `git archive` of the committed tree at `/opt/homelab-iac`, with the deployed revision in `.deployed-rev`. The block is gated on that file disagreeing with `git rev-parse HEAD`. Immediately after a commit it always disagrees, so three tasks always change:

1. `Build a tracked-files archive of the committed tree` — `delegate_to: localhost`, `changed_when: true`
2. `Unpack the archive onto the runner`
3. `Record the deployed revision`

Because task 1 is delegated, the default callback prints it as `changed: [svc-infra -> localhost]`. The parser must accept that form and attribute it to `svc-infra`, not to `localhost`.

The fourth task in that block, `Remove the local archive`, is in an `always:` and carries `changed_when: false`, so it never appears.

## File Structure

- Create: `scripts/deploy_proof.py` — the parser and the runner. One file: the parser is ~60 lines and splitting it into a module for one consumer would be ceremony.
- Create: `tests/validate_deploy_proof.py` — the gate, `GATE_GROUP = "ci"`.
- Create: `tests/fixtures/deploy-proof/clean.log`, `infra-sync.log`, `drift.log`, `truncated.log` — recorded and hand-trimmed deploy output.
- Modify: `Makefile` — a `deploy-proof` target and its `.PHONY` entry.
- Modify: `CLAUDE.md` — replace the procedural instruction in "Second caveat" with the command, keeping the explanation of *why* three.

---

### Task 1: The parser, with its failure cases pinned first

**Files:**
- Create: `scripts/deploy_proof.py`
- Create: `tests/validate_deploy_proof.py`
- Create: `tests/fixtures/deploy-proof/clean.log`, `infra-sync.log`, `drift.log`, `truncated.log`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `parse_changed(text: str) -> dict[str, list[str]]` — host → list of changed task names, in order of first appearance. Raises `NoRecapError` when the text contains no `PLAY RECAP`.
  - `class NoRecapError(Exception)`
  - `verdict(changed: dict[str, list[str]], infra_host: str) -> tuple[int, str]` — returns `(exit_code, human_message)`.
  - `SYNC_TASKS: tuple[str, str, str]` — the three allowlisted names.

- [ ] **Step 1: Write the fixtures**

Create `tests/fixtures/deploy-proof/clean.log` — a minimal but structurally faithful deploy tail:

```
TASK [svc_media : Render rootless media Quadlets] ******************************
ok: [svc-media]

TASK [svc_media : Verify all rootless media services are active] ***************
ok: [svc-media]

PLAY RECAP *********************************************************************
svc-media                  : ok=142  changed=0    unreachable=0    failed=0    skipped=18   rescued=0    ignored=0
svc-infra                  : ok=201  changed=0    unreachable=0    failed=0    skipped=24   rescued=0    ignored=0
```

Create `tests/fixtures/deploy-proof/infra-sync.log` — the expected post-commit shape, including the delegated form:

```
TASK [svc_infra : Build a tracked-files archive of the committed tree] *********
changed: [svc-infra -> localhost]

TASK [svc_infra : Unpack the archive onto the runner] **************************
changed: [svc-infra]

TASK [svc_infra : Record the deployed revision] ********************************
changed: [svc-infra]

PLAY RECAP *********************************************************************
svc-infra                  : ok=201  changed=3    unreachable=0    failed=0    skipped=24   rescued=0    ignored=0
```

Create `tests/fixtures/deploy-proof/drift.log` — a genuine diff that must not be waved through, including the case that matters most: real drift sitting *alongside* the allowlisted trio:

```
TASK [svc_infra : Build a tracked-files archive of the committed tree] *********
changed: [svc-infra -> localhost]

TASK [svc_infra : Unpack the archive onto the runner] **************************
changed: [svc-infra]

TASK [svc_infra : Record the deployed revision] ********************************
changed: [svc-infra]

TASK [svc_infra : Render the Authelia configuration] ***************************
changed: [svc-infra]

PLAY RECAP *********************************************************************
svc-infra                  : ok=201  changed=4    unreachable=0    failed=0    skipped=24   rescued=0    ignored=0
```

Create `tests/fixtures/deploy-proof/truncated.log` — a deploy that died before reporting:

```
TASK [svc_infra : Render the Authelia configuration] ***************************
changed: [svc-infra]

ERROR! The task includes an option with an undefined variable.
```

- [ ] **Step 2: Write the gate that these fixtures drive**

Create `tests/validate_deploy_proof.py`:

```python
#!/usr/bin/env python3
"""Exercise deploy_proof's verdict logic, especially the ways it must FAIL.

scripts/deploy_proof.py decides whether a deploy proved that what is running
equals what is committed. On a healthy estate its answer is always "clean",
which makes it exactly the kind of check that can stop working without anyone
noticing — the failure this repo keeps writing down.

So the interesting cases are exercised here, offline, against recorded output:

  clean       nothing changed anywhere                              -> 0
  infra-sync  only the three known .deployed-rev sync tasks         -> 0
  drift       the sync trio PLUS a real change                      -> 1
  truncated   the deploy never reached PLAY RECAP                   -> 2

`drift` is the one that earns this file. CLAUDE.md warns against papering over
a genuine diff by deploying twice and quoting the second number; a tool that
allowlisted the trio by COUNT rather than by name would do precisely that, and
would pass the infra-sync fixture while waving the drift one through.

`truncated` is the second: a deploy that died has proved nothing, and must not
be reported as the same thing as a deploy that changed nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from deploy_proof import NoRecapError, parse_changed, verdict  # noqa: E402

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "ci"

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/deploy-proof"
INFRA_HOST = "svc-infra"

CASES = (
    ("clean.log", 0, "clean"),
    ("infra-sync.log", 0, "runner checkout"),
    ("drift.log", 1, "Authelia"),
    ("truncated.log", 2, "PLAY RECAP"),
)


def main() -> int:
    problems: list[str] = []
    for name, expected_code, expected_fragment in CASES:
        text = (FIXTURES / name).read_text(encoding="utf-8")
        try:
            changed = parse_changed(text)
        except NoRecapError as error:
            code, message = 2, str(error)
        else:
            code, message = verdict(changed, INFRA_HOST)

        if code != expected_code:
            problems.append(
                f"{name}: expected exit {expected_code}, got {code} — {message}"
            )
        elif expected_fragment.lower() not in message.lower():
            problems.append(
                f"{name}: exit {code} was right but the message did not mention "
                f"{expected_fragment!r}: {message}"
            )

    if problems:
        print("deploy_proof verdict validation failed:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(f"deploy_proof: OK ({len(CASES)} cases, including 2 that must fail)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Run the gate to verify it fails**

Run: `.venv/bin/python tests/validate_deploy_proof.py`

Expected: FAIL with `ModuleNotFoundError: No module named 'deploy_proof'`. That is the correct first failure.

- [ ] **Step 4: Write the parser and verdict**

Create `scripts/deploy_proof.py`:

```python
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
as the complete set.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

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
    """Map each host to the task names that reported `changed` for it."""
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
            changed.setdefault(host, []).append(current_task)
    return changed


def verdict(changed: dict[str, list[str]], infra_host: str) -> tuple[int, str]:
    """Return (exit code, message) for a parsed set of changed tasks."""
    unexplained: list[str] = []
    sync_seen = False

    for host, tasks in sorted(changed.items()):
        if host == infra_host and sorted(tasks) == sorted(SYNC_TASKS):
            sync_seen = True
            continue
        unexplained.extend(f"{host}: {task}" for task in tasks)

    if unexplained:
        lines = "\n".join(f"    {entry}" for entry in unexplained)
        return 1, (
            "Deployed state does not match the commit. These tasks reported "
            f"changed and are not the runner checkout sync:\n{lines}\n"
            "    Explain each one before merging. Do not re-run the deploy and "
            "quote the second number."
        )

    if sync_seen:
        return 0, (
            "Only the runner checkout sync changed (the three expected tasks "
            "on "
            f"{infra_host}). That is the documented first-deploy-after-commit "
            "state. Run this once more; the second run must be fully clean."
        )

    return 0, "clean — nothing changed; deployed state matches the commit."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
        help="the deploy to run, e.g. -- make infra USE_VAULT_FILE=1",
    )
    arguments = parser.parse_args()

    if arguments.log:
        text = open(arguments.log, encoding="utf-8").read()
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
                f"\nDEPLOY FAILED (exit {process.returncode}). No proof is "
                "claimed.",
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
```

- [ ] **Step 5: Run the gate and confirm all four cases pass**

Run: `.venv/bin/python tests/validate_deploy_proof.py`

Expected: `deploy_proof: OK (4 cases, including 2 that must fail)`

If `drift.log` returns 0, the allowlist is matching by count somewhere — that is the bug this fixture exists to catch, and it must be fixed, not the fixture.

- [ ] **Step 6: Confirm the gate is discovered**

Run: `make validate-ci`

Expected: the new gate appears in the echoed command list alongside `validate_ci_safety.py`, `validate_verify_safety.py` and `validate_scan_readonly.py`, and passes.

- [ ] **Step 7: Commit**

```bash
git add scripts/deploy_proof.py tests/validate_deploy_proof.py tests/fixtures/deploy-proof
git commit -m "feat: prove changed=0 mechanically instead of by reading a recap

Step 6 of the workflow was a rule a human applies at the end of a deploy,
with a documented exception (svc-infra's runner sync reports changed=3
right after a commit) and a warning not to paper over a real diff by
deploying twice.

The allowlist matches the three sync tasks BY NAME, not by count: the
drift fixture carries the trio plus a real Authelia change, and a
count-based check would wave it through. 'Could not look' exits 2, so a
deploy that died is not reported as a deploy that changed nothing."
```

---

### Task 2: The Make target and the workflow documentation

**Files:**
- Modify: `Makefile`
- Modify: `CLAUDE.md` — the "Second caveat" paragraph under "Why the commit comes before the final deploy"

**Interfaces:**
- Consumes: `scripts/deploy_proof.py` from Task 1.
- Produces: `make deploy-proof TARGET=<target>`. Nothing later consumes it.

- [ ] **Step 1: Add the target**

In `Makefile`, after the `verify` target:

```make
TARGET ?= deploy

deploy-proof: ## Run TARGET=<deploy|dl|media|infra> and assert changed=0 mechanically
	@$(PYTHON) scripts/deploy_proof.py -- $(MAKE) $(TARGET) $(if $(USE_VAULT_FILE),USE_VAULT_FILE=$(USE_VAULT_FILE),) ARGS="$(ARGS)"
```

Add `deploy-proof` to `.PHONY`.

- [ ] **Step 2: Verify the target wires up without deploying**

Run: `make deploy-proof TARGET=nonexistent-target`

Expected: the wrapper runs `make nonexistent-target`, make fails, and the wrapper reports `DEPLOY FAILED (exit 2). No proof is claimed.` with exit 2. This confirms the plumbing without touching a VM.

- [ ] **Step 3: Rewrite the caveat in CLAUDE.md**

Find the paragraph beginning "**Second caveat: on svc-infra the first deploy after any commit reports `changed=3`, not `changed=0`.**" Keep the whole explanation of *why* three — it is the reason the allowlist is what it is. Replace only the closing instruction, currently:

> So step 6 in practice is: deploy, and if svc-infra reports exactly those three, deploy once more and require `changed=0` from the second run. Anything else still has to be explained before merging. Do not paper over a genuine diff by running the deploy twice and quoting the second number — check *which* tasks changed.

with:

> So step 6 in practice is `make deploy-proof TARGET=infra`, which does the checking. It parses which *tasks* changed rather than how many, allowlists exactly the three sync tasks above on svc-infra only, and exits non-zero on anything else — including the case a count-based check would wave through, where the sync trio and a real change land in the same run. A deploy that never reached `PLAY RECAP` exits 2 rather than 0, because a run that died has proved nothing.
>
> It deliberately does not re-run the deploy for you. When it reports the sync-only state, run it again yourself and require a fully clean second run.

- [ ] **Step 4: Check the links gate still passes**

Run: `make validate-links`

Expected: PASS. `CLAUDE.md` was edited; if the link checker covers it, a mangled reference shows up here.

- [ ] **Step 5: Full validation**

Run: `make validate`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add Makefile CLAUDE.md
git commit -m "docs: make step 6 a command rather than a rule to remember

make deploy-proof TARGET=infra now performs the check the Second caveat
described in prose. The explanation of why svc-infra reports three stays;
the instruction to apply it by hand goes."
```

---

## Verification before merge

The gate and the fixtures are offline, so `make validate` is most of the evidence. The one thing it cannot prove is that `TASK_RE` and `CHANGED_RE` match this repo's *real* callback output, because the fixtures are hand-written.

**That gap has to be closed by a live run before merging, and it needs the operator** — deploys are not available to an agent in this environment.

- [ ] `make validate` passes.
- [ ] `git status --porcelain` prints nothing.
- [ ] **Operator step:** run `make deploy-proof TARGET=infra USE_VAULT_FILE=1` immediately after committing. Expect the sync-only verdict, exit 0, message mentioning the runner checkout.
- [ ] **Operator step:** run it a second time. Expect `PROOF OK: clean`, exit 0.
- [ ] If either run reports unexplained tasks that are genuinely expected, the regexes are mis-parsing real output — capture that output into a new fixture and fix the parser. Do **not** widen the allowlist to make a live run pass.

## Merge

Standard workflow, with the two operator steps above standing in for the usual deploy evidence. Confirm the clean tree, merge to `main`, push, delete the branch.
