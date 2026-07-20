#!/usr/bin/env python3
"""Require output suppression on Ansible tasks that can touch vault data."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "roles"
CHILD_BLOCKS = {
    "always",
    "block",
    "handlers",
    "post_tasks",
    "pre_tasks",
    "rescue",
    "tasks",
}
VAULT_RE = re.compile(r"\bvault_[A-Za-z0-9_]+\b")


def sensitive_templates() -> set[str]:
    result: set[str] = set()
    for template in ROLES.glob("*/templates/**/*"):
        if template.is_file() and VAULT_RE.search(
            template.read_text(encoding="utf-8", errors="replace")
        ):
            result.add(template.name)
    return result


def direct_task_is_sensitive(task: dict[str, Any], template_names: set[str]) -> bool:
    direct = {key: value for key, value in task.items() if key not in CHILD_BLOCKS}
    rendered = yaml.safe_dump(direct)
    if VAULT_RE.search(rendered):
        return True
    for module_name in ("ansible.builtin.template", "template"):
        module_args = task.get(module_name)
        if isinstance(module_args, dict):
            source = module_args.get("src")
            if isinstance(source, str) and Path(source).name in template_names:
                return True
    return False


def walk_tasks(
    node: Any,
    source: Path,
    template_names: set[str],
    failures: list[str],
    inherited_no_log: bool = False,
    inherited_diff_false: bool = False,
) -> None:
    if isinstance(node, list):
        for item in node:
            walk_tasks(
                item,
                source,
                template_names,
                failures,
                inherited_no_log,
                inherited_diff_false,
            )
        return
    if not isinstance(node, dict):
        return

    effective_no_log = task_no_log = node.get("no_log", inherited_no_log)
    effective_diff_false = task_diff = node.get("diff", not inherited_diff_false) is False
    if direct_task_is_sensitive(node, template_names):
        name = node.get("name", "unnamed task")
        if task_no_log is not True:
            failures.append(f"{source.relative_to(ROOT)}: {name!r} needs no_log: true")
        if task_diff is not True:
            failures.append(f"{source.relative_to(ROOT)}: {name!r} needs diff: false")

    for child_key in CHILD_BLOCKS:
        if child_key in node:
            walk_tasks(
                node[child_key],
                source,
                template_names,
                failures,
                effective_no_log is True,
                effective_diff_false,
            )


def main() -> int:
    template_names = sensitive_templates()
    failures: list[str] = []
    task_files = list(ROLES.glob("*/tasks/*.yml"))
    task_files.append(ROOT / "preflight.yml")
    for task_file in sorted(task_files):
        try:
            document = yaml.safe_load(task_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            failures.append(f"{task_file.relative_to(ROOT)}: cannot parse YAML: {error}")
            continue
        walk_tasks(document, task_file, template_names, failures)

    if failures:
        print("Secret-bearing Ansible task validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("Secret-bearing Ansible tasks: no_log + diff suppression OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
