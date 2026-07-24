#!/usr/bin/env python3
"""High-signal secret scan for tracked and pending repository files."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATHS = {
    ".vault_pass",
    "inventory/group_vars/all/vault.yml",
}
FORBIDDEN_PREFIXES = ("docs/sessions/", "docs/transcripts/")
TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".conf",
    ".env",
    ".example",
    ".j2",
    ".json",
    ".md",
    ".nft",
    ".service",
    ".sh",
    ".te",
    ".timer",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SIGNATURES = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "GitHub token": re.compile(r"\b(?:gh[opsu]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
}
ASSIGNMENT_RE = re.compile(
    r"(?i)^\s*[A-Za-z0-9_.-]*(?:password|passwd|token_secret|api_key|"
    r"auth_secret|secret_key|client_secret|private_key)[A-Za-z0-9_.-]*\s*[:=]\s*[\"']?([^\s#\"']+)"
)
PLACEHOLDER_RE = re.compile(
    r"(?i)^(?:\{\{|\$|<|!(?:env_var|secret)$|[|>][+-]?$|x{4,}|replace|change[_-]?me|example|dummy|fixture|"
    r"redacted|not[_-]?a[_-]?secret|vault_|/|false$|true$)"
)


def repository_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [part.decode("utf-8") for part in result.stdout.split(b"\0") if part]


def main() -> int:
    findings: list[str] = []
    for relative_name in repository_files():
        relative = Path(relative_name).as_posix()
        path = ROOT / relative
        # `git ls-files --cached` also reports index entries deleted in the
        # worktree; another validation gate checks that deletion itself.
        if not path.is_file():
            continue
        if relative in FORBIDDEN_PATHS or relative.startswith(FORBIDDEN_PREFIXES):
            findings.append(f"{relative}: secret-bearing path must not be committed")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.stat().st_size > 5_000_000:
            findings.append(f"{relative}: text file exceeds the scanner's 5 MB safety limit")
            continue
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            for description, signature in SIGNATURES.items():
                if signature.search(line):
                    findings.append(f"{relative}:{line_number}: possible {description}")
            assignment = ASSIGNMENT_RE.match(line)
            if assignment:
                value = assignment.group(1).strip()
                if value and not PLACEHOLDER_RE.match(value):
                    findings.append(
                        f"{relative}:{line_number}: populated secret-like assignment"
                    )

    if findings:
        print("Secret scan failed (values are intentionally not displayed):", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print("Repository secret scan: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
