# In-Chat Image Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make in-chat image generation at `chat.fortwow.dev` actually produce an image, and leave behind a check that proves it did.

**Architecture:** A catalog in `group_vars` that no play reads, holding a ComfyUI node mapping and a selector for committed API-format workflow files. An offline validator gates the config before it leaves the repo; a `make` target pushes it through Open WebUI's admin API (the environment no longer wins, see the spec); a runtime check generates a real image and asserts it came back at 1024×1024, proving our workflow ran rather than the compiled-in default.

**Tech Stack:** Python 3 (stdlib only — `urllib.request`, no `requests`), Ansible, systemd, ComfyUI, Open WebUI.

**Spec:** [docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md](../specs/2026-08-14-comfyui-image-generation-design.md)

## Global Constraints

- **Python: stdlib only.** No script in this repo imports `requests` even though it is pinned in `requirements.txt`. Use `urllib.request` / `urllib.error`, matching `scripts/release_check.py` and `scripts/abliteration_control.py`.
- **There is no pytest.** Validators are standalone scripts run as `$(PYTHON) tests/validate_x.py`, returning `0`/`1` from `def main() -> int:` with `sys.exit(main())` at the bottom. "Write a failing test" here means **adding a case to the validator's own self-check table**, not creating a test file.
- **Every gate carries its own self-check.** A `*_CASES` tuple plus a `*_self_check() -> list[str]` called unconditionally at the top of `main()`. Convention: `tests/validate_grafana_dashboards.py:112-135`.
- **Script preamble:** `#!/usr/bin/env python3`, a module docstring stating *why* the file exists and linking the relevant doc, then `from __future__ import annotations`.
- **Never echo vault secrets** to terminal, logs, or a commit. Secret-bearing Ansible tasks use `no_log: true`.
- **Never `git add -A`.** Stage explicit paths. The repo root holds working notes quoting live credentials.
- **Never commit `vault.yml`.** It is gitignored.
- **Workflow JSON must not live under `inventory/group_vars/all/`** — Ansible auto-loads every file there as variables.
- **Node IDs `3`,`4`,`5`,`6`,`7` are mapped and must exist in every committed workflow.** `sdxl.json` and `pony.json` share them deliberately.
- Open WebUI base URL: `http://192.168.1.32:3007`. ComfyUI: `http://192.168.1.40:8188`. GPU host IP lives in `inventory/group_vars/all/main.yml` as `gpu_host_ip: "192.168.1.40"`.

## Machine Requirements

This repo was recently handed between machines because one could not run Ansible. Each task below is tagged:

| Tag | Needs |
|---|---|
| **[repo]** | Any machine with the checkout and Python. No network. |
| **[http]** | Network access to `192.168.1.32:3007` and `192.168.1.40:8188`, plus an Open WebUI admin token. |
| **[ansible]** | A machine that can run `make infra` — POSIX Ansible controller with an SSH key authorised on svc-infra. |

TERRA satisfies **[repo]** and **[http]** but **not [ansible]**.

## Branch

Work continues on `docs/image-generation-design`, branched from `origin/main`.

**Note on a precedent the spec cites:** the spec says the catalog follows the `models.yml` pattern. That file exists only on the unmerged `docs/inference-capacity-roster` branch, so it is not visible here. The *pattern* is what matters — catalog data in `group_vars` read only by `scripts/` and `tests/`, never by a play — and this plan implements it from scratch without depending on that branch.

---

### Task 1: Catalog, SDXL workflow, and the validator's structural rules

**[repo]**

**Files:**
- Create: `inventory/group_vars/all/images.yml`
- Create: `inventory/comfyui-workflows/sdxl.json`
- Create: `tests/validate_openwebui_image_config.py`
- Modify: `Makefile` (`.PHONY` list at line ~59, `validate-catalog` target at line ~121)

**Interfaces:**
- Produces: `load_all(root: Path) -> tuple[dict, dict[str, dict], dict]` returning `(catalog, workflows_by_name, main_vars)`; `check_config(catalog, workflows, main_vars) -> list[str]` returning human-readable failure strings (empty list means pass). Tasks 2 and 3 extend `check_config`. Task 4's push tool imports nothing from here — it re-reads the YAML itself — but relies on the catalog key names fixed below.

- [ ] **Step 1: Create the catalog**

`inventory/group_vars/all/images.yml`:

```yaml
---
# ComfyUI image-generation catalog for Open WebUI on svc-infra.
#
# NO PLAY OR ROLE READS THIS FILE. It is consumed only by
# scripts/owui_image_config.py, scripts/image_generation_check.py and
# tests/validate_openwebui_image_config.py, so adding to it never changes what
# `make infra` deploys and never disturbs the changed=0 proof.
#
# This file is the authority for Open WebUI's image settings, NOT the env block
# in infra-apps.yml. ENABLE_PERSISTENT_CONFIG became true on 2026-08-10, so the
# environment is a first-boot seed that a database row overrides permanently.
# Apply changes here with `make owui-image-config`; `make infra` will not.
#
# See docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md.

# Which file in inventory/comfyui-workflows/ to submit. Every mapped node ID
# below must exist in EVERY workflow there, not just this one, so that changing
# this line can never be the step that discovers a broken mapping.
image_workflow: sdxl

# comfyui_base_url and image_generation_enabled DUPLICATE gpu_host_ip and
# gpu_host_online in main.yml. That is deliberate: this file is read by plain
# yaml.safe_load with no Jinja rendering, so it cannot reference them. The
# duplication is safe only because validate_openwebui_image_config.py fails the
# build when the two disagree — do not "fix" one without the other.
comfyui_base_url: "http://192.168.1.40:8188"
image_generation_enabled: true

# The checkpoint filename as ComfyUI lists it in
# /object_info/CheckpointLoaderSimple. This is where the checkpoint name comes
# from: _apply_workflow_nodes writes it into the mapped `model` node. With it
# empty, a flawless mapping still submits an empty checkpoint name.
image_generation_model: "sd_xl_base_1.0.safetensors"

# 1024x1024 because SDXL is trained at 1024 and degrades badly below it.
# The runtime check asserts the returned PNG is exactly this size — a 512x512
# result means the mapping never reached ComfyUI and the compiled-in default
# workflow ran, which is the original defect.
image_size: "1024x1024"
image_steps: 28

# Serialised to JSON as COMFYUI_WORKFLOW_NODES by the push tool. Declared as
# YAML so diffs are readable.
#
# `key` is explicit on every entry. ComfyUINodeInput.key defaults to 'text',
# so an omitted key on a model/seed/image node writes inputs['text'] — a key
# CheckpointLoaderSimple does not have and ComfyUI quietly ignores, running the
# workflow's hardcoded checkpoint instead of erroring.
image_workflow_nodes:
  - type: model
    key: ckpt_name
    node_ids: ["4"]
  - type: prompt
    key: text
    node_ids: ["6"]
  - type: negative_prompt
    key: text
    node_ids: ["7"]
  - type: width
    key: width
    node_ids: ["5"]
  - type: height
    key: height
    node_ids: ["5"]
  - type: steps
    key: steps
    node_ids: ["3"]
  - type: seed
    key: seed
    node_ids: ["3"]
```

- [ ] **Step 2: Create the SDXL workflow**

`inventory/comfyui-workflows/sdxl.json` — ComfyUI **API format** (a flat map of node-id → `{class_type, inputs}`). The editor format, which has top-level `nodes` and `links` arrays, is not readable by Open WebUI.

```json
{
  "3": {
    "class_type": "KSampler",
    "inputs": {
      "seed": 0,
      "steps": 28,
      "cfg": 7.0,
      "sampler_name": "dpmpp_2m",
      "scheduler": "karras",
      "denoise": 1.0,
      "model": ["4", 0],
      "positive": ["6", 0],
      "negative": ["7", 0],
      "latent_image": ["5", 0]
    }
  },
  "4": {
    "class_type": "CheckpointLoaderSimple",
    "inputs": { "ckpt_name": "sd_xl_base_1.0.safetensors" }
  },
  "5": {
    "class_type": "EmptyLatentImage",
    "inputs": { "width": 1024, "height": 1024, "batch_size": 1 }
  },
  "6": {
    "class_type": "CLIPTextEncode",
    "inputs": { "text": "placeholder, overwritten per request", "clip": ["4", 1] }
  },
  "7": {
    "class_type": "CLIPTextEncode",
    "inputs": { "text": "", "clip": ["4", 1] }
  },
  "8": {
    "class_type": "VAEDecode",
    "inputs": { "samples": ["3", 0], "vae": ["4", 2] }
  },
  "9": {
    "class_type": "SaveImage",
    "inputs": { "filename_prefix": "homelab-owui", "images": ["8", 0] }
  }
}
```

`filename_prefix` is `homelab-owui` so the nightly check's output is identifiable and prunable on the GPU host — otherwise ComfyUI's output directory grows by one image a day forever with no way to tell which are ours.

- [ ] **Step 3: Write the failing self-check cases**

Create `tests/validate_openwebui_image_config.py` with the case table and a **deliberately incomplete** `check_config` that returns `[]`. The self-check must fail.

```python
#!/usr/bin/env python3
"""Assert the ComfyUI image config is well-formed before it reaches Open WebUI.

Every failure mode in this feature is silent. Open WebUI's
_apply_workflow_nodes writes workflow[node_id]["inputs"][key] = value, and
comfyui_create_image swallows the resulting KeyError in a broad `except
Exception` and returns None: no image, no error message, green container. An
unrecognised node `type` is skipped without comment, which is indistinguishable
from a bad node ID. So a typo here does not fail loudly anywhere downstream,
and this gate is the only thing between a typo and a silently dead feature.

Design and the upstream source reading behind each rule:
docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "inventory" / "group_vars" / "all" / "images.yml"
MAIN_VARS_PATH = ROOT / "inventory" / "group_vars" / "all" / "main.yml"
WORKFLOW_DIR = ROOT / "inventory" / "comfyui-workflows"

# Node types Open WebUI's _apply_workflow_nodes actually branches on. A type
# outside this set falls through every branch and is skipped in silence.
HANDLED_TYPES = {
    "model", "prompt", "negative_prompt", "image",
    "width", "height", "n", "steps", "seed",
}

# Types whose branch reads node.key with no per-type default. key defaults to
# 'text' on the model, so omitting it writes inputs['text'] rather than raising.
NEEDS_EXPLICIT_KEY = {"model", "seed", "image"}

# Mapping entries a generation config cannot work without.
REQUIRED_TYPES = {"model", "prompt", "width", "height", "steps", "seed"}

# ComfyUI collects output images only from these two class_types
# (_ws_get_images). A workflow ending in neither returns an empty image list:
# generation succeeds, no image, no error.
OUTPUT_CLASSES = {"SaveImage", "PreviewImage"}


def good_catalog() -> dict:
    """A minimal catalog that must pass every rule."""
    return {
        "image_workflow": "sdxl",
        "comfyui_base_url": "http://192.168.1.40:8188",
        "image_generation_enabled": True,
        "image_generation_model": "sd_xl_base_1.0.safetensors",
        "image_size": "1024x1024",
        "image_steps": 28,
        "image_workflow_nodes": [
            {"type": "model", "key": "ckpt_name", "node_ids": ["4"]},
            {"type": "prompt", "key": "text", "node_ids": ["6"]},
            {"type": "negative_prompt", "key": "text", "node_ids": ["7"]},
            {"type": "width", "key": "width", "node_ids": ["5"]},
            {"type": "height", "key": "height", "node_ids": ["5"]},
            {"type": "steps", "key": "steps", "node_ids": ["3"]},
            {"type": "seed", "key": "seed", "node_ids": ["3"]},
        ],
    }


def good_workflow() -> dict:
    return {
        "3": {"class_type": "KSampler",
              "inputs": {"seed": 0, "steps": 28, "cfg": 7.0}},
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "homelab-owui"}},
    }


def good_main_vars() -> dict:
    return {"gpu_host_ip": "192.168.1.40", "gpu_host_online": True}


def _drop_output_node(catalog, workflows, main_vars):
    del workflows["sdxl"]["9"]


def _editor_format(catalog, workflows, main_vars):
    workflows["sdxl"] = {"nodes": [{"id": 3}], "links": []}


def _missing_workflow_file(catalog, workflows, main_vars):
    catalog["image_workflow"] = "nonexistent"


def _drift_base_url(catalog, workflows, main_vars):
    catalog["comfyui_base_url"] = "http://192.168.1.99:8188"


def _drift_enabled(catalog, workflows, main_vars):
    main_vars["gpu_host_online"] = False


# Each case is (name, mutation, substring that must appear in a failure).
# A mutation takes (catalog, workflows, main_vars) and breaks exactly one rule.
VALIDATION_CASES = (
    ("workflow with no SaveImage/PreviewImage", _drop_output_node, "SaveImage"),
    ("editor-format workflow", _editor_format, "editor format"),
    ("image_workflow names a missing file", _missing_workflow_file, "nonexistent"),
    ("comfyui_base_url disagrees with gpu_host_ip", _drift_base_url, "gpu_host_ip"),
    ("image_generation_enabled disagrees with gpu_host_online", _drift_enabled,
     "gpu_host_online"),
)


def validation_self_check() -> list[str]:
    """Prove each rule still fires. A gate against silent failure is not
    allowed to fail silently itself."""
    problems: list[str] = []

    baseline = check_config(good_catalog(), {"sdxl": good_workflow()}, good_main_vars())
    if baseline:
        problems.append(
            f"the known-good configuration failed: {baseline} — every case below "
            "is measured against it, so the whole gate is untrustworthy"
        )

    for name, mutate, expected in VALIDATION_CASES:
        catalog = good_catalog()
        workflows = {"sdxl": good_workflow()}
        main_vars = good_main_vars()
        mutate(catalog, workflows, main_vars)
        failures = check_config(catalog, workflows, main_vars)
        if not any(expected in f for f in failures):
            problems.append(
                f"case {name!r} did not produce a failure mentioning {expected!r} "
                f"(got {failures}) — this rule is not enforced, so a config "
                "breaking it would deploy and produce no image and no error"
            )
    return problems


def check_config(catalog: dict, workflows: dict[str, dict],
                 main_vars: dict) -> list[str]:
    """Return human-readable failures. Empty means the config is sound."""
    return []


def load_all(root: Path) -> tuple[dict, dict[str, dict], dict]:
    catalog = yaml.safe_load((root / "inventory" / "group_vars" / "all"
                              / "images.yml").read_text())
    main_vars = yaml.safe_load((root / "inventory" / "group_vars" / "all"
                                / "main.yml").read_text())
    workflows = {}
    for path in sorted((root / "inventory" / "comfyui-workflows").glob("*.json")):
        workflows[path.stem] = json.loads(path.read_text())
    return catalog, workflows, main_vars


def main() -> int:
    failures: list[str] = validation_self_check()

    catalog, workflows, main_vars = load_all(ROOT)
    if not workflows:
        print(f"no workflows found in {WORKFLOW_DIR}", file=sys.stderr)
        return 1
    failures.extend(check_config(catalog, workflows, main_vars))

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"Open WebUI image config: OK ({len(workflows)} workflow(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run it and confirm it fails**

```bash
python tests/validate_openwebui_image_config.py
```

Expected: FAIL. Five lines, one per case, each saying the rule "is not enforced". `check_config` returns `[]`, so no case fires.

- [ ] **Step 5: Implement the structural rules**

Replace the stub `check_config` with:

```python
def check_config(catalog: dict, workflows: dict[str, dict],
                 main_vars: dict) -> list[str]:
    """Return human-readable failures. Empty means the config is sound."""
    failures: list[str] = []

    selected = catalog.get("image_workflow")
    if selected not in workflows:
        failures.append(
            f"image_workflow is {selected!r} but no such file exists in "
            f"inventory/comfyui-workflows/ (have: {sorted(workflows)})"
        )

    # These duplicate main.yml because this file is read without Jinja
    # rendering. The duplication is only safe while this check exists.
    expected_url = f"http://{main_vars.get('gpu_host_ip')}:8188"
    if catalog.get("comfyui_base_url") != expected_url:
        failures.append(
            f"comfyui_base_url is {catalog.get('comfyui_base_url')!r} but "
            f"gpu_host_ip in main.yml implies {expected_url!r} — the two have "
            "drifted and the push would point at the wrong host"
        )
    if bool(catalog.get("image_generation_enabled")) != bool(
            main_vars.get("gpu_host_online")):
        failures.append(
            "image_generation_enabled disagrees with gpu_host_online in "
            "main.yml — since the push retired the environment gate, this "
            "catalog is the only thing that still turns the feature off"
        )

    for name, workflow in sorted(workflows.items()):
        if not isinstance(workflow, dict):
            failures.append(f"workflow {name!r} is not a JSON object")
            continue
        if isinstance(workflow.get("nodes"), list):
            failures.append(
                f"workflow {name!r} looks like ComfyUI's editor format (it has "
                "a top-level `nodes` array). Open WebUI can only read the API "
                "format — re-export with Workflow -> Export (API)"
            )
            continue
        bad = [nid for nid, node in workflow.items()
               if not isinstance(node, dict) or "class_type" not in node
               or "inputs" not in node]
        if bad:
            failures.append(
                f"workflow {name!r} nodes {sorted(bad)} lack class_type/inputs "
                "— not valid API format"
            )
            continue
        classes = {node["class_type"] for node in workflow.values()}
        if not (classes & OUTPUT_CLASSES):
            failures.append(
                f"workflow {name!r} contains no SaveImage or PreviewImage node. "
                "_ws_get_images collects outputs only from those, so generation "
                "would succeed and return an empty image list with no error"
            )

    return failures
```

- [ ] **Step 6: Run it and confirm it passes**

```bash
python tests/validate_openwebui_image_config.py
```

Expected: PASS — `Open WebUI image config: OK (1 workflow(s))`.

- [ ] **Step 7: Wire it into validate-catalog**

In `Makefile`, add to the `validate-catalog` target after `validate_release_overrides.py`:

```make
	$(PYTHON) tests/validate_openwebui_image_config.py
```

`.PHONY` needs no change here — `validate-catalog` is already listed, and the new line is a command inside it rather than a target of its own.

- [ ] **Step 8: Confirm the gate runs in the suite**

```bash
make validate-catalog
```

Expected: PASS, with `Open WebUI image config: OK` in the output.

- [ ] **Step 9: Commit**

```bash
git add inventory/group_vars/all/images.yml inventory/comfyui-workflows/sdxl.json tests/validate_openwebui_image_config.py Makefile
git commit -m "feat: add the ComfyUI image catalog and its structural gate"
```

---

### Task 2: Validator mapping rules

**[repo]**

**Files:**
- Modify: `tests/validate_openwebui_image_config.py`

**Interfaces:**
- Consumes: `check_config(catalog, workflows, main_vars) -> list[str]`, `HANDLED_TYPES`, `NEEDS_EXPLICIT_KEY`, `REQUIRED_TYPES` from Task 1.
- Produces: the same `check_config`, extended. Task 3 extends it again.

- [ ] **Step 1: Add the failing self-check cases**

Add these mutations above `VALIDATION_CASES`:

```python
def _node_id_absent_from_one_workflow(catalog, workflows, main_vars):
    # A second workflow that omits node 5. The mapping is shared across all
    # committed workflows, so this must fail even though `sdxl` is selected.
    other = good_workflow()
    del other["5"]
    workflows["other"] = other


def _unknown_node_type(catalog, workflows, main_vars):
    catalog["image_workflow_nodes"][0]["type"] = "checkpoint"


def _model_node_without_key(catalog, workflows, main_vars):
    del catalog["image_workflow_nodes"][0]["key"]


def _image_type_in_generation_mapping(catalog, workflows, main_vars):
    catalog["image_workflow_nodes"].append(
        {"type": "image", "key": "image", "node_ids": ["6"]})


def _missing_required_type(catalog, workflows, main_vars):
    catalog["image_workflow_nodes"] = [
        n for n in catalog["image_workflow_nodes"] if n["type"] != "seed"]
```

Extend `VALIDATION_CASES` with:

```python
    ("mapped node absent from a non-selected workflow",
     _node_id_absent_from_one_workflow, "other"),
    ("unrecognised node type", _unknown_node_type, "checkpoint"),
    ("model node without an explicit key", _model_node_without_key, "explicit key"),
    ("image-type node in a generation mapping",
     _image_type_in_generation_mapping, "AttributeError"),
    ("required mapping type missing", _missing_required_type, "seed"),
```

- [ ] **Step 2: Run it and confirm the new cases fail**

```bash
python tests/validate_openwebui_image_config.py
```

Expected: FAIL with five lines naming the new cases. The Task 1 cases still pass.

- [ ] **Step 3: Implement the mapping rules**

Insert before `return failures` in `check_config`:

```python
    nodes = catalog.get("image_workflow_nodes") or []
    if not isinstance(nodes, list):
        failures.append("image_workflow_nodes must be a list")
        return failures

    seen_types: set[str] = set()
    for index, node in enumerate(nodes):
        node_type = node.get("type")
        where = f"image_workflow_nodes[{index}] (type={node_type!r})"
        seen_types.add(node_type)

        if node_type is not None and node_type not in HANDLED_TYPES:
            failures.append(
                f"{where} is not a type Open WebUI handles. "
                "_apply_workflow_nodes falls through every branch and skips it "
                "in silence, which is indistinguishable from a bad node ID"
            )
        if node_type == "image":
            failures.append(
                f"{where} belongs only to an EDIT mapping. "
                "ComfyUICreateImageForm has no `image` field, so "
                "_apply_workflow_nodes raises AttributeError on payload.image, "
                "which comfyui_create_image swallows into None — no image, no error"
            )
        if node_type in NEEDS_EXPLICIT_KEY and "key" not in node:
            failures.append(
                f"{where} needs an explicit key. ComfyUINodeInput.key defaults "
                "to 'text', so omitting it writes inputs['text'] — a key the "
                "target class does not have, which ComfyUI ignores without error"
            )

        for node_id in node.get("node_ids") or []:
            for name, workflow in sorted(workflows.items()):
                if isinstance(workflow.get("nodes"), list):
                    continue  # already reported as editor format
                if node_id not in workflow:
                    failures.append(
                        f"{where} maps node id {node_id!r}, absent from workflow "
                        f"{name!r}. The mapping is shared across every committed "
                        "workflow so that switching image_workflow can never be "
                        "the step that discovers a broken mapping"
                    )

    for required in sorted(REQUIRED_TYPES - seen_types):
        failures.append(
            f"image_workflow_nodes has no {required!r} entry — that value would "
            "never reach ComfyUI and the workflow's hardcoded one would be used"
        )
```

- [ ] **Step 4: Run it and confirm it passes**

```bash
python tests/validate_openwebui_image_config.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/validate_openwebui_image_config.py
git commit -m "feat: gate the node mapping against every committed workflow"
```

---

### Task 3: Class/key agreement and mirrored server-side rules

**[repo]**

This is the rule that replaces a warning with a check. The spec's source page warns that a shared mapping has two latent mismatches — `model` writes `ckpt_name`, correct for `CheckpointLoaderSimple` and wrong for `UNETLoader` (`unet_name`); `seed` writes `seed`, correct for `KSampler` and wrong for `RandomNoise` (`noise_seed`) — and advises remembering to fix it. Both node IDs exist, so nothing raises and Task 2's rules pass. The workflow records each node's `class_type`, so this is checkable.

**Files:**
- Modify: `tests/validate_openwebui_image_config.py`

**Interfaces:**
- Consumes: `check_config` from Task 2.
- Produces: `CLASS_INPUTS: dict[str, set[str]]` — the static table of node class → accepted input names. Task 9 extends it with `ConditioningConcat`.

- [ ] **Step 1: Add the failing self-check cases**

Add the table below `OUTPUT_CLASSES`:

```python
# Inputs each node class accepts, so a mapped key can be checked offline.
# ComfyUI's /object_info is the live source of truth, but this gate runs with
# no network. An unknown class_type is a FAILURE rather than a skip: a silent
# skip here is exactly the class of hole this file exists to close. Adding a
# node type to a workflow means adding it here in the same commit.
CLASS_INPUTS = {
    "KSampler": {"seed", "steps", "cfg", "sampler_name", "scheduler",
                 "denoise", "model", "positive", "negative", "latent_image"},
    "CheckpointLoaderSimple": {"ckpt_name"},
    "EmptyLatentImage": {"width", "height", "batch_size"},
    "CLIPTextEncode": {"text", "clip"},
    "VAEDecode": {"samples", "vae"},
    "SaveImage": {"filename_prefix", "images"},
    "PreviewImage": {"images"},
}
```

Add the mutations:

```python
def _key_not_accepted_by_class(catalog, workflows, main_vars):
    # unet_name is right for UNETLoader and wrong for CheckpointLoaderSimple.
    # Both node IDs exist, so every ID-based rule passes and the value is
    # simply ignored at generation time.
    catalog["image_workflow_nodes"][0]["key"] = "unet_name"


def _unknown_class_type(catalog, workflows, main_vars):
    workflows["sdxl"]["4"]["class_type"] = "SomeCustomLoader"


def _bad_image_size(catalog, workflows, main_vars):
    catalog["image_size"] = "1024"


def _negative_steps(catalog, workflows, main_vars):
    catalog["image_steps"] = -1


def _empty_model(catalog, workflows, main_vars):
    catalog["image_generation_model"] = ""
```

Extend `VALIDATION_CASES`:

```python
    ("mapped key the node class does not accept", _key_not_accepted_by_class,
     "unet_name"),
    ("workflow node of an unknown class", _unknown_class_type, "SomeCustomLoader"),
    ("image_size not WxH", _bad_image_size, "image_size"),
    ("negative image_steps", _negative_steps, "image_steps"),
    ("empty image_generation_model", _empty_model, "image_generation_model"),
```

- [ ] **Step 2: Run it and confirm the new cases fail**

```bash
python tests/validate_openwebui_image_config.py
```

Expected: FAIL with five new lines.

- [ ] **Step 3: Implement the rules**

Add `import re` to the imports. Inside the `for node_id in node.get("node_ids") or []:` loop, after the `if node_id not in workflow:` block, add:

```python
                else:
                    class_type = workflow[node_id]["class_type"]
                    accepted = CLASS_INPUTS.get(class_type)
                    key = node.get("key", "text")
                    if accepted is None:
                        failures.append(
                            f"workflow {name!r} node {node_id!r} has class_type "
                            f"{class_type!r}, which is not in CLASS_INPUTS. Add it "
                            "there in this commit — an unchecked class means a "
                            "mapped key can silently target an input that does "
                            "not exist"
                        )
                    elif key not in accepted:
                        failures.append(
                            f"{where} writes key {key!r} into workflow {name!r} "
                            f"node {node_id!r} ({class_type}), which accepts "
                            f"{sorted(accepted)}. Nothing raises — the value is "
                            "simply ignored and the workflow's hardcoded one is used"
                        )
```

Then before `return failures`, add the mirrored server-side rules:

```python
    # Mirrors update_config's own validation so a bad value fails at
    # `make validate` rather than at push time.
    size = catalog.get("image_size")
    if not isinstance(size, str) or not re.fullmatch(r"\d+x\d+", size):
        failures.append(
            f"image_size is {size!r}; Open WebUI's update_config requires "
            r"^\d+x\d+$ and would reject the push"
        )
    steps = catalog.get("image_steps")
    if not isinstance(steps, int) or steps < 0:
        failures.append(
            f"image_steps is {steps!r}; update_config rejects anything negative"
        )
    if not (catalog.get("image_generation_model") or "").strip():
        failures.append(
            "image_generation_model is empty. _apply_workflow_nodes writes this "
            "value into the mapped model node, so an empty one submits an empty "
            "checkpoint name and ComfyUI rejects the prompt at validation"
        )
```

- [ ] **Step 4: Run it and confirm it passes**

```bash
python tests/validate_openwebui_image_config.py && make validate-catalog
```

Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/validate_openwebui_image_config.py
git commit -m "fix: check the mapped key against the node class, not just the id"
```

---

### Task 4: The push tool

**[repo]** to write, **[http]** to exercise.

**Files:**
- Create: `scripts/owui_image_config.py`
- Modify: `Makefile` (`.PHONY` list, new `owui-image-config` target)

**Interfaces:**
- Consumes: the catalog keys fixed in Task 1.
- Produces: `managed_keys(catalog: dict, workflow_json: str) -> dict[str, object]` — the Open WebUI field names this tool owns; `diff_keys(current: dict, desired: dict) -> list[str]` — names of keys that differ; `show(key: str, value: object) -> str` — a display string, redacted unless the key is in `SHOWABLE: set[str]`; `api_get(base_url, path, token, timeout)` and `api_post(base_url, path, token, payload, timeout)`, both imported by Task 6.

- [ ] **Step 1: Write the tool**

```python
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
    1  bad arguments, Open WebUI unreachable, or auth rejected -- COULD NOT LOOK
    2  pushed, but the readback disagrees -- rejected or silently rewritten
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

# The GET response carries COMFYUI_API_KEY, IMAGES_OPENAI_API_KEY and both
# Gemini keys. This tool holds the whole config in memory by construction, so
# every diagnostic prints key NAMES and never values unless the key is here.
SHOWABLE = {
    "ENABLE_IMAGE_GENERATION", "IMAGE_GENERATION_ENGINE", "IMAGE_GENERATION_MODEL",
    "IMAGE_SIZE", "IMAGE_STEPS", "COMFYUI_BASE_URL", "COMFYUI_WORKFLOW_NODES",
}


def managed_keys(catalog: dict, workflow_json: str) -> dict[str, object]:
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
        desired = managed_keys(catalog, workflow_json)
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
```

- [ ] **Step 2: Add the Makefile target**

Add `owui-image-config` to the `.PHONY` list, then:

```make
owui-image-config: ## Push inventory/group_vars/all/images.yml into Open WebUI (needs OWUI_ADMIN_TOKEN)
	$(PYTHON) scripts/owui_image_config.py $(ARGS)
```

- [ ] **Step 3: Confirm it fails cleanly with no token**

```bash
env -u OWUI_ADMIN_TOKEN python scripts/owui_image_config.py; echo "exit=$?"
```

Expected: `exit=1` and the "no admin token" message. This is the *could not look* path — it must never be confused with success.

- [ ] **Step 4: Confirm it reports a bad catalog as exit 3**

```bash
python - <<'EOF'
import pathlib, shutil
p = pathlib.Path("inventory/group_vars/all/images.yml")
shutil.copy(p, "/tmp/images.yml.bak")
p.write_text("image_workflow: nope\n")
EOF
OWUI_ADMIN_TOKEN=dummy python scripts/owui_image_config.py; echo "exit=$?"
cp /tmp/images.yml.bak inventory/group_vars/all/images.yml
```

Expected: `exit=3`, naming the missing workflow file. Confirm the catalog is restored with `git diff --exit-code inventory/group_vars/all/images.yml`.

- [ ] **Step 5: Validate and commit**

```bash
make validate-catalog
git add scripts/owui_image_config.py Makefile
git commit -m "feat: push the image config through the admin API, not the env"
```

---

### Task 5: Measure the starting state, then push

**[http]** — needs an Open WebUI admin token and network access. No Ansible.

This is the spec's Step 0. The answer does not change the design, but it determines whether the environment path was ever viable, and that belongs in the record rather than in an assumption.

**Files:**
- Modify: `docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md` (record the measured answer)

- [ ] **Step 1: Obtain an admin token**

In Open WebUI: *Settings → Account → API Keys → Create*. The account must be an admin. Export it, and record it in the vault for the next operator:

```bash
export OWUI_ADMIN_TOKEN='<the key>'
```

```bash
make vault-edit
```

Add `vault_openwebui_admin_token: <the key>`. **Do not** echo the variable, paste it into a commit, or pipe it through a `make` recipe.

- [ ] **Step 2: Measure whether database rows already exist**

```bash
curl -sS -H "Authorization: Bearer $OWUI_ADMIN_TOKEN" \
  http://192.168.1.32:3007/api/v1/images/config \
  | python -c "import json,sys; d=json.load(sys.stdin); print({k: d.get(k) for k in ('IMAGE_SIZE','IMAGE_STEPS','COMFYUI_WORKFLOW_NODES','IMAGE_GENERATION_MODEL')})"
```

Record what comes back. `COMFYUI_WORKFLOW_NODES` of `[]` with `IMAGE_SIZE` of `1024x1024` means the environment seeded and the mapping is genuinely unset. Any value that does *not* match `infra-apps.yml` means a database row is overriding the environment.

This command prints only four non-secret keys deliberately — dumping the whole config would put four API keys on the terminal.

- [ ] **Step 3: Dry-run the push**

```bash
make owui-image-config ARGS=--dry-run
```

Expected: a list of changing keys with `COMFYUI_WORKFLOW` and `COMFYUI_WORKFLOW_NODES` among them, exit 0.

- [ ] **Step 4: Push**

```bash
make owui-image-config
```

Expected: `pushed N key(s) and confirmed by readback`, exit 0.

- [ ] **Step 5: Confirm idempotence**

```bash
make owui-image-config; echo "exit=$?"
```

Expected: `Open WebUI image config already matches the catalog — no change`, `exit=0`. This is the tool's analogue of `changed=0`.

- [ ] **Step 6: Confirm the mapping resolves live**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $OWUI_ADMIN_TOKEN" \
  http://192.168.1.32:3007/api/v1/images/models
```

Expected: `200`. A `400` means the mapping names a node absent from the workflow — Open WebUI evaluates `workflow[model_node_id]["class_type"]` here, so this endpoint is a free mapping check.

- [ ] **Step 7: Record the measured answer in the spec**

Under *Rollout → Step 0*, replace the instruction with what was actually found, dated. If no rows existed, say so plainly — the spec's risk section already predicts that possibility and it should be closed out rather than left open.

- [ ] **Step 8: Commit**

```bash
git add docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md
git commit -m "docs: record whether image config rows existed before the push"
```

---

### Task 6: The runtime check

**[repo]** to write, **[http]** to exercise.

**Files:**
- Create: `scripts/image_generation_check.py`
- Modify: `Makefile` (`.PHONY` list, new `image-gen-check` target)

**Interfaces:**
- Consumes: `api_get`, `api_post` from `scripts/owui_image_config.py`.
- Produces: `probe(base_url, token, catalog, timeout) -> tuple[str, dict[str, float]]` returning a verdict in `{"ok", "broken", "inconclusive"}` and the metric values Task 8 publishes.

- [ ] **Step 1: Write the check**

```python
#!/usr/bin/env python3
"""Generate one image end to end and prove OUR workflow produced it.

A green container, an active unit and a 200 together prove a process started.
This feature spent days looking exactly like that while never once reaching
ComfyUI. So this check generates a real image, fetches the bytes, and asserts
they are a PNG of the expected size.

The size assertion is the load-bearing one. Open WebUI has a workflow compiled
in whose EmptyLatentImage is 512x512; that is what was being submitted during
the defect. A 1024x1024 result therefore proves the width/height mapping
reached ComfyUI -- not merely that an image appeared.

Verdicts are tri-state. `inconclusive` means could-not-look (GPU host asleep,
Open WebUI unreachable, auth rejected, timeout) and escalates rather than
passing, because "could not look" reading as "fine" is how every other check
in this repo has failed at least once.

Design: docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from owui_image_config import api_get, api_post  # noqa: E402

CATALOG_PATH = ROOT / "inventory" / "group_vars" / "all" / "images.yml"
PROMPT = "a plain grey ceramic mug on a wooden table, soft daylight"


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG IHDR. Raises ValueError if not a PNG."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def probe(base_url: str, token: str, catalog: dict,
          timeout: int) -> tuple[str, dict[str, float]]:
    expected_model = catalog["image_generation_model"]
    expected_w, expected_h = (int(n) for n in catalog["image_size"].split("x"))
    started = time.monotonic()

    # Cheapest first: this evaluates workflow[model_node_id]["class_type"]
    # internally, so a mapping naming an absent node 400s here rather than
    # failing silently later. It also returns ComfyUI's live checkpoint list.
    try:
        models = api_get(base_url, "/api/v1/images/models", token, timeout)
    except urllib.error.HTTPError as error:
        if error.code == 400:
            return "broken", {}
        return "inconclusive", {}
    except (urllib.error.URLError, OSError, ValueError):
        return "inconclusive", {}

    available = {m.get("id") for m in models} if isinstance(models, list) else set()
    if expected_model not in available:
        print(f"FAIL: {expected_model!r} is not among the checkpoints ComfyUI "
              f"offers ({sorted(available)})", file=sys.stderr)
        return "broken", {}

    try:
        result = api_post(base_url, "/api/v1/images/generations", token,
                          {"prompt": PROMPT, "n": 1}, timeout)
    except urllib.error.HTTPError:
        return "broken", {}
    except (urllib.error.URLError, OSError, ValueError):
        return "inconclusive", {}

    if not isinstance(result, list) or not result:
        print("FAIL: generation returned no image. comfyui_create_image "
              "swallows every exception into None, so this is what a broken "
              "mapping looks like from outside", file=sys.stderr)
        return "broken", {}

    url = result[0].get("url", "")
    if url.startswith("/"):
        url = f"{base_url.rstrip('/')}{url}"
    try:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return "inconclusive", {}

    try:
        width, height = png_size(data)
    except ValueError:
        print(f"FAIL: returned {len(data)} bytes that are not a PNG",
              file=sys.stderr)
        return "broken", {}

    duration = time.monotonic() - started
    metrics = {
        "homelab_image_generation_duration_seconds": round(duration, 2),
        "homelab_image_generation_width": float(width),
    }

    if (width, height) != (expected_w, expected_h):
        print(f"FAIL: image is {width}x{height}, expected "
              f"{expected_w}x{expected_h}. Open WebUI's compiled-in default "
              "workflow is 512x512 — this size means the width/height mapping "
              "never reached ComfyUI and the default workflow ran",
              file=sys.stderr)
        metrics["homelab_image_generation_ok"] = 0.0
        return "broken", metrics

    metrics["homelab_image_generation_ok"] = 1.0
    print(f"image generation OK: {width}x{height} PNG, {len(data)} bytes, "
          f"{duration:.1f}s")
    return "ok", metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://192.168.1.32:3007")
    parser.add_argument("--token-file", metavar="FILE")
    parser.add_argument("--metrics-dir", metavar="DIR",
                        help="publish metrics via homelab-metric-write. Omit "
                             "for a local run: an operator diagnostic must not "
                             "write to the nightly series")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    if args.token_file:
        token = Path(args.token_file).read_text().strip()
    else:
        token = os.environ.get("OWUI_ADMIN_TOKEN", "").strip()
    if not token:
        print("no admin token: set OWUI_ADMIN_TOKEN or pass --token-file",
              file=sys.stderr)
        return 1

    catalog = yaml.safe_load(CATALOG_PATH.read_text())
    if not catalog.get("image_generation_enabled"):
        print("image_generation_enabled is false in images.yml — not probing")
        return 0

    verdict, metrics = probe(args.base_url, token, catalog, args.timeout)

    # Emit BEFORE asserting, so the chart does not go blank exactly when
    # something is wrong. Publish nothing when inconclusive:
    # homelab-metric-write leaves the previous file in place, and a stale
    # number is detectable where a fabricated zero reads as good news.
    if args.metrics_dir and metrics:
        lines = "".join(f"{name} {value}\n" for name, value in sorted(metrics.items()))
        subprocess.run(
            ["homelab-metric-write", "--dir", args.metrics_dir,
             "--file", "image-generation", "--prefix", "homelab_image_generation",
             *(["--success"] if verdict == "ok" else [])],
            input=lines, text=True, check=True,
        )

    print(f"verdict={verdict}")
    return {"ok": 0, "broken": 1, "inconclusive": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Add the Makefile target**

Add `image-gen-check` to `.PHONY`, then:

```make
image-gen-check: ## Generate one image end to end and assert it is a 1024x1024 PNG
	$(PYTHON) scripts/image_generation_check.py $(ARGS)
```

Named `image-gen-check` because `image-check` already belongs to the container-digest tooling.

- [ ] **Step 3: Confirm the PNG parser rejects non-PNGs**

```bash
python -c "
import sys; sys.path.insert(0,'scripts')
from image_generation_check import png_size
# A real 1x1 PNG must parse; an HTML error page wearing an image name must not.
print(png_size(bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489')))
try:
    png_size(b'<!doctype html><title>502 Bad Gateway</title>')
except ValueError as e:
    print('rejected non-PNG:', e)
"
```

Expected: `(1, 1)` then `rejected non-PNG: not a PNG`. Both halves matter — a parser that rejects everything would make the check permanently `broken`, and one that accepts anything would let an error page count as an image.

- [ ] **Step 4: Run it live**

```bash
make image-gen-check
```

Expected: `image generation OK: 1024x1024 PNG, <n> bytes, <t>s` then `verdict=ok`, exit 0.

**If it reports 512x512**, the mapping did not reach ComfyUI — re-run `make owui-image-config` and check `GET /api/v1/images/models` returns 200. **If it hangs**, confirm Open WebUI can open a WebSocket to the GPU host; the spec flags this as newly documented and previously unverified, and the symptom is identical to the defect being fixed.

- [ ] **Step 5: Confirm ComfyUI actually executed our graph**

```bash
curl -sS http://192.168.1.40:8188/history | python -c "
import json,sys
h=json.load(sys.stdin)
print(len(h),'entries')
for pid, entry in list(h.items())[-1:]:
    print(entry['prompt'][2]['4']['inputs']['ckpt_name'])
"
```

Expected: at least one entry, and `sd_xl_base_1.0.safetensors`. This is the direct read that the 1024×1024 assertion only proxies.

- [ ] **Step 6: Commit**

```bash
make validate-catalog
git add scripts/image_generation_check.py Makefile
git commit -m "feat: prove image generation with a real PNG at the mapped size"
```

---

### Task 7: Retire the superseded environment keys

**[ansible]** — needs a machine that can run `make infra`.

The push wrote database rows for the entire image subtree, so these environment keys are now decoration. A stale environment variable that looks authoritative and is not is precisely the trap this repo keeps documenting, so they are deleted rather than left in place.

**Files:**
- Modify: `inventory/group_vars/all/infra-apps.yml:611-640`

- [ ] **Step 1: Delete the superseded keys**

Remove `ENABLE_IMAGE_GENERATION`, `IMAGE_GENERATION_ENGINE`, `COMFYUI_BASE_URL`, `IMAGE_SIZE` and `IMAGE_STEPS` from the `open-webui` env block, together with the long comment about `IMAGE_SIZE` defaults that no longer describes reality. Replace with:

```yaml
      # Image generation is configured in inventory/group_vars/all/images.yml
      # and applied with `make owui-image-config`, NOT from here.
      #
      # Open WebUI's config update endpoint upserts every image key at once, so
      # the first push wrote database rows for all of them. With
      # ENABLE_PERSISTENT_CONFIG true, a row overrides the environment
      # permanently — anything set here would render, deploy, report `changed`
      # and do nothing.
      #
      # The consequence worth knowing: flipping gpu_host_online no longer
      # disables image generation on its own. images.yml carries
      # image_generation_enabled, validate_openwebui_image_config.py fails the
      # build when the two disagree, and `make owui-image-config` applies it.
```

- [ ] **Step 2: Validate**

```bash
make validate
```

Expected: PASS. The new gate fails here if `image_generation_enabled` and `gpu_host_online` disagree.

- [ ] **Step 3: Deploy**

```bash
make infra
```

Expected: the quadlet changes and Open WebUI restarts. Confirm the restart did **not** revert the pushed config:

```bash
make image-gen-check
```

Expected: `verdict=ok`. This is the real test of the whole design — a restart with the keys absent from the environment must leave the database rows intact.

- [ ] **Step 4: Commit and re-deploy clean**

```bash
git add inventory/group_vars/all/infra-apps.yml
git commit -m "fix: stop infra-apps.yml claiming authority over image settings"
git status --porcelain   # must print nothing
make infra               # expect the three git-archive-sync tasks, then changed=0
make infra               # expect changed=0
```

---

### Task 8: Run it nightly

**[ansible]**

**Files:**
- Create: `roles/svc_infra/files/homelab-image-gen@.service`
- Create: `roles/svc_infra/files/homelab-image-gen@.timer`
- Modify: `roles/svc_infra/tasks/verify-runner.yml` (install and enable, alongside the scan units)
- Modify: `roles/svc_infra/tasks/files.yml` (deploy the token file)
- Modify: `inventory/host_vars/svc-infra.yml` (`onfailure_units_extra`)

- [ ] **Step 1: Write the service unit**

`roles/svc_infra/files/homelab-image-gen@.service`:

```ini
# Nightly end-to-end proof that in-chat image generation still works.
# The instance is the unprivileged account owning the checkout:
#   systemctl start homelab-image-gen@svcops.service
#
# Separate from homelab-verify@ because it answers a different question and
# has a different failure meaning: a red verify means the estate drifted, a red
# image-gen means one application stopped functioning while every container
# stayed green. That is the exact failure this check exists to catch, so
# collapsing it into verify would bury it.
[Unit]
Description=homelab-iac in-chat image generation check for %i
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=%i
WorkingDirectory=/opt/homelab-iac
# ExecStartPre, NOT ConditionPathIsDirectory: systemd treats an unmet condition
# as SUCCESS, so a missing venv would end nightly checking with every indicator
# green. Same correction as homelab-verify@.service and homelab-scan@.service.
ExecStartPre=/usr/bin/test -d /opt/homelab-iac/.venv
ExecStart=/opt/homelab-iac/.venv/bin/python \
    /opt/homelab-iac/scripts/image_generation_check.py \
    --token-file /etc/homelab-owui-admin.token \
    --metrics-dir /opt/homelab/appdata/node-exporter-textfile
# Generation against a card that may hold a 17 GB resident chat model, on a
# desktop that may be asleep. Long enough to be a real answer, short enough
# that a hang is reported the same night.
TimeoutStartSec=900
UMask=0077
```

- [ ] **Step 2: Write the timer**

`roles/svc_infra/files/homelab-image-gen@.timer`:

```ini
# Enabled for the runner account by roles/svc_infra/tasks/verify-runner.yml:
#   systemctl enable --now homelab-image-gen@svcops.timer
[Unit]
Description=Run the in-chat image generation check nightly for %i

[Timer]
Unit=homelab-image-gen@%i.service
# 06:15, after the 05:30 scan. Staggered rather than chained, like the others:
# an image check that hangs against a sleeping GPU must not delay a scan.
OnCalendar=*-*-* 06:15:00
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Deploy the token**

In `roles/svc_infra/tasks/files.yml`, following the existing secret-file pattern:

```yaml
- name: Install the Open WebUI admin token for the image generation check
  ansible.builtin.copy:
    # Written as root with an explicit owner rather than under become_user:
    # Ansible's copy under become_user needs setfacl, absent on these hosts.
    content: "{{ vault_openwebui_admin_token }}"
    dest: /etc/homelab-owui-admin.token
    owner: "{{ verify_runner_user }}"
    group: root
    mode: "0400"
  no_log: true
```

- [ ] **Step 4: Register the OnFailure drop-in**

In `inventory/host_vars/svc-infra.yml`, add to `onfailure_units_extra`:

```yaml
  # The image generation check publishes its own verdict, so this drop-in is
  # specifically for the check failing to RUN — which would otherwise look
  # exactly like a healthy estate, the failure the check itself exists for.
  - homelab-image-gen@.service
```

- [ ] **Step 5: Install and enable the units**

In `roles/svc_infra/tasks/verify-runner.yml`, add `homelab-image-gen@.service` and `homelab-image-gen@.timer` to the existing unit-file copy loop and the `systemctl enable --now` for the timer, following exactly how `homelab-scan@` is handled there.

- [ ] **Step 6: Validate**

```bash
make validate
```

Expected: PASS. `validate_onfailure.py` fails here if the drop-in entry is missing; `validate_systemd_units.py` parses the units (with real `systemd-analyze` in CI).

- [ ] **Step 7: Deploy and prove the alert path works**

```bash
make infra
sudo systemctl start homelab-image-gen@svcops.service
systemctl status homelab-image-gen@svcops.service --no-pager
```

Then read the metric back rather than trusting the exit code:

```bash
cat /opt/homelab/appdata/node-exporter-textfile/image-generation.prom
```

Expected: `homelab_image_generation_ok 1`, a duration, and a width of 1024.

Per `CLAUDE.md`, a timer being active does not prove alerting works. Force a failure and read it back out of ntfy:

```bash
# Force a failure: run the check against a base URL nothing answers on.
sudo systemd-run --unit=image-gen-alert-test --property=OnFailure=notify-failure@%n.service \
    /opt/homelab-iac/.venv/bin/python \
    /opt/homelab-iac/scripts/image_generation_check.py \
    --base-url http://127.0.0.1:1 --token-file /etc/homelab-owui-admin.token
# ntfy runs on svc-media (192.168.1.30), not svc-infra.
curl -s "http://192.168.1.30:8080/homelab-alerts/json?poll=1&since=10m"
```

Expected: a message naming the failed unit. `since` accepts `24h`/`168h`/`all`, not `7d`; retention is ~12h and in memory, so poll promptly — the journals are the durable record.

- [ ] **Step 8: Commit and re-deploy clean**

```bash
git add roles/svc_infra/files/homelab-image-gen@.service \
        roles/svc_infra/files/homelab-image-gen@.timer \
        roles/svc_infra/tasks/verify-runner.yml \
        roles/svc_infra/tasks/files.yml \
        inventory/host_vars/svc-infra.yml
git commit -m "feat: check nightly that image generation still produces an image"
git status --porcelain   # must print nothing
make infra               # three git-archive-sync tasks
make infra               # changed=0
```

---

### Task 9: Pony Diffusion V6 XL

**[http]** for the download and push; the GPU host is not Ansible-managed and never will be.

**Files:**
- Create: `inventory/comfyui-workflows/pony.json`
- Modify: `inventory/group_vars/all/images.yml`
- Modify: `tests/validate_openwebui_image_config.py` (`CLASS_INPUTS`)
- Modify: `docs/gpu-host.md`

- [ ] **Step 1: Download and verify the checkpoint**

On the GPU host, into `models/checkpoints/`:

```
https://huggingface.co/LyliaEngine/Pony_Diffusion_V6_XL/resolve/main/ponyDiffusionV6XL_v6StartWithThisOne.safetensors
```

```powershell
Get-FileHash -Algorithm SHA256 ponyDiffusionV6XL_v6StartWithThisOne.safetensors
```

Expected: `67ab2fd8ec439a89b3fedb15cc65f54336af163c7eb5e4f2acc98f090a29b0b3`

**Do not skip this.** A truncated or error-page download wearing a `.safetensors` name is the failure `docs/gpu-host.md` already warns about. Confirm ComfyUI sees it:

```bash
curl -s http://192.168.1.40:8188/object_info/CheckpointLoaderSimple \
  | python -c "import json,sys; print(json.load(sys.stdin)['CheckpointLoaderSimple']['input']['required']['ckpt_name'][0])"
```

- [ ] **Step 2: Add `ConditioningConcat` to the validator's class table**

In `CLASS_INPUTS`:

```python
    "ConditioningConcat": {"conditioning_to", "conditioning_from"},
```

Without this the new workflow fails Task 3's unknown-class rule — which is the rule working as designed, not a bug.

- [ ] **Step 3: Create the Pony workflow**

`inventory/comfyui-workflows/pony.json`. Node IDs `3`,`4`,`5`,`6`,`7` are unchanged from `sdxl.json` because the mapping is shared and the validator requires every mapped ID in every workflow.

Pony was trained with `score_9, score_8_up, score_7_up` and degrades visibly without them, but Open WebUI overwrites the mapped prompt node wholesale on every request — so the tags cannot live in node `6`. Node `10` holds them fixed and node `11` concatenates.

```json
{
  "3": {
    "class_type": "KSampler",
    "inputs": {
      "seed": 0,
      "steps": 28,
      "cfg": 7.0,
      "sampler_name": "dpmpp_2m",
      "scheduler": "karras",
      "denoise": 1.0,
      "model": ["4", 0],
      "positive": ["11", 0],
      "negative": ["7", 0],
      "latent_image": ["5", 0]
    }
  },
  "4": {
    "class_type": "CheckpointLoaderSimple",
    "inputs": { "ckpt_name": "ponyDiffusionV6XL_v6StartWithThisOne.safetensors" }
  },
  "5": {
    "class_type": "EmptyLatentImage",
    "inputs": { "width": 1024, "height": 1024, "batch_size": 1 }
  },
  "6": {
    "class_type": "CLIPTextEncode",
    "inputs": { "text": "placeholder, overwritten per request", "clip": ["4", 1] }
  },
  "7": {
    "class_type": "CLIPTextEncode",
    "inputs": { "text": "", "clip": ["4", 1] }
  },
  "8": {
    "class_type": "VAEDecode",
    "inputs": { "samples": ["3", 0], "vae": ["4", 2] }
  },
  "9": {
    "class_type": "SaveImage",
    "inputs": { "filename_prefix": "homelab-owui", "images": ["8", 0] }
  },
  "10": {
    "class_type": "CLIPTextEncode",
    "inputs": { "text": "score_9, score_8_up, score_7_up", "clip": ["4", 1] }
  },
  "11": {
    "class_type": "ConditioningConcat",
    "inputs": { "conditioning_to": ["10", 0], "conditioning_from": ["6", 0] }
  }
}
```

- [ ] **Step 4: Confirm the gate accepts both workflows**

```bash
python tests/validate_openwebui_image_config.py
```

Expected: `Open WebUI image config: OK (2 workflow(s))`. Still on `sdxl` — this proves the shared mapping is valid against Pony *before* selecting it, which is the whole point of checking every workflow rather than only the selected one.

- [ ] **Step 5: Switch to Pony**

In `inventory/group_vars/all/images.yml`:

```yaml
image_workflow: pony
image_generation_model: "ponyDiffusionV6XL_v6StartWithThisOne.safetensors"
```

- [ ] **Step 6: Validate, push, and prove**

```bash
make validate-catalog
make owui-image-config
make image-gen-check
```

Expected: `verdict=ok` at 1024×1024.

- [ ] **Step 7: Confirm the score tags actually reached ComfyUI**

The size assertion proves our workflow ran; it cannot see the tags. Read the executed graph directly:

```bash
curl -sS http://192.168.1.40:8188/history | python -c "
import json,sys
h=json.load(sys.stdin)
last=list(h.values())[-1]['prompt'][2]
print('ckpt:', last['4']['inputs']['ckpt_name'])
print('tags:', last['10']['inputs']['text'])
print('user:', last['6']['inputs']['text'])
"
```

Expected: the Pony checkpoint, `score_9, score_8_up, score_7_up` intact in node `10`, and the check's prompt in node `6`. History is in-memory and this read races with concurrent use, so treat a miss as inconclusive and re-run rather than as proof of failure.

- [ ] **Step 8: Document the checkpoint on the GPU host**

Add Pony to the checkpoint list in `docs/gpu-host.md`, with its checksum and the note that `LyliaEngine/Pony_Diffusion_V6_XL` is an unaffiliated mirror because CivitAI requires an API token — `.safetensors` cannot execute on load, and the checksum pins the reviewed bytes.

- [ ] **Step 9: Commit**

```bash
make validate
git add inventory/comfyui-workflows/pony.json inventory/group_vars/all/images.yml \
        tests/validate_openwebui_image_config.py docs/gpu-host.md
git commit -m "feat: switch in-chat generation to Pony with its score tags intact"
```

---

## Finishing

Follow `CLAUDE.md`'s change workflow to close out:

1. `git status --porcelain` prints nothing.
2. `make infra` from the clean tree — expect the three git-archive-sync tasks on svc-infra, then `changed=0` on a second run. Check *which* tasks changed; do not quote the second number to paper over a genuine diff.
3. `make verify`.
4. `make image-gen-check` — `verdict=ok`.
5. Merge to `main`, push, delete the branch locally and on the remote.

**One follow-up this plan deliberately does not do.** `docs/plans/uncensored-image-generation.md` contains two claims the spec corrects (the `inputs[None]` mechanism, and the `/history` positive control). That file is also modified by the unmerged `docs/inference-capacity-roster` branch, so editing it here would create a merge conflict for the machine finishing that work. Correct it once that branch lands.
