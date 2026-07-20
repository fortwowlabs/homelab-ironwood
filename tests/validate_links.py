#!/usr/bin/env python3
"""Fail when a repository Markdown link points at a missing local path."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def repository_markdown() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "*.md",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return [ROOT / name for name in result.stdout.splitlines()]


def link_target(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        return raw[1 : raw.index(">")]
    # Markdown permits a quoted title after a whitespace-separated target.
    return raw.split(maxsplit=1)[0]


def main() -> int:
    failures: list[str] = []
    for document in repository_markdown():
        if not document.is_file():
            continue
        in_fence = False
        for line_number, line in enumerate(
            document.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for match in LINK_RE.finditer(line):
                target = link_target(match.group(1))
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                if parsed.path.startswith("/"):
                    failures.append(
                        f"{document.relative_to(ROOT)}:{line_number}: "
                        f"repository documentation must not use absolute path {target!r}"
                    )
                    continue
                destination = (document.parent / unquote(parsed.path)).resolve()
                if not destination.exists():
                    failures.append(
                        f"{document.relative_to(ROOT)}:{line_number}: missing {target!r}"
                    )

    if failures:
        print("Local Markdown link validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("Local Markdown links: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
