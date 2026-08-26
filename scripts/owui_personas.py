#!/usr/bin/env python3
"""Seed the committed personas into Open WebUI, creating only what is missing.

WHY THIS CREATES BUT NEVER UPDATES. The agreed model for this app is seeded
from git, modified in the UI, captured by the backup (2026-08-10). An updating
seeder would fight the second half of that: every `make owui-personas` would
revert whatever somebody tuned in the UI, and `thera` -- renamed there from
`Therapist` -- would be renamed back on every run. So a persona that already
exists is left completely alone, and this is idempotent by construction rather
than by comparison.

The cost is stated rather than hidden: editing the text in personas.yml does
NOT update a persona already in the database. To push an edit, delete it in the
UI and re-seed.

WHY THIS IS NOT AN ANSIBLE TASK. A task that POSTs every run reports `changed`
every run, which would destroy the changed=0 proof the whole repo leans on.
Keeping it a make target means `make infra` neither knows nor cares about it --
the same reasoning as scripts/owui_image_config.py and the metric writes.

Exit codes:
    0  every persona present, or the missing ones were created and read back
    1  bad arguments, Open WebUI unreachable, or the request was refused
       (401/403/4xx) -- COULD NOT LOOK, nothing was created
    2  a create returned success but the readback disagrees, or a 5xx left it
       ambiguous -- something may have half-applied
    3  the catalog is invalid (validate_personas.py should have caught this)
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
CATALOG_PATH = ROOT / "inventory" / "group_vars" / "all" / "personas.yml"

# Granting user:* read is what Open WebUI's own access_control=None means --
# see backend/open_webui/models/access_grants.py. Personas default to
# private-to-creator, which is why the first two were invisible to every other
# account until that default was understood.
PUBLIC_READ = {"principal_type": "user", "principal_id": "*", "permission": "read"}


class NotJSON(Exception):
    """Reached Open WebUI, but it answered with something that is not JSON.

    Worth its own type rather than being folded into the unreachable case.
    `/api/v1/models/` -- with the trailing slash -- returns HTTP 200 and the
    SPA's HTML shell, so a wrong path looks like a healthy server right up
    until the parse. Reporting that as "could not reach" sent the first live
    run looking at the network instead of at the URL.
    """


def api(base_url: str, path: str, token: str, timeout: int,
        payload: object | None = None) -> object:
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            head = body[:80].decode("utf-8", "replace").replace("\n", " ")
            raise NotJSON(
                f"{path} returned HTTP {response.status} but not JSON "
                f"(starts {head!r}). A path that serves the web UI answers 200 "
                "with HTML rather than 404."
            ) from exc


def model_list(payload: object) -> list | None:
    """The models listing, whichever envelope this version wraps it in.

    0.11.0 answers `/api/v1/models` with {"data": [...]}, but the same route
    has returned a bare list in other versions and `/api/v1/models/base` does
    so today. Returns None for anything else, so an envelope this does not
    recognise refuses loudly instead of reading as an empty estate -- an empty
    list here would mean "no personas exist" and create duplicates of both.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return None


def desired_form(persona: dict) -> dict:
    """The ModelForm Open WebUI expects (backend/open_webui/models/models.py)."""
    params = dict(persona.get("params") or {})
    params["system"] = persona["system"].strip()
    form = {
        "id": persona["id"],
        "name": persona["name"],
        "base_model_id": persona["base_model"],
        "meta": {"description": persona["description"], "capabilities": {}},
        "params": params,
        "is_active": True,
    }
    if persona.get("public"):
        form["access_grants"] = [
            {"resource_type": "model", "resource_id": persona["id"], **PUBLIC_READ}
        ]
    return form


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="https://chat.fortwow.dev",
                        help="Open WebUI base URL")
    parser.add_argument("--token-file", metavar="FILE",
                        help="file holding the admin token; overrides OWUI_ADMIN_TOKEN")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be created and change nothing")
    args = parser.parse_args()

    token = (Path(args.token_file).read_text().strip() if args.token_file
             else os.environ.get("OWUI_ADMIN_TOKEN", "").strip())
    if not token:
        print("no admin token: set OWUI_ADMIN_TOKEN or pass --token-file",
              file=sys.stderr)
        return 1

    try:
        doc = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
        personas = doc["openwebui_personas"]
        if not personas:
            raise ValueError("openwebui_personas is empty")
    except Exception as exc:
        print(f"catalog unusable: {exc}", file=sys.stderr)
        return 3

    try:
        live = api(args.base_url, "/api/v1/models", token, args.timeout)
    except urllib.error.HTTPError as exc:
        print(f"could not list models: HTTP {exc.code}. Nothing was created.",
              file=sys.stderr)
        return 1
    except NotJSON as exc:
        print(f"{exc} Nothing was created.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"could not reach {args.base_url}: {exc}. Nothing was created.",
              file=sys.stderr)
        return 1

    items = model_list(live)
    if items is None:
        print(f"unexpected response listing models: {type(live).__name__}. "
              "Refusing to create anything on an answer this tool does not "
              "understand.", file=sys.stderr)
        return 1

    existing = {m.get("id") for m in items if isinstance(m, dict)}
    missing = [p for p in personas if p["id"] not in existing]

    for persona in personas:
        state = "present" if persona["id"] in existing else "MISSING"
        print(f"  {persona['id']:<14} {state}")

    if not missing:
        print(f"\nAll {len(personas)} personas already present; nothing to do.")
        return 0

    if args.dry_run:
        print(f"\n--dry-run: would create {len(missing)}: "
              f"{', '.join(p['id'] for p in missing)}")
        return 0

    created: list[str] = []
    for persona in missing:
        try:
            api(args.base_url, "/api/v1/models/create", token, args.timeout,
                desired_form(persona))
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200]
            if exc.code >= 500:
                print(f"\n{persona['id']}: HTTP {exc.code} {detail!r}. This may "
                      "have partially applied -- check before re-running.",
                      file=sys.stderr)
                return 2
            print(f"\n{persona['id']}: refused with HTTP {exc.code} {detail!r}. "
                  f"Created so far: {created or 'none'}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"\n{persona['id']}: {exc}. Created so far: {created or 'none'}",
                  file=sys.stderr)
            return 2
        created.append(persona["id"])

    # Read back rather than trusting the 200. A create that succeeds and stores
    # something different is the failure this repo keeps re-learning.
    try:
        after = model_list(api(args.base_url, "/api/v1/models", token,
                               args.timeout)) or []
        after_ids = {m.get("id") for m in after if isinstance(m, dict)}
    except Exception as exc:
        print(f"\ncreated {created} but could not read back ({exc}) -- unverified",
              file=sys.stderr)
        return 2

    unverified = [p for p in created if p not in after_ids]
    if unverified:
        print(f"\ncreated {created} but {unverified} are absent on readback -- "
              "accepted and silently dropped", file=sys.stderr)
        return 2

    print(f"\nCreated and verified: {', '.join(created)}")
    print("Personas are seeds. This tool will never update them again; edit "
          "them in the UI, or delete and re-seed to push a change from git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
