#!/usr/bin/env python3
"""Export Open WebUI's live config into the repo so drift is visible in git.

WHY THIS EXISTS. ENABLE_PERSISTENT_CONFIG became true on 2026-08-10, so a key
with a database row ignores the environment permanently. Editing a value in
infra-apps.yml and running `make infra` then silently does nothing -- no error,
and changed=0 still reports success because the quadlet really is up to date.
That is a hole in this repo's central guarantee, and a backup does not close
it: a webui.db inside a tarball is not reviewable and not diffable.

This writes the live config into a tracked file, so a settings change made in
the UI turns up in `git diff` instead of nowhere. It is the recording half;
tests/validate_openwebui_config_drift.py is the half that fails the build.

SECRETS. The export carries API keys and the OAuth client secret. Values are
written verbatim ONLY for keys matching the allowlist below; everything else is
recorded by NAME with its value replaced by `<redacted>`. That direction is
deliberate -- a denylist of secret-looking names silently leaks whatever
upstream adds next, whereas an unknown key here is merely uninformative. Widen
SAFE_PREFIXES when a new setting is worth tracking, having looked at it.

Redaction hides value drift for redacted keys, which is a real limit: rotating
a token shows no diff. That is accepted, because this tool exists to track
settings, not secrets.

Run it as a make target, never from a play. A file that changes every run
inside roles/svc_infra would make every `make infra` report `changed` and
destroy the changed=0 proof -- the same rule the metric writes follow.

Exit codes:
    0  exported; the file was written (or was already identical)
    1  bad arguments, Open WebUI unreachable, or the request was refused --
       COULD NOT LOOK, the previous file is left in place
    2  the response was not a config object this tool understands
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT / "inventory" / "group_vars" / "all" / "openwebui-config.yml"

# Values are shown only for keys under these prefixes. Everything else is
# recorded by name with its value redacted.
SAFE_PREFIXES = (
    "ui.",
    "user.permissions",
    "image_generation.",
    "ollama.enable",
    "openai.enable",
    "rag.embedding_engine",
    "rag.embedding_model",
    "task.",
    "web.search.engine",
    "web.search.enable",
    "auth.",
)

# Belt and braces: never show a value whose key looks like a credential, even
# if it sits under a safe prefix.
SECRET_MARKERS = ("key", "secret", "token", "password", "credential")

REDACTED = "<redacted>"


def is_safe(key: str) -> bool:
    lowered = key.lower()
    if any(marker in lowered for marker in SECRET_MARKERS):
        return False
    return any(lowered.startswith(prefix) for prefix in SAFE_PREFIXES)


def flatten(value: object, prefix: str = "") -> dict[str, object]:
    """Flatten nested config into dotted keys so a diff points at one setting."""
    if isinstance(value, dict):
        flat: dict[str, object] = {}
        for key, inner in value.items():
            flat.update(flatten(inner, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: value}


def redact(flat: dict[str, object]) -> dict[str, object]:
    return {key: (value if is_safe(key) else REDACTED)
            for key, value in sorted(flat.items())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="https://chat.fortwow.dev")
    parser.add_argument("--token-file", metavar="FILE",
                        help="file holding the admin token; overrides OWUI_ADMIN_TOKEN")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--stdout", action="store_true",
                        help="print instead of writing the tracked file")
    args = parser.parse_args()

    token = (Path(args.token_file).read_text().strip() if args.token_file
             else os.environ.get("OWUI_ADMIN_TOKEN", "").strip())
    if not token:
        print("no admin token: set OWUI_ADMIN_TOKEN or pass --token-file",
              file=sys.stderr)
        return 1

    request = urllib.request.Request(
        f"{args.base_url.rstrip('/')}/api/v1/configs/export",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            live = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        print(f"export refused: HTTP {exc.code}. The previous file is left in "
              "place rather than being replaced with a guess.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"could not reach {args.base_url}: {exc}. The previous file is "
              "left in place.", file=sys.stderr)
        return 1

    if not isinstance(live, dict) or not live:
        print(f"export returned {type(live).__name__} rather than a populated "
              "config object; refusing to overwrite the file with it",
              file=sys.stderr)
        return 2

    flat = redact(flatten(live))
    shown = sum(1 for value in flat.values() if value != REDACTED)

    document = {
        "openwebui_live_config": flat,
    }
    header = (
        "---\n"
        "# GENERATED by scripts/owui_config_export.py -- do not hand-edit.\n"
        "#\n"
        "# A record of Open WebUI's LIVE config, which since\n"
        "# ENABLE_PERSISTENT_CONFIG became true is not the same thing as what\n"
        "# infra-apps.yml declares. Its purpose is to make UI drift show up in\n"
        "# git diff. Nothing consumes it at deploy time.\n"
        "#\n"
        f"# Values are shown for {shown} of {len(flat)} keys; the rest are\n"
        "# redacted by allowlist, so a name appearing here never leaks a value.\n"
        "# Redaction also hides value drift for those keys -- rotating a token\n"
        "# produces no diff. That is accepted; this tracks settings, not secrets.\n"
        "#\n"
        "# Refresh with `make owui-export`.\n"
    )
    rendered = header + yaml.safe_dump(document, sort_keys=True,
                                       default_flow_style=False, allow_unicode=True)

    if args.stdout:
        print(rendered, end="")
        return 0

    previous = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else None
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    state = "unchanged" if previous == rendered else "UPDATED"
    print(f"{OUTPUT_PATH.relative_to(ROOT).as_posix()}: {state} "
          f"({len(flat)} keys, {shown} with values)")
    if state == "UPDATED":
        print("Review the diff: anything that moved was changed in the UI, not "
              "by this repo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
