#!/usr/bin/env python3
"""Push the committed ComfyUI image config into Open WebUI's database.

WHY THIS IS NOT A LINE IN infra-apps.yml. ENABLE_PERSISTENT_CONFIG became true
on 2026-08-10, so Open WebUI reads the environment only for keys with no
database row and ignores it permanently for keys that have one. Setting
COMFYUI_WORKFLOW_NODES in the quadlet can therefore change nothing at all while
`make infra` reports `changed` — the failure openwebui-settings-as-code.md
exists to describe. This pushes through the admin API, which wins either way.

It is also the only practical way to deliver the workflow: quadlets render env
as `Environment="NAME=value"` on ONE line, and a workflow is multi-line JSON
full of double quotes.

Design: docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md

Exit codes:
    0  pushed and read back identical, or already identical (nothing to do)
    1  bad arguments, Open WebUI unreachable, the GET was rejected, or the
       POST was refused outright (401/403 or any other 4xx) -- COULD NOT
       LOOK, nothing was pushed
    2  the POST returned a 5xx (ambiguous -- it may have partially applied
       before erroring), or it returned 200 but the readback disagrees --
       rejected or silently rewritten
    3  catalog or workflow file invalid (validate_openwebui_image_config.py
       should have caught this first)
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
CATALOG_PATH = ROOT / "inventory" / "group_vars" / "all" / "images.yml"
WORKFLOW_DIR = ROOT / "inventory" / "comfyui-workflows"
EDIT_WORKFLOW_DIR = ROOT / "inventory" / "comfyui-edit-workflows"

# The GET response carries COMFYUI_API_KEY, IMAGES_OPENAI_API_KEY and both
# Gemini keys. This tool holds the whole config in memory by construction, so
# every diagnostic prints key NAMES and never values unless the key is here.
SHOWABLE = {
    "ENABLE_IMAGE_GENERATION", "IMAGE_GENERATION_ENGINE", "IMAGE_GENERATION_MODEL",
    "IMAGE_SIZE", "IMAGE_STEPS", "COMFYUI_BASE_URL", "COMFYUI_WORKFLOW_NODES",
    "ENABLE_IMAGE_EDIT", "IMAGE_EDIT_ENGINE", "IMAGE_EDIT_MODEL",
    "IMAGES_EDIT_COMFYUI_BASE_URL", "IMAGES_EDIT_COMFYUI_WORKFLOW_NODES",
}


def managed_keys(catalog: dict, workflow_json: str, edit_workflow_json: str) -> dict[str, object]:
    """The Open WebUI fields this tool owns. Everything else is passed through
    from the live config untouched."""
    return {
        "ENABLE_IMAGE_GENERATION": bool(catalog["image_generation_enabled"]),
        "IMAGE_GENERATION_ENGINE": "comfyui",
        "IMAGE_GENERATION_MODEL": catalog["image_generation_model"],
        "IMAGE_SIZE": catalog["image_size"],
        "IMAGE_STEPS": int(catalog["image_steps"]),
        "COMFYUI_BASE_URL": catalog["comfyui_base_url"],
        "COMFYUI_WORKFLOW": workflow_json,
        "COMFYUI_WORKFLOW_NODES": catalog["image_workflow_nodes"],
        "ENABLE_IMAGE_EDIT": bool(catalog["image_edit_enabled"]),
        "IMAGE_EDIT_ENGINE": "comfyui",
        "IMAGE_EDIT_MODEL": catalog["image_edit_model"],
        "IMAGES_EDIT_COMFYUI_BASE_URL": catalog["comfyui_base_url"],
        "IMAGES_EDIT_COMFYUI_WORKFLOW": edit_workflow_json,
        "IMAGES_EDIT_COMFYUI_WORKFLOW_NODES": catalog["image_edit_workflow_nodes"],
    }


def diff_keys(current: dict, desired: dict) -> list[str]:
    return sorted(k for k, v in desired.items() if current.get(k) != v)


def show(key: str, value: object) -> str:
    if key in SHOWABLE:
        rendered = json.dumps(value)
        return rendered if len(rendered) <= 200 else f"<{len(rendered)} chars>"
    return "<redacted>"


def api_get(base_url: str, path: str, token: str, timeout: int) -> object:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def api_post(base_url: str, path: str, token: str, payload: object,
             timeout: int) -> object:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://192.168.1.32:3007",
                        help="Open WebUI base URL")
    parser.add_argument("--token-file", metavar="FILE",
                        help="file holding the admin token; "
                             "default reads OWUI_ADMIN_TOKEN from the environment")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and exit without pushing")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    if args.token_file:
        token = Path(args.token_file).read_text().strip()
    else:
        token = os.environ.get("OWUI_ADMIN_TOKEN", "").strip()
    if not token:
        print("no admin token: set OWUI_ADMIN_TOKEN or pass --token-file. "
              "The Makefile deliberately does not read it out of vault.yml — "
              "a recipe that pipes a secret can echo it on failure.",
              file=sys.stderr)
        return 1

    try:
        catalog = yaml.safe_load(CATALOG_PATH.read_text())
        workflow_path = WORKFLOW_DIR / f"{catalog['image_workflow']}.json"
        workflow = json.loads(workflow_path.read_text())
        workflow_json = json.dumps(workflow)
        edit_workflow_path = (EDIT_WORKFLOW_DIR
                              / f"{catalog['image_edit_workflow']}.json")
        edit_workflow = json.loads(edit_workflow_path.read_text())
        edit_workflow_json = json.dumps(edit_workflow)
        desired = managed_keys(catalog, workflow_json, edit_workflow_json)
    except (OSError, KeyError, ValueError, yaml.YAMLError) as error:
        print(f"catalog or workflow is unusable: {error}", file=sys.stderr)
        return 3

    try:
        current = api_get(args.base_url, "/api/v1/images/config", token,
                          args.timeout)
    except urllib.error.HTTPError as error:
        print(f"GET /api/v1/images/config returned {error.code} — "
              "could not look, nothing was changed", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"could not reach Open WebUI at {args.base_url}: {error}",
              file=sys.stderr)
        return 1

    changing = diff_keys(current, desired)
    if not changing:
        print("Open WebUI image config already matches the catalog — no change")
        return 0

    for key in changing:
        print(f"  {key}: {show(key, current.get(key))} -> {show(key, desired[key])}")
    if args.dry_run:
        print(f"--dry-run: {len(changing)} key(s) would change")
        return 0

    # Read-modify-write is mandatory, not an optimisation: the POST body is the
    # entire ImagesConfig model with every field required, so constructing it
    # from scratch would blank every key this tool does not manage.
    payload = dict(current)
    payload.update(desired)

    try:
        api_post(args.base_url, "/api/v1/images/config/update", token, payload,
                 args.timeout)
    except urllib.error.HTTPError as error:
        if 400 <= error.code < 500:
            # A 4xx means the request was refused outright -- nothing was
            # pushed. That is COULD NOT LOOK, the same bucket as an
            # unreachable server, not "pushed but disagreed".
            print(f"POST /api/v1/images/config/update returned {error.code} "
                  "-- the request was refused, nothing was pushed",
                  file=sys.stderr)
            return 1
        print(f"POST /api/v1/images/config/update returned {error.code}",
              file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f"push failed: {error}", file=sys.stderr)
        return 1

    # The 200 is not the proof. update_config validates and normalises —
    # stripping trailing slashes from base URLs, enforcing ^\d+x\d+$ on
    # IMAGE_SIZE — so a readback disagreeing with what was sent is the signal
    # that something was rejected or silently rewritten.
    try:
        after = api_get(args.base_url, "/api/v1/images/config", token,
                        args.timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            ValueError) as error:
        print(f"pushed, but could not read back to confirm: {error}",
              file=sys.stderr)
        return 2

    disagreed = diff_keys(after, desired)
    if disagreed:
        for key in disagreed:
            print(f"FAIL: {key} was sent as {show(key, desired[key])} but reads "
                  f"back as {show(key, after.get(key))}", file=sys.stderr)
        return 2

    print(f"pushed {len(changing)} key(s) and confirmed by readback")
    return 0


if __name__ == "__main__":
    sys.exit(main())
