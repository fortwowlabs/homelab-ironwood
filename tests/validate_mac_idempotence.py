#!/usr/bin/env python3
"""Require every command/shell task in roles/mac_control to set changed_when.

CLAUDE.md leans harder on `changed=0` than on any other single signal: a deploy
that reports it against a clean tree is the proof that what is running equals
what is committed. Every Linux role here earns that mostly for free, because
Ansible modules know whether they changed anything.

macOS does not work that way. Its state is mostly not file-shaped — pmset,
systemsetup, scutil, launchctl, ollama — so the role is largely `command`
tasks, and Ansible reports every one of them as changed unless told otherwise.
A role with one bare command can never report changed=0, and the proof stops
meaning anything for the whole estate rather than just for this host.

The opposite mistake is worse and this gate does NOT catch it: `changed_when:
false` on a task that really does change something reports idempotence without
having it. That is why the rule is written down in the spec and why every
setter here is paired with a reader it compares against.

The role must exist and must contain at least one command task, so this cannot
pass by finding nothing to check.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/mac_control"

COMMAND_MODULES = {
    "command", "shell", "ansible.builtin.command", "ansible.builtin.shell",
    "raw", "ansible.builtin.raw", "script", "ansible.builtin.script",
}


def iter_tasks(node, path):
    """Yield (task, path) for every task-shaped mapping, including nested blocks."""
    if isinstance(node, list):
        for item in node:
            yield from iter_tasks(item, path)
    elif isinstance(node, dict):
        if any(key in node for key in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                if key in node:
                    yield from iter_tasks(node[key], path)
        else:
            yield node, path


def main() -> int:
    if not ROLE.is_dir():
        print(f"roles/mac_control does not exist ({ROLE})", file=sys.stderr)
        return 1

    task_files = sorted((ROLE / "tasks").glob("*.yml"))
    if not task_files:
        print("roles/mac_control/tasks contains no task files", file=sys.stderr)
        return 1

    failures = []
    command_tasks = 0

    for path in task_files:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for task, _ in iter_tasks(loaded, path):
            modules = COMMAND_MODULES & set(task)
            if not modules:
                continue
            command_tasks += 1
            if "changed_when" not in task:
                name = task.get("name", "<unnamed>")
                failures.append(
                    f"{path.relative_to(ROOT)}: task {name!r} uses "
                    f"{sorted(modules)[0]} without changed_when")

    if command_tasks == 0:
        print(
            "roles/mac_control has no command/shell tasks at all. That is almost\n"
            "certainly wrong for a macOS role, and a gate that finds nothing to\n"
            "check is a gate nobody can tell is broken.",
            file=sys.stderr)
        return 1

    if failures:
        print("mac_control idempotence regressions:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}\n", file=sys.stderr)
        print("  Every command task must derive changed_when from a reader task.\n"
              "  `make mac` twice must report changed=0 on the second run.\n",
              file=sys.stderr)
        return 1

    print(f"mac_control idempotence: OK ({command_tasks} command tasks, all with changed_when)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
