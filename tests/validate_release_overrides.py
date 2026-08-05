#!/usr/bin/env python3
"""Every release_feed_overrides key must name an image this repo still pins.

The override map in inventory/group_vars/all/main.yml is the hand-maintained
part of the weekly release report, which makes it the part most likely to rot.
An image gets removed from a catalog and its override lingers, pointing the
report at a project this estate no longer runs — and because an orphan override
simply never matches anything, it produces no error, no warning and no visible
symptom. It just sits there being wrong.

So it is checked here, in the same shape as validate_scan_image_coverage.py.

WHAT THIS GATE CANNOT DO, stated so nobody mistakes a pass for more than it is:

  - It does not check that a repository exists. That needs the network, and
    `make validate` is offline by construction.
  - It does not check that a repository is the RIGHT one. `crocodilestick/
    Calibre-Web-Automated` and `linuxserver/docker-baseimage-ubuntu` are equally
    valid strings; only one of them is Calibre-Web. That is a human judgement
    made once, when the entry is recorded, and the reasoning belongs in the
    comment beside it.

The gate has a positive control of its own: if it finds no overrides and no
pins at all, the parser has broken rather than the repo having emptied, and it
fails rather than passing with nothing checked.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CATALOGS = (
    "inventory/group_vars/all/apps.yml",
    "inventory/group_vars/all/infra-apps.yml",
    "inventory/group_vars/all/main.yml",
    "inventory/group_vars/all/minecraft.yml",
)

# Deliberately the same expressions scripts/release_check.py uses, including
# the comment-line skip. If the two ever disagree, this gate would be checking
# a different set of pins from the one the report reads, which is worse than no
# gate — it would pass while the report failed to find the same image.
PIN_RE = re.compile(r"([A-Za-z0-9._/-]+)@(sha256:[0-9a-f]{64})")
OVERRIDE_BLOCK_RE = re.compile(
    r"^release_feed_overrides:\s*$(.*?)(?=^\S|\Z)", re.MULTILINE | re.DOTALL)
ENTRY_RE = re.compile(r'^"?([A-Za-z0-9._/-]+)"?:\s*"?([^"#]*?)"?\s*(?:#.*)?$')


def pinned_images() -> set[str]:
    images: set[str] = set()
    for relative in CATALOGS:
        path = ROOT / relative
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = PIN_RE.search(line)
            if match:
                images.add(match.group(1))
    return images


def overrides() -> dict[str, str]:
    text = (ROOT / "inventory/group_vars/all/main.yml").read_text(encoding="utf-8")
    block = OVERRIDE_BLOCK_RE.search(text)
    if not block:
        return {}
    found: dict[str, str] = {}
    for line in block.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entry = ENTRY_RE.match(stripped)
        if entry:
            found[entry.group(1)] = entry.group(2).strip()
    return found


def main() -> int:
    pins = pinned_images()
    mapped = overrides()
    problems: list[str] = []

    if not pins:
        problems.append(
            "no pinned images were found in any catalog. This repo pins dozens, "
            "so the pin parser has broken — not the catalogs. Fix PIN_RE here "
            "and in scripts/release_check.py together.")

    for image in sorted(mapped):
        if image not in pins:
            problems.append(
                f"release_feed_overrides names {image!r}, which no catalog pins. "
                "Either the image was removed and this entry should go with it, "
                "or the key is misspelled — in which case the report has been "
                "silently falling back to the image's own label all along.")

    for image, feed in sorted(mapped.items()):
        # An empty value is a deliberate "there is no feed for this". A value
        # that is not owner/repo is a typo that would 404 every week.
        if feed and not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", feed):
            problems.append(
                f"release_feed_overrides[{image!r}] is {feed!r}, which is not a "
                "GitHub owner/repository. Record the repository alone, not a URL.")

    if problems:
        print("release feed override problems:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}\n", file=sys.stderr)
        return 1

    print(f"release feed overrides OK: {len(mapped)} override(s), "
          f"all naming one of {len(pins)} pinned image(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
