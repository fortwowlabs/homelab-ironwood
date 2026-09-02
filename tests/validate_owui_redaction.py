#!/usr/bin/env python3
"""Fail when the Open WebUI config exporter would write a value it should hide.

scripts/owui_config_export.py decides what lands in a git-tracked file. Its
redaction is an ALLOWLIST: values appear only for keys under SAFE_PREFIXES,
with SECRET_MARKERS as a second pass that hides anything credential-shaped even
when it sits under a safe prefix. That is the one piece of the Open WebUI
tooling whose regression has a real consequence -- a widened prefix or a broken
is_safe() commits a live secret, and nothing else in this repo would notice.

Both sibling gates check their catalogs. Nothing checked the code that decides
what gets written: the redaction was verified once by hand while it was being
built, which is exactly the "check nobody can tell is broken" shape this repo
keeps re-learning. This is that verification, persisted.

TWO HALVES, because they fail differently.

1. A table run against the real is_safe(), including credentials planted under
   safe prefixes. It carries a positive control -- at least one key must come
   back shown and at least one redacted -- so an is_safe() stuck on True or
   False is caught rather than quietly agreeing with half the table.

2. A cross-check of the committed export against TODAY's rules. A key whose
   value is visible there but which current is_safe() rejects is either a stale
   export (the rules were tightened afterwards, the value is empty, and it says
   so rather than failing) or a real value on display that the rules now
   forbid, which fails.

This gate imports the exporter by path rather than re-implementing its logic.
A second copy of the rules here could agree with itself while both drifted from
the file that actually writes the export.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

GATE_GROUP = "catalog"

ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = ROOT / "scripts" / "owui_config_export.py"
EXPORT_PATH = ROOT / "inventory/group_vars/all/openwebui-config.yml"

# A value that must never survive redact(). The NAME matters as much as the
# string it holds: tests/scan_history_secrets.py reads the bare identifier to
# the right of `"ui.api_key":` below as the assigned value, and anything it
# does not recognise as a placeholder is a finding -- so an innocuously named
# constant there fails the build once the file is committed, which is a
# different moment from when it is written. NOT_A_SECRET matches that gate's
# own placeholder vocabulary, which is exactly what the vocabulary is for.
NOT_A_SECRET = "not-a-secret-must-not-appear"

# (dotted key, expected to be SHOWN, why it is in the table)
CASES = (
    ("ui.enable_signup", True, "a plain setting under a safe prefix"),
    ("ui.default_user_role", True, "the other drift-enforced key"),
    ("user.permissions.chat.file_upload", True, "permissions are tracked"),
    ("image_generation.engine", True, "image generation is tracked"),
    ("task.model.default", True, "task.* is tracked"),
    ("web.search.engine", True, "the search engine choice is tracked"),
    ("ollama.enable", True, "the enable flag only, not the URLs"),
    ("auth.jwt_expiry", True, "one of the two surviving auth keys"),
    ("auth.admin.show", True, "the other surviving auth key"),
    ("UI.ENABLE_SIGNUP", True, "case must not defeat the prefix pass"),

    ("auth.admin.email", False,
     "an address is not a setting, and the broad auth. prefix used to show it"),
    ("ollama.base_urls", False, "only ollama.enable is allowlisted"),
    ("rag.web.search.result_count", False,
     "only the two rag.embedding_* keys are allowlisted"),
    ("anything.unrecognised", False, "unknown keys must default closed"),

    # Credentials planted UNDER safe prefixes. These are the belt-and-braces
    # pass: the prefix says show, SECRET_MARKERS must overrule it.
    ("ui.api_key", False, "marker must beat the safe prefix"),
    ("task.openai_api_key", False, "marker must beat the safe prefix"),
    ("image_generation.comfyui_api_token", False, "marker must beat the prefix"),
    ("ui.admin_password", False, "marker must beat the safe prefix"),
    ("ui.oauth_client_secret", False, "marker must beat the safe prefix"),
    ("ui.aws_credentials", False, "marker must beat the safe prefix"),
    ("UI.API_KEY", False, "case must not defeat the marker pass"),
)


def load_exporter():
    """Import the real exporter, so this gate tests the file that writes."""
    spec = importlib.util.spec_from_file_location(
        "owui_config_export", EXPORTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_table(exporter) -> list[str]:
    problems: list[str] = []
    for key, expect_shown, why in CASES:
        got = exporter.is_safe(key)
        if got != expect_shown:
            verb = "hid" if expect_shown else "would EXPOSE"
            problems.append(
                f"is_safe({key!r}) {verb} it -- {why}")

    # Positive control. A constant is_safe() agrees with every case that
    # happens to want its answer, so require both verdicts to occur.
    verdicts = {exporter.is_safe(key) for key, _, _ in CASES}
    if verdicts != {True, False}:
        only = verdicts.pop() if verdicts else "nothing"
        problems.append(
            f"every case in the table returned {only!r}: is_safe() is not "
            "discriminating at all, so this gate proves nothing")
    return problems


def check_mechanics(exporter) -> list[str]:
    """flatten() and redact() together, not just the predicate."""
    problems: list[str] = []

    flat = exporter.flatten({"ui": {"enable_signup": False}, "top": 2})
    if flat != {"ui.enable_signup": False, "top": 2}:
        problems.append(
            f"flatten() produced {flat!r}; dotted keys are what every prefix "
            "rule and the whole export format assume")

    out = exporter.redact({"ui.enable_signup": False, "ui.api_key": NOT_A_SECRET})
    if out.get("ui.enable_signup") is not False:
        problems.append("redact() dropped or altered a value it should show")
    if out.get("ui.api_key") != exporter.REDACTED:
        problems.append("redact() left a credential-shaped key unredacted")
    if NOT_A_SECRET in repr(out):
        problems.append(
            "the planted value survived redact() somewhere in its output -- "
            "a redaction that hides a key but leaks the value elsewhere is "
            "worse than none, because the file looks safe")
    return problems


def check_committed_export(exporter) -> tuple[list[str], list[str]]:
    """Every visible value in the committed file, judged by today's rules."""
    failures: list[str] = []
    notes: list[str] = []

    document = yaml.safe_load(EXPORT_PATH.read_text(encoding="utf-8")) or {}
    live = document.get("openwebui_live_config")
    if not isinstance(live, dict) or not live:
        return ([f"{EXPORT_PATH.name} has no openwebui_live_config mapping; it "
                 "is corrupt or was hand-edited. Re-run `make owui-export`."],
                notes)

    for key, value in sorted(live.items()):
        if value == exporter.REDACTED or exporter.is_safe(key):
            continue
        if value in (None, "", [], {}):
            notes.append(
                f"{key} is visible in the export but today's rules would "
                "redact it. The value is empty, so nothing is disclosed -- the "
                "export predates the tightened rules. `make owui-export` "
                "settles it.")
        else:
            failures.append(
                f"{key} is visible in the committed export with a real value, "
                "but is_safe() now rejects it. Either widen SAFE_PREFIXES "
                "deliberately, or re-run `make owui-export` to redact it -- a "
                "tracked file must not show what the rules say to hide.")
    return failures, notes


def main() -> int:
    if not EXPORTER_PATH.exists():
        print(f"{EXPORTER_PATH.relative_to(ROOT).as_posix()} is missing; the "
              "exporter this gate protects does not exist", file=sys.stderr)
        return 1

    exporter = load_exporter()

    failures = check_table(exporter) + check_mechanics(exporter)
    if failures:
        print("Open WebUI redaction is broken:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    notes: list[str] = []
    if EXPORT_PATH.exists():
        export_failures, notes = check_committed_export(exporter)
        if export_failures:
            print("Open WebUI export shows what the rules hide:", file=sys.stderr)
            for failure in export_failures:
                print(f"  {failure}", file=sys.stderr)
            return 1
    else:
        print("Open WebUI redaction: no export to cross-check (run "
              "`make owui-export`); the rules themselves were still checked.")

    for note in notes:
        print(f"  note: {note}")
    print(f"Open WebUI redaction: OK ({len(CASES)} keys checked against "
          f"is_safe(), mechanics verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
