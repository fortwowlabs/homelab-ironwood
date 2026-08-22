#!/usr/bin/env python3
"""Both hand-maintained release maps must name images this repo still pins.

inventory/group_vars/all/main.yml carries two of them, and they are the
hand-maintained parts of the weekly release report — which makes them the parts
most likely to rot:

  release_feed_overrides   image -> upstream GitHub repository
  release_version_probes   image -> a URL and a regex that reads its version

An image gets removed from a catalog and its entry lingers, pointing the report
at a project this estate no longer runs. Because an orphan entry simply never
matches anything, it produces no error, no warning and no visible symptom. It
just sits there being wrong.

This gate covered only the first map when it was written, and the second was
added later with the identical failure mode and no gate — which is the whole
argument for the gate repeated as an oversight. Both are checked now.

The probe map gets two checks the feed map does not need, because a probe is
code rather than a name:

  - the pattern must COMPILE, and
  - it must have exactly ONE capture group.

Neither is pedantry. A pattern that does not compile, or that captures nothing,
yields no version — and the report then reads `unknown-version`, which is a
truthful-looking answer that quietly hides a broken probe. That is the same
"could not look rendering as an all-clear" this repo keeps writing down.

Shaped after validate_scan_image_coverage.py.

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

import yaml

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "catalog"

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
PROBE_BLOCK_RE = re.compile(
    r"^release_version_probes:\s*$(.*?)(?=^\S|\Z)", re.MULTILINE | re.DOTALL)


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


def probes() -> dict[str, dict[str, str]]:
    """The image -> {url, pattern} map, parsed as YAML.

    PARSE EACH MAP THE WAY ITS CONSUMER DOES. That is the rule these two
    functions follow, and it is why they differ:

      release_feed_overrides  is read by scripts/release_check.py's own regex
                              parser, so overrides() mirrors that regex exactly.
                              A gate that parsed it more cleverly than the
                              script would pass on input the script misreads.
      release_version_probes  is read by ANSIBLE, as YAML. So this uses YAML.

    The first version of this hand-rolled an indentation parser and stripped
    quotes off both ends of each value, which broke in two ways a code review
    caught before either could bite:

      - It compiled and group-counted a DIFFERENT STRING from the one Ansible
        uses. `'"version":\\s*"([^"]+)"'` had its outer YAML quoting stripped
        AND then its real leading and trailing `"` — so the gate was validating
        `version":\\s*"([^"]+)`. A valid pattern ending in an escaped quote
        would fail as `bad escape`, and a pattern with a trailing inline comment
        would keep the comment inside the regex, still compile, still count one
        group, and pass while the live probe matched nothing.
      - A flow-style entry (`image: {url: ..., pattern: ...}`) was absorbed as a
        field of the previous image. No entry was created, so its name, url and
        pattern went unchecked and the gate still printed OK.

    Both were the same underlying mistake: reimplementing YAML badly instead of
    using it. A parse failure here is a GATE FAILURE, not an empty result —
    including the case where somebody writes an unquoted Jinja value, which YAML
    reads as a flow mapping. The message says to quote it.
    """
    text = (ROOT / "inventory/group_vars/all/main.yml").read_text(encoding="utf-8")
    block = PROBE_BLOCK_RE.search(text)
    if not block:
        return {}
    document = "release_version_probes:\n" + block.group(1)
    loaded = yaml.safe_load(document)
    return (loaded or {}).get("release_version_probes") or {}


def check_probes(pins: set[str], mapped: dict[str, dict[str, str]]) -> list[str]:
    problems: list[str] = []
    for image, spec in sorted(mapped.items()):
        if not isinstance(spec, dict):
            # A scalar or list here means the entry was written in a shape the
            # playbook cannot loop over. Caught explicitly so it reports as a
            # malformed probe rather than raising an AttributeError below.
            problems.append(
                f"release_version_probes[{image!r}] is a "
                f"{type(spec).__name__}, not a mapping with url and pattern.")
            continue
        if image not in pins:
            problems.append(
                f"release_version_probes names {image!r}, which no catalog pins. "
                "Either the image was removed and this probe should go with it, "
                "or the key is misspelled — in which case the probe has been "
                "silently doing nothing and the image reads as unknown-version.")
        for field in ("url", "pattern"):
            if not spec.get(field):
                problems.append(
                    f"release_version_probes[{image!r}] has no {field}. "
                    "A probe missing either one cannot produce a version, and "
                    "the image would read as unmeasured with nothing to say why.")
        pattern = spec.get("pattern")
        if not pattern:
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            problems.append(
                f"release_version_probes[{image!r}] pattern does not compile: "
                f"{error}. It would capture nothing and the image would read as "
                "unknown-version — a truthful-looking answer hiding a broken probe.")
            continue
        if compiled.groups != 1:
            problems.append(
                f"release_version_probes[{image!r}] pattern has "
                f"{compiled.groups} capture groups; it needs exactly 1. "
                "The playbook takes group 1 as the version.")
    return problems


def main() -> int:
    pins = pinned_images()
    mapped = overrides()
    try:
        probe_map = probes()
    except yaml.YAMLError as error:
        # Never degrade to an empty map. That is the difference between "there
        # are no probes" and "the probe block does not parse", and reading the
        # second as the first is how a gate passes while measuring nothing.
        print("release_version_probes does not parse as YAML:\n", file=sys.stderr)
        print(f"  {error}\n", file=sys.stderr)
        print("  An unquoted Jinja value is the usual cause — YAML reads a bare\n"
              "  {{ ... }} as a flow mapping. Quote it.", file=sys.stderr)
        return 1
    if not isinstance(probe_map, dict):
        print(f"release_version_probes parsed as {type(probe_map).__name__}, "
              "not a mapping.", file=sys.stderr)
        return 1
    problems: list[str] = check_probes(pins, probe_map)

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

    if not probe_map:
        # Not fatal on its own — the map is optional — but silence here after it
        # was populated would mean the block parser broke, and every probed
        # image would quietly revert to unknown-version.
        problems.append(
            "release_version_probes parsed as empty. If the map really was "
            "removed, delete this check with it; otherwise the block parser "
            "has broken and three images have silently stopped being measured.")

    if problems:
        print("release map problems:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}\n", file=sys.stderr)
        return 1

    print(f"release maps OK: {len(mapped)} feed override(s) and "
          f"{len(probe_map)} version probe(s), all naming one of "
          f"{len(pins)} pinned image(s); every probe pattern compiles "
          f"with exactly one capture group.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
