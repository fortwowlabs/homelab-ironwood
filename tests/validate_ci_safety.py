#!/usr/bin/env python3
"""Prove hosted validation is fixture-only and cannot address the homelab."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/validate.yml"
REQUIRED = (
    "pull_request:",
    "contents: read",
    "ANSIBLE_INVENTORY: tests/fixtures/inventory.yml",
    "make validate",
    "fetch-depth: 0",
)
FORBIDDEN = (
    "inventory/hosts.yml",
    ".vault_pass",
    "--ask-vault-pass",
    "make deploy",
    "make preflight",
    "make verify",
    "make check",
)


def main() -> int:
    if not WORKFLOW.is_file():
        print("missing .github/workflows/validate.yml", file=sys.stderr)
        return 1
    content = WORKFLOW.read_text(encoding="utf-8")
    failures = [f"workflow is missing {value!r}" for value in REQUIRED if value not in content]
    failures.extend(
        f"workflow contains forbidden live-run input {value!r}"
        for value in FORBIDDEN
        if value in content
    )
    for address in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", content):
        if not address.startswith("127."):
            failures.append(f"workflow contains non-loopback IP address {address}")

    if failures:
        print("Hosted CI safety validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("Hosted CI safety: fixture-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
