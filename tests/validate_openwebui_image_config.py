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
import re
import sys
from pathlib import Path

import yaml

# Which `make validate-*` target runs this gate. Discovered by
# tests/run_gates.py, so a gate with no group fails the build rather than
# silently never running.
GATE_GROUP = "catalog"

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "inventory" / "group_vars" / "all" / "images.yml"
MAIN_VARS_PATH = ROOT / "inventory" / "group_vars" / "all" / "main.yml"
WORKFLOW_DIR = ROOT / "inventory" / "comfyui-workflows"
EDIT_WORKFLOW_DIR = ROOT / "inventory" / "comfyui-edit-workflows"

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

# Mapping entries an edit config cannot work without. Unlike generation,
# width/height are not required (design §1 — ComfyUIEditImageForm forwards
# them only if IMAGE_EDIT_SIZE is set, and this workflow derives size from
# the input image instead).
EDIT_REQUIRED_TYPES = {"model", "prompt", "image", "steps", "seed"}

# negative_prompt has no field on ComfyUIEditImageForm at all (design §1) —
# unlike generation, where it is merely optional-and-safe, here it is
# actively wrong: _apply_workflow_nodes would raise AttributeError on
# payload.negative_prompt, swallowed into a silent None by comfyui_edit_image.
EDIT_FORBIDDEN_TYPES = {"negative_prompt"}

# ComfyUI collects output images only from these two class_types
# (_ws_get_images). A workflow ending in neither returns an empty image list:
# generation succeeds, no image, no error.
OUTPUT_CLASSES = {"SaveImage", "PreviewImage"}

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
    "ConditioningConcat": {"conditioning_to", "conditioning_from"},
    "UNETLoader": {"unet_name", "weight_dtype"},
    "CLIPLoader": {"clip_name", "type", "device"},
    "VAELoader": {"vae_name"},
    "LoadImage": {"image"},
    "TextEncodeQwenImageEdit": {"clip", "vae", "image", "prompt"},
    "ModelSamplingAuraFlow": {"model", "shift"},
    "CFGNorm": {"model", "strength"},
    # resolution_steps is a required input on the live host (Task 2's
    # /object_info query), not just the three the design's provisional
    # table assumed — omitted here it would reject any mapping entry that
    # legitimately targets it.
    "ImageScaleToTotalPixels": {"image", "upscale_method", "megapixels",
                                 "resolution_steps"},
    "VAEEncode": {"pixels", "vae"},
}


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


def good_edit_catalog() -> dict:
    return {
        "image_edit_workflow": "qwen-image-edit",
        "image_edit_enabled": True,
        "image_edit_model": "qwen_image_edit_fp8_e4m3fn.safetensors",
        "image_edit_workflow_nodes": [
            {"type": "model", "key": "unet_name", "node_ids": ["10"]},
            {"type": "prompt", "key": "prompt", "node_ids": ["15"]},
            {"type": "image", "key": "image", "node_ids": ["13"]},
            {"type": "steps", "key": "steps", "node_ids": ["20"]},
            {"type": "seed", "key": "seed", "node_ids": ["20"]},
        ],
    }


def good_edit_workflow() -> dict:
    return {
        "10": {"class_type": "UNETLoader",
               "inputs": {"unet_name": "qwen_image_edit_fp8_e4m3fn.safetensors"}},
        "13": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
        "15": {"class_type": "TextEncodeQwenImageEdit",
               "inputs": {"clip": None, "vae": None, "image": None, "prompt": ""}},
        "20": {"class_type": "KSampler",
               "inputs": {"seed": 0, "steps": 50, "cfg": 4.0}},
        "22": {"class_type": "SaveImage",
               "inputs": {"filename_prefix": "x", "images": None}},
    }


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


def _empty_node_ids(catalog, workflows, main_vars):
    # The width mapping is present -- REQUIRED_TYPES is happy -- but its
    # node_ids is empty, so the value never reaches ComfyUI.
    catalog["image_workflow_nodes"][3]["node_ids"] = []


# Each case is (name, mutation, substring that must appear in a failure).
# A mutation takes (catalog, workflows, main_vars) and breaks exactly one rule.
VALIDATION_CASES = (
    ("workflow with no SaveImage/PreviewImage", _drop_output_node, "SaveImage"),
    ("editor-format workflow", _editor_format, "editor format"),
    ("image_workflow names a missing file", _missing_workflow_file, "nonexistent"),
    ("comfyui_base_url disagrees with gpu_host_ip", _drift_base_url, "gpu_host_ip"),
    ("image_generation_enabled disagrees with gpu_host_online", _drift_enabled,
     "gpu_host_online"),
    ("mapped node absent from a non-selected workflow",
     _node_id_absent_from_one_workflow, "other"),
    ("unrecognised node type", _unknown_node_type, "checkpoint"),
    ("model node without an explicit key", _model_node_without_key, "explicit key"),
    ("image-type node in a generation mapping",
     _image_type_in_generation_mapping, "AttributeError"),
    ("required mapping type missing", _missing_required_type, "seed"),
    ("mapped key the node class does not accept", _key_not_accepted_by_class,
     "unet_name"),
    ("workflow node of an unknown class", _unknown_class_type, "SomeCustomLoader"),
    ("image_size not WxH", _bad_image_size, "image_size"),
    ("negative image_steps", _negative_steps, "image_steps"),
    ("empty image_generation_model", _empty_model, "image_generation_model"),
    ("empty node_ids on a mapping entry", _empty_node_ids, "node_ids"),
)


def _edit_negative_prompt_forbidden(catalog, workflows, edit_workflows, main_vars):
    catalog["image_edit_workflow_nodes"].append(
        {"type": "negative_prompt", "key": "text", "node_ids": ["15"]})


def _edit_missing_image_type(catalog, workflows, edit_workflows, main_vars):
    catalog["image_edit_workflow_nodes"] = [
        n for n in catalog["image_edit_workflow_nodes"] if n["type"] != "image"]


def _edit_node_id_absent_from_edit_workflow(catalog, workflows, edit_workflows, main_vars):
    other = good_edit_workflow()
    del other["13"]
    edit_workflows["other"] = other


def _edit_empty_model(catalog, workflows, edit_workflows, main_vars):
    catalog["image_edit_model"] = ""


def _edit_drift_enabled(catalog, workflows, edit_workflows, main_vars):
    main_vars["gpu_host_online"] = False


def _edit_drop_output_node(catalog, workflows, edit_workflows, main_vars):
    del edit_workflows["qwen-image-edit"]["22"]


def _edit_editor_format(catalog, workflows, edit_workflows, main_vars):
    edit_workflows["qwen-image-edit"] = {"nodes": [{"id": 10}], "links": []}


def _edit_missing_workflow_file(catalog, workflows, edit_workflows, main_vars):
    catalog["image_edit_workflow"] = "nonexistent"


def _edit_unknown_node_type(catalog, workflows, edit_workflows, main_vars):
    catalog["image_edit_workflow_nodes"][0]["type"] = "checkpoint"


def _edit_model_node_without_key(catalog, workflows, edit_workflows, main_vars):
    del catalog["image_edit_workflow_nodes"][0]["key"]


def _edit_unknown_class_type(catalog, workflows, edit_workflows, main_vars):
    edit_workflows["qwen-image-edit"]["10"]["class_type"] = "SomeCustomLoader"


def _edit_key_not_accepted_by_class(catalog, workflows, edit_workflows, main_vars):
    # ckpt_name is right for CheckpointLoaderSimple and wrong for UNETLoader.
    # Node "10" exists, so the ID-based rule passes and the value is simply
    # ignored at edit time.
    catalog["image_edit_workflow_nodes"][0]["key"] = "ckpt_name"


def _edit_empty_node_ids(catalog, workflows, edit_workflows, main_vars):
    # The image mapping is present -- EDIT_REQUIRED_TYPES is happy -- but its
    # node_ids is empty, so the value never reaches ComfyUI.
    catalog["image_edit_workflow_nodes"][2]["node_ids"] = []


# Each case is (name, mutation, substring that must appear in a failure).
# A mutation takes (catalog, workflows, edit_workflows, main_vars) and breaks
# exactly one rule in check_edit_config.
EDIT_VALIDATION_CASES = (
    ("negative_prompt in edit mapping", _edit_negative_prompt_forbidden,
     "no field on ComfyUIEditImageForm"),
    ("edit mapping missing required image type", _edit_missing_image_type, "image"),
    ("mapped node absent from a non-selected edit workflow",
     _edit_node_id_absent_from_edit_workflow, "other"),
    ("empty image_edit_model", _edit_empty_model, "image_edit_model"),
    ("image_edit_enabled disagrees with gpu_host_online", _edit_drift_enabled,
     "gpu_host_online"),
    ("edit workflow with no SaveImage/PreviewImage", _edit_drop_output_node,
     "SaveImage"),
    ("editor-format edit workflow", _edit_editor_format, "editor format"),
    ("image_edit_workflow names a missing file", _edit_missing_workflow_file,
     "nonexistent"),
    ("unrecognised edit node type", _edit_unknown_node_type, "checkpoint"),
    ("edit model node without an explicit key", _edit_model_node_without_key,
     "explicit key"),
    ("edit workflow node of an unknown class", _edit_unknown_class_type,
     "SomeCustomLoader"),
    ("mapped key the edit node class does not accept",
     _edit_key_not_accepted_by_class, "ckpt_name"),
    ("empty node_ids on an edit mapping entry", _edit_empty_node_ids, "node_ids"),
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

    edit_baseline = check_edit_config(
        good_edit_catalog(), {"qwen-image-edit": good_edit_workflow()},
        good_main_vars())
    if edit_baseline:
        problems.append(
            f"the known-good EDIT configuration failed: {edit_baseline} — every "
            "edit case below is measured against it, so the whole edit gate is "
            "untrustworthy"
        )

    for name, mutate, expected in EDIT_VALIDATION_CASES:
        catalog = good_edit_catalog()
        workflows = {"sdxl": good_workflow()}
        edit_workflows = {"qwen-image-edit": good_edit_workflow()}
        main_vars = good_main_vars()
        mutate(catalog, workflows, edit_workflows, main_vars)
        failures = check_edit_config(catalog, edit_workflows, main_vars)
        if not any(expected in f for f in failures):
            problems.append(
                f"edit case {name!r} did not produce a failure mentioning "
                f"{expected!r} (got {failures}) — this rule is not enforced, so "
                "a config breaking it would deploy and produce no image and no "
                "error"
            )
    return problems


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

        node_ids = node.get("node_ids")
        if (not isinstance(node_ids, list) or not node_ids
                or not all(isinstance(nid, str) for nid in node_ids)):
            failures.append(
                f"{where} has node_ids {node_ids!r}; it must be a non-empty "
                "list of strings. A missing key, a non-list, an empty list, "
                "or a non-string element each map the value to nothing while "
                "REQUIRED_TYPES still sees the type as present — the exact "
                "silent failure this gate exists to catch"
            )
            continue

        for node_id in node_ids:
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

    for required in sorted(REQUIRED_TYPES - seen_types):
        failures.append(
            f"image_workflow_nodes has no {required!r} entry — that value would "
            "never reach ComfyUI and the workflow's hardcoded one would be used"
        )

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

    return failures


def check_edit_config(catalog: dict, edit_workflows: dict[str, dict],
                       main_vars: dict) -> list[str]:
    """Return human-readable failures for the EDIT mapping. Empty means sound."""
    failures: list[str] = []

    selected = catalog.get("image_edit_workflow")
    if selected not in edit_workflows:
        failures.append(
            f"image_edit_workflow is {selected!r} but no such file exists in "
            f"inventory/comfyui-edit-workflows/ (have: {sorted(edit_workflows)})"
        )

    # Mirrors check_config's image_generation_enabled vs gpu_host_online
    # check. image_edit_enabled DUPLICATES gpu_host_online for the same
    # reason (design §1 / images.yml comment): the push retired the
    # environment gate, so this catalog field is the only thing left that
    # can still turn the edit feature off.
    if bool(catalog.get("image_edit_enabled")) != bool(
            main_vars.get("gpu_host_online")):
        failures.append(
            "image_edit_enabled disagrees with gpu_host_online in "
            "main.yml — since the push retired the environment gate, this "
            "catalog is the only thing that still turns the edit feature off"
        )

    for name, workflow in sorted(edit_workflows.items()):
        if not isinstance(workflow, dict):
            failures.append(f"edit workflow {name!r} is not a JSON object")
            continue
        if isinstance(workflow.get("nodes"), list):
            failures.append(
                f"edit workflow {name!r} looks like ComfyUI's editor format. "
                "Open WebUI can only read the API format — re-export with "
                "Workflow -> Export (API), or hand-flatten as this repo does"
            )
            continue
        bad = [nid for nid, node in workflow.items()
               if not isinstance(node, dict) or "class_type" not in node
               or "inputs" not in node]
        if bad:
            failures.append(
                f"edit workflow {name!r} nodes {sorted(bad)} lack "
                "class_type/inputs — not valid API format"
            )
            continue
        classes = {node["class_type"] for node in workflow.values()}
        if not (classes & OUTPUT_CLASSES):
            failures.append(
                f"edit workflow {name!r} contains no SaveImage or "
                "PreviewImage node — _ws_get_images would return no images"
            )

    nodes = catalog.get("image_edit_workflow_nodes") or []
    if not isinstance(nodes, list):
        failures.append("image_edit_workflow_nodes must be a list")
        return failures

    seen_types: set[str] = set()
    for index, node in enumerate(nodes):
        node_type = node.get("type")
        where = f"image_edit_workflow_nodes[{index}] (type={node_type!r})"
        seen_types.add(node_type)

        if node_type is not None and node_type not in HANDLED_TYPES:
            failures.append(
                f"{where} is not a type Open WebUI handles"
            )
        if node_type in EDIT_FORBIDDEN_TYPES:
            failures.append(
                f"{where} has no field on ComfyUIEditImageForm. "
                "_apply_workflow_nodes would raise AttributeError, swallowed "
                "by comfyui_edit_image into a silent None — no image, no error"
            )
        if node_type in NEEDS_EXPLICIT_KEY and "key" not in node:
            failures.append(f"{where} needs an explicit key")

        node_ids = node.get("node_ids")
        if (not isinstance(node_ids, list) or not node_ids
                or not all(isinstance(nid, str) for nid in node_ids)):
            failures.append(
                f"{where} has node_ids {node_ids!r}; it must be a non-empty "
                "list of strings"
            )
            continue

        for node_id in node_ids:
            for name, workflow in sorted(edit_workflows.items()):
                if isinstance(workflow.get("nodes"), list):
                    continue
                if node_id not in workflow:
                    failures.append(
                        f"{where} maps node id {node_id!r}, absent from edit "
                        f"workflow {name!r}. The mapping is shared across "
                        "every committed EDIT workflow so switching "
                        "image_edit_workflow can never discover a broken mapping"
                    )
                else:
                    class_type = workflow[node_id]["class_type"]
                    accepted = CLASS_INPUTS.get(class_type)
                    key = node.get("key", "text")
                    if accepted is None:
                        failures.append(
                            f"edit workflow {name!r} node {node_id!r} has "
                            f"class_type {class_type!r}, not in CLASS_INPUTS"
                        )
                    elif key not in accepted:
                        failures.append(
                            f"{where} writes key {key!r} into edit workflow "
                            f"{name!r} node {node_id!r} ({class_type}), which "
                            f"accepts {sorted(accepted)}"
                        )

    for required in sorted(EDIT_REQUIRED_TYPES - seen_types):
        failures.append(
            f"image_edit_workflow_nodes has no {required!r} entry"
        )

    if not (catalog.get("image_edit_model") or "").strip():
        failures.append("image_edit_model is empty")

    return failures


def load_all(root: Path) -> tuple[dict, dict[str, dict], dict[str, dict], dict]:
    catalog = yaml.safe_load((root / "inventory" / "group_vars" / "all"
                              / "images.yml").read_text())
    main_vars = yaml.safe_load((root / "inventory" / "group_vars" / "all"
                                / "main.yml").read_text())
    workflows = {}
    for path in sorted((root / "inventory" / "comfyui-workflows").glob("*.json")):
        workflows[path.stem] = json.loads(path.read_text())
    edit_workflows = {}
    for path in sorted((root / "inventory" / "comfyui-edit-workflows").glob("*.json")):
        edit_workflows[path.stem] = json.loads(path.read_text())
    return catalog, workflows, edit_workflows, main_vars


def main() -> int:
    failures: list[str] = validation_self_check()

    catalog, workflows, edit_workflows, main_vars = load_all(ROOT)
    if not workflows:
        print(f"no workflows found in {WORKFLOW_DIR}", file=sys.stderr)
        return 1
    if not edit_workflows:
        print(f"no edit workflows found in {EDIT_WORKFLOW_DIR}", file=sys.stderr)
        return 1
    failures.extend(check_config(catalog, workflows, main_vars))
    failures.extend(check_edit_config(catalog, edit_workflows, main_vars))

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"Open WebUI image config: OK ({len(workflows)} generation "
          f"workflow(s), {len(edit_workflows)} edit workflow(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
