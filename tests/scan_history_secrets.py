#!/usr/bin/env python3
"""Scan every reachable Git blob without ever echoing a matched value."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_BLOB_BYTES = 10_000_000
NON_SECRET_VAULT_KEYS = {
    "vault_mullvad_endpoint_ip",
    "vault_mullvad_endpoint_port",
    "vault_mullvad_peer_pubkey",
    "vault_mullvad_wg_addr",
    "vault_pve_token_id",
    "vault_password_file",
    "vault_re",
}
SECRET_KEY_PATTERN = (
    r"(?:vault_[a-z0-9_]+|[a-z0-9_.-]*(?:password|passwd|token_secret|"
    r"api_key|auth_secret|secret_key|client_secret|private_key)[a-z0-9_.-]*)"
)
QUOTED_ASSIGNMENT_RE = re.compile(
    r"(?:^|[,{;])\s*[+\-]?[\"']?(?P<key>" + SECRET_KEY_PATTERN + r")[\"']?\s*[:=]\s*"
    r"[\"'](?P<value>[^\"'\r\n]{4,})[\"']",
    re.IGNORECASE,
)
BARE_ASSIGNMENT_RE = re.compile(
    r"(?:^|[,{;])\s*[+\-]?[\"']?(?P<key>" + SECRET_KEY_PATTERN + r")[\"']?\s*[:=]\s*"
    r"(?P<value>[^\s,;}]{6,})",
    re.IGNORECASE,
)
SIGNATURES = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(r"\b(?:gh[opsu]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
}
PLACEHOLDER_RE = re.compile(
    r"(?i)^(?:\{\{|\$|<|!(?:env_var|secret)$|[|>][+-]?$|x{4,}|replace|change[_-]?me|example|dummy|fixture|"
    r"redacted|not[_-]?a[_-]?secret|vault_|false$|true$|none$)"
)
REDACTION_MARKER_RE = re.compile(r"(?i)(?:redact|replace|x{4,}|dummy|example)")


def run_git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True
    ).stdout


def reachable_blobs() -> list[tuple[str, str]]:
    objects: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_line in run_git("rev-list", "--objects", "--all").splitlines():
        object_id_raw, separator, path_raw = raw_line.partition(b" ")
        if not separator or object_id_raw in seen:
            continue
        object_id = object_id_raw.decode("ascii")
        if run_git("cat-file", "-t", object_id).strip() != b"blob":
            continue
        seen.add(object_id_raw)
        objects.append((object_id, path_raw.decode("utf-8", errors="replace")))
    return objects


def assignment_is_placeholder(key: str, value: str) -> bool:
    normalized_key = key.lower()
    normalized_value = value.strip().lstrip(r"\`\"'")
    return (
        normalized_key in NON_SECRET_VAULT_KEYS
        or not normalized_value
        or PLACEHOLDER_RE.match(normalized_value) is not None
        or REDACTION_MARKER_RE.search(normalized_value) is not None
        or "{{" in normalized_value
        or "$ANSIBLE_VAULT" in normalized_value
    )


def main() -> int:
    findings: list[str] = []
    scanned = 0
    for object_id, path in reachable_blobs():
        content = run_git("cat-file", "-p", object_id)
        if len(content) > MAX_BLOB_BYTES or b"\0" in content:
            continue
        scanned += 1
        # JSONL transcripts encode command output newlines and quotes. Decode
        # only those separators in memory so assignments remain detectable.
        text = content.decode("utf-8", errors="replace")
        normalized = text.replace(r"\n", "\n").replace(r'\"', '"')
        for line_number, line in enumerate(normalized.splitlines(), 1):
            for category, signature in SIGNATURES.items():
                if signature.search(line):
                    findings.append(
                        f"{object_id[:12]} {path}:{line_number}: {category}"
                    )
            for expression in (QUOTED_ASSIGNMENT_RE, BARE_ASSIGNMENT_RE):
                for match in expression.finditer(line):
                    key = match.group("key")
                    value = match.group("value")
                    if assignment_is_placeholder(key, value):
                        continue
                    findings.append(
                        f"{object_id[:12]} {path}:{line_number}: populated {key.lower()}"
                    )

    findings = sorted(set(findings))
    if findings:
        print(
            "Git history secret scan failed; matched values are intentionally hidden:",
            file=sys.stderr,
        )
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print(f"Git history secret scan: OK ({scanned} unique text blobs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
