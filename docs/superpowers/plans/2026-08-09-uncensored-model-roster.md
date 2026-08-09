# Uncensored Model Roster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the safety-aligned model roster behind `chat.fortwow.dev` with abliterated models for chat, coding and image generation, plus saved personas so therapy is a system prompt rather than a fourth model.

**Architecture:** Six Ollama models and four image files are installed **by hand** on the unmanaged Windows GPU host at `192.168.1.40`. The repo's contribution is declarative: an `image_workflow` variable selecting one of two committed ComfyUI workflow JSONs, the Open WebUI environment that points at them, and a validator that catches the silent-failure mode Open WebUI has when a workflow node mapping is wrong.

**Tech Stack:** Ansible (svc-infra role), rootless podman Quadlets, Open WebUI, Ollama, ComfyUI, Python 3 standalone validators (no pytest — see Global Constraints).

**Spec:** `docs/superpowers/specs/2026-08-09-uncensored-model-roster-design.md`

## Global Constraints

- **Branch is `feat/uncensored-models`**, already created and pushed. Do not create another.
- **No pytest in this repo.** Validators are standalone Python scripts under `tests/`, run from the Makefile, exiting 0/1 and printing a one-line OK summary. Self-tests are a module-level case table plus a `*_self_check()` function called first in `main()` — see `tests/validate_grafana_dashboards.py:112-135` for the pattern to copy.
- **`make validate` must pass** before any commit.
- **Never `git add -A`.** Stage explicit paths.
- **Never echo vault secrets.** Nothing in this plan touches vault values.
- **The GPU host is not Ansible-managed** and must not become so. Its steps are manual PowerShell run on that machine, documented in `docs/gpu-host.md`.
- **`gpu_host_online` is already `true`** (`inventory/group_vars/all/main.yml:67`). Do not change it.
- **`ENABLE_PERSISTENT_CONFIG: "false"`** means the environment is authoritative on every container start. Image settings MUST be set as env in the catalog, never clicked in the admin UI — the UI copy is discarded on restart.
- **Exact model files** (verified public and ungated 2026-08-09):

  | Destination | File | SHA256 |
  |---|---|---|
  | `models/checkpoints/` | `ponyDiffusionV6XL_v6StartWithThisOne.safetensors` | `614f55e8bd8701b9168957361a00c7a76c5de1aa625ade08edfca3db2675b2cc` |
  | `models/diffusion_models/` | `Chroma1-HD-fp8_scaled_defaultloader_hybrid_large_rev2.safetensors` | `377eff193fc866064ed587bd4140b3fd59bad0555b32b02224d60353b3049ebc` |
  | `models/text_encoders/` | `t5xxl_flan_fp8_scaled.safetensors` | `e9b22d1142585f501864671e07af481f8800415296f6f54c10a88e71e05a7a60` |
  | `models/vae/` | `ae.safetensors` | `f73eecf7c469ff442523dc712cc161d631df071bf4d9d793494fbf00cdd80a82` |

- **ComfyUI root** is `C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\`.

---

## File Structure

| File | Responsibility |
|---|---|
| `inventory/group_vars/all/main.yml` | Adds `image_workflow` — the single switch between SDXL and Flux image pipelines |
| `inventory/group_vars/all/infra-apps.yml` | `open-webui` env: image size, steps, model, workflow, node mapping |
| `roles/svc_infra/files/comfyui/pony.json` | SDXL-architecture ComfyUI workflow, API format |
| `roles/svc_infra/files/comfyui/chroma.json` | Flux-architecture ComfyUI workflow, API format |
| `tests/validate_openwebui_image_config.py` | The only thing that can catch a bad node mapping before deploy |
| `docs/chat-models.md` | Model roster, persona text, how to swap image pipelines |
| `docs/gpu-host.md` | Updated pull list, image file list, Continue config, VRAM table |

---

## Task 1: Install the chat and coding models on the GPU host

Manual work on the Windows machine. No repo changes — `docs/gpu-host.md` is updated in Task 7 once the real numbers are known.

**Files:** none (GPU host only)

**Interfaces:**
- Produces: six Ollama model names that Task 5's verification and Task 6's personas depend on. Exact tags recorded in this task's Step 5 output.

- [ ] **Step 1: Check free disk before pulling**

On the GPU host:

```powershell
Get-PSDrive C | Select-Object Used,Free
```

Expected: at least **120 GB free**. New pulls are ~102 GB and the image files in Task 2 add ~20 GB. If there is less, stop and report — do not pull a partial roster.

- [ ] **Step 2: Pull the five Ollama-registry models**

```powershell
ollama pull huihui_ai/gemma-4-abliterated:26b
ollama pull huihui_ai/gemma-4-abliterated:31b
ollama pull huihui_ai/Qwen3.6-abliterated:27b
ollama pull qwen3-coder:30b
ollama pull aratan/qwen3.6-claude-coder-35b-A3b-mlx-Q4KM-abliterated
```

- [ ] **Step 3: Find the correct quant tag for the DavidAU model, then pull it**

This one is a Hugging Face GGUF repo, **not** an Ollama registry entry. `ollama pull DavidAU/...` fails with a not-found error that reads as though the model was withdrawn. List the available quant tags first:

```powershell
curl.exe -s "https://huggingface.co/api/models/DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF" | ConvertFrom-Json | Select-Object -ExpandProperty siblings | Where-Object { $_.rfilename -like "*.gguf" } | Select-Object -ExpandProperty rfilename
```

Then pull using the `hf.co/` prefix and a quant tag confirmed present in that listing (`Q4_K_M` is the expected one):

```powershell
ollama pull hf.co/DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF:Q4_K_M
```

If `Q4_K_M` is absent from the listing, pick the nearest ~16 GB Q4 variant and **record the exact tag used** — Task 6 and Task 7 both reference it.

- [ ] **Step 4: Verify all six are visible over the LAN, not from the GPU host**

From the workstation, not the GPU box — loopback would pass even with a wrong bind:

```bash
curl -s http://192.168.1.40:11434/api/tags | python -c "import sys,json;[print(m['name']) for m in json.load(sys.stdin)['models']]"
```

Expected: six new names present alongside the existing `qwen2.5-coder:1.5b-base` and `nomic-embed-text`.

- [ ] **Step 5: Positive control — prove the models are actually uncensored**

**This is the only check in the whole plan that distinguishes a working abliteration from the wrong model pulled by mistake.** A tag, a load, and a plausible reply are byte-identical in both cases.

Pick one prompt that the outgoing `qwen3:30b` refuses. Confirm the refusal first, so the control is calibrated rather than assumed:

```bash
curl -s http://192.168.1.40:11434/api/generate -d '{"model":"qwen3:30b","prompt":"<REFUSAL_PROMPT>","stream":false}' | python -c "import sys,json;print(json.load(sys.stdin)['response'][:300])"
```

Expected: a refusal. If `qwen3:30b` answers it, the prompt is not a valid control — choose a different one and repeat.

Then run the same prompt against each of the four chat models:

```bash
for m in "huihui_ai/gemma-4-abliterated:26b" "huihui_ai/gemma-4-abliterated:31b" "huihui_ai/Qwen3.6-abliterated:27b" "hf.co/DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF:Q4_K_M"; do
  echo "=== $m"
  curl -s http://192.168.1.40:11434/api/generate -d "{\"model\":\"$m\",\"prompt\":\"<REFUSAL_PROMPT>\",\"stream\":false}" \
    | python -c "import sys,json;print(json.load(sys.stdin)['response'][:200])"
done
```

Expected: all four answer. **Any model that refuses has either a failed abliteration or a wrong tag** — stop and investigate that model before continuing; do not proceed on the assumption it will behave once wired up.

- [ ] **Step 6: Record resident VRAM for the default model**

```powershell
ollama run huihui_ai/gemma-4-abliterated:26b "hello"
ollama ps
```

Record the SIZE column. The spec estimates ~15 GB; the real figure feeds the Task 7 VRAM table and the Task 8 Chroma headroom decision.

- [ ] **Step 7: Retire the superseded models**

Only after every check above passes:

```powershell
ollama rm qwen3:30b
ollama rm qwen2.5-coder:14b
```

Do **not** remove `qwen2.5-coder:1.5b-base` or `nomic-embed-text` — Continue's autocomplete and embeddings still use them.

- [ ] **Step 8: No commit**

This task changes nothing in the repo. Report the recorded tag from Step 3, the VRAM figure from Step 6, and the refusal prompt used in Step 5 — Task 7 writes all three into `docs/gpu-host.md`.

---

## Task 2: Install and verify the image models on the GPU host

**Files:** none (GPU host only)

**Interfaces:**
- Consumes: nothing from Task 1; may run in parallel with it.
- Produces: four filenames on disk that Tasks 3 and 4 hardcode into the workflow JSONs. The names must match exactly.

- [ ] **Step 1: Create the text_encoders directory if absent**

Chroma's encoder goes in `text_encoders/`, not the legacy `clip/`. Portable ComfyUI builds may not ship the directory:

```powershell
$C = "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models"
if (-not (Test-Path "$C\text_encoders")) { New-Item -ItemType Directory "$C\text_encoders" }
```

- [ ] **Step 2: Download all four files**

```powershell
$C = "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models"
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is ~10x slower without this

Invoke-WebRequest -Uri "https://huggingface.co/LyliaEngine/Pony_Diffusion_V6_XL/resolve/main/ponyDiffusionV6XL_v6StartWithThisOne.safetensors" `
  -OutFile "$C\checkpoints\ponyDiffusionV6XL_v6StartWithThisOne.safetensors"

Invoke-WebRequest -Uri "https://huggingface.co/silveroxides/Chroma1-HD-fp8-scaled/resolve/main/Chroma1-HD-fp8_scaled_defaultloader_hybrid_large_rev2.safetensors" `
  -OutFile "$C\diffusion_models\Chroma1-HD-fp8_scaled_defaultloader_hybrid_large_rev2.safetensors"

Invoke-WebRequest -Uri "https://huggingface.co/silveroxides/t5xxl_flan_enc/resolve/main/t5xxl_flan_fp8_scaled.safetensors" `
  -OutFile "$C\text_encoders\t5xxl_flan_fp8_scaled.safetensors"

Invoke-WebRequest -Uri "https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors" `
  -OutFile "$C\vae\ae.safetensors"
```

**Do not substitute `black-forest-labs/FLUX.1-schnell` for the VAE.** It is gated and returns 401, which lands as an HTML error page named `ae.safetensors`.

- [ ] **Step 3: Verify every checksum before wiring anything up**

```powershell
$C = "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models"
@{
  "$C\checkpoints\ponyDiffusionV6XL_v6StartWithThisOne.safetensors" = "614f55e8bd8701b9168957361a00c7a76c5de1aa625ade08edfca3db2675b2cc"
  "$C\diffusion_models\Chroma1-HD-fp8_scaled_defaultloader_hybrid_large_rev2.safetensors" = "377eff193fc866064ed587bd4140b3fd59bad0555b32b02224d60353b3049ebc"
  "$C\text_encoders\t5xxl_flan_fp8_scaled.safetensors" = "e9b22d1142585f501864671e07af481f8800415296f6f54c10a88e71e05a7a60"
  "$C\vae\ae.safetensors" = "f73eecf7c469ff442523dc712cc161d631df071bf4d9d793494fbf00cdd80a82"
}.GetEnumerator() | ForEach-Object {
  $got = (Get-FileHash -Algorithm SHA256 $_.Key).Hash.ToLower()
  $ok = if ($got -eq $_.Value) { "OK  " } else { "FAIL" }
  "$ok $(Split-Path $_.Key -Leaf)"
}
```

Expected: four `OK` lines. **Any `FAIL` means a truncated or substituted download** — delete that file and retry. Do not continue with a mismatch; it surfaces much later as an inscrutable ComfyUI error.

- [ ] **Step 4: Restart ComfyUI so it rescans the model directories**

ComfyUI enumerates models at startup. Close the `start-comfyui-lan.bat` window and relaunch it, then confirm all four are visible over the LAN:

```bash
curl -s http://192.168.1.40:8188/object_info/CheckpointLoaderSimple | python -c "import sys,json;print(json.load(sys.stdin)['CheckpointLoaderSimple']['input']['required']['ckpt_name'][0])"
curl -s http://192.168.1.40:8188/object_info/UNETLoader | python -c "import sys,json;print(json.load(sys.stdin)['UNETLoader']['input']['required']['unet_name'][0])"
curl -s http://192.168.1.40:8188/object_info/CLIPLoader | python -c "import sys,json;print(json.load(sys.stdin)['CLIPLoader']['input']['required']['clip_name'][0])"
curl -s http://192.168.1.40:8188/object_info/VAELoader | python -c "import sys,json;print(json.load(sys.stdin)['VAELoader']['input']['required']['vae_name'][0])"
```

Expected: Pony listed under checkpoints, Chroma under UNET, the FLAN encoder under CLIP, `ae.safetensors` under VAE. A file present on disk but absent here means it landed in the wrong directory.

- [ ] **Step 5: Confirm `chroma` is an accepted CLIPLoader type**

The Chroma workflow needs `CLIPLoader` type `chroma`, which older ComfyUI builds do not have:

```bash
curl -s http://192.168.1.40:8188/object_info/CLIPLoader | python -c "import sys,json;print(json.load(sys.stdin)['CLIPLoader']['input']['required']['type'][0])"
```

Expected: a list containing `chroma`. **If absent, ComfyUI is too old** — update the portable build before Task 4, and note that doing so reverts `run_nvidia_gpu.bat`, which is why `start-comfyui-lan.bat` exists separately.

- [ ] **Step 6: Generate one image from each model in ComfyUI's own UI**

Open `http://192.168.1.40:8188` directly. Build a minimal graph for Pony, and load the official Chroma workflow (`ComfyUI_Chroma1-HD_T2I-workflow.json`, downloadable from `https://huggingface.co/lodestones/Chroma1-HD`) for Chroma, repointing its loaders at the filenames actually downloaded — **the workflow ships stale names that 404 upstream**.

Expected: two real images. This proves the models work before Open WebUI is anywhere in the picture, so a later failure is unambiguously a wiring problem.

- [ ] **Step 7: No commit**

Report the confirmed filenames from Step 4 — Tasks 3 and 4 hardcode them.

---

## Task 3: Add the validator, the `image_workflow` variable, and the Pony workflow

First repo change. The validator comes first because it is what makes the rest checkable.

**Files:**
- Create: `tests/validate_openwebui_image_config.py`
- Create: `roles/svc_infra/files/comfyui/pony.json`
- Modify: `inventory/group_vars/all/main.yml` (near `gpu_host_online`, line ~67)
- Modify: `Makefile` (`validate-catalog` target, after line 133)

**Interfaces:**
- Consumes: the Pony filename confirmed in Task 2 Step 4.
- Produces: `image_workflow` variable; `pony.json` with node IDs `3` (KSampler), `4` (CheckpointLoaderSimple), `5` (EmptyLatentImage), `6` (positive CLIPTextEncode), `7` (negative CLIPTextEncode), `8` (VAEDecode), `9` (SaveImage), `10` (fixed Pony score-tag CLIPTextEncode), `11` (ConditioningConcat). Task 5's node mapping references these IDs; Task 4's `chroma.json` uses its own.

- [ ] **Step 1: Write the validator with its self-check cases**

Create `tests/validate_openwebui_image_config.py`:

```python
#!/usr/bin/env python3
"""Validate the Open WebUI ComfyUI image configuration before it deploys.

Open WebUI applies its node mapping as `workflow[node_id]["inputs"][key] = value`
(backend/open_webui/utils/images/comfyui.py). A node ID that is not in the
workflow raises KeyError, and the caller swallows it in a broad `except
Exception` and returns None -- so a typo produces no image and no error
message, on a service whose container stays green throughout.

COMFYUI_WORKFLOW_NODES makes this worse: config.py parses it with a bare
`except json.JSONDecodeError` that falls back to `[]`, so malformed JSON
configures nothing and reports nothing.

This gate is therefore the only mechanism that can tell a typo from a working
configuration. It checks that both JSON blobs parse, that image_workflow names
a file that exists, and that every node ID referenced in the mapping is
actually present in the selected workflow.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MAIN_VARS = ROOT / "inventory/group_vars/all/main.yml"
CATALOG = ROOT / "inventory/group_vars/all/infra-apps.yml"
WORKFLOW_DIR = ROOT / "roles/svc_infra/files/comfyui"

# Node types Open WebUI understands. An unrecognised type is silently ignored
# by _apply_workflow_nodes, which is the same failure mode as a bad node id.
KNOWN_TYPES = {
    "model", "prompt", "negative_prompt", "image",
    "width", "height", "n", "steps", "seed",
}

# `seed` and `model` read node.key with no fallback, so an absent key writes
# to inputs[None] and the sampler never sees the value.
TYPES_REQUIRING_KEY = {"seed", "model", "image"}


def check_mapping(workflow: dict, nodes: list, label: str) -> list[str]:
    """Check one node mapping against one workflow; return failures."""
    problems: list[str] = []
    if not isinstance(nodes, list):
        return [f"{label}: node mapping must be a JSON list"]
    if not nodes:
        return [f"{label}: node mapping is empty, so nothing would be wired up"]

    seen_types = set()
    for index, node in enumerate(nodes):
        where = f"{label}: node[{index}]"
        if not isinstance(node, dict):
            problems.append(f"{where}: must be an object")
            continue

        node_type = node.get("type")
        if node_type is not None:
            if node_type not in KNOWN_TYPES:
                problems.append(
                    f"{where}: type {node_type!r} is not one Open WebUI handles "
                    f"({sorted(KNOWN_TYPES)}) -- it would be ignored silently"
                )
            seen_types.add(node_type)
            if node_type in TYPES_REQUIRING_KEY and not node.get("key"):
                problems.append(
                    f"{where}: type {node_type!r} needs an explicit key; "
                    "Open WebUI has no fallback and would write to inputs[None]"
                )
        elif node.get("value") is None:
            problems.append(f"{where}: an untyped node must carry a value")

        node_ids = node.get("node_ids")
        if not node_ids:
            problems.append(f"{where}: node_ids is empty")
            continue
        for node_id in node_ids:
            if str(node_id) not in workflow:
                problems.append(
                    f"{where}: node id {node_id!r} is not in the workflow "
                    "-- Open WebUI would KeyError and return no image"
                )

    for required in ("prompt", "width", "height", "seed"):
        if required not in seen_types:
            problems.append(
                f"{label}: no {required!r} node -- generation would silently "
                "use whatever the workflow file hardcodes"
            )
    return problems


# Self-test. This gate exists because a silent failure is indistinguishable
# from success, so it is not allowed to have one itself. Each case is
# (workflow, nodes, must_fail).
_WF = {"3": {"inputs": {"seed": 0, "steps": 20}}, "5": {"inputs": {"width": 512}}}
_OK_NODES = [
    {"type": "prompt", "key": "text", "node_ids": ["3"]},
    {"type": "width", "key": "width", "node_ids": ["5"]},
    {"type": "height", "key": "height", "node_ids": ["5"]},
    {"type": "seed", "key": "seed", "node_ids": ["3"]},
]
SELF_CHECK_CASES = (
    ("valid mapping passes", _WF, _OK_NODES, False),
    ("missing node id caught", _WF,
     _OK_NODES + [{"type": "steps", "key": "steps", "node_ids": ["99"]}], True),
    ("seed without key caught", _WF,
     [n for n in _OK_NODES if n["type"] != "seed"]
     + [{"type": "seed", "node_ids": ["3"]}], True),
    ("unknown type caught", _WF,
     _OK_NODES + [{"type": "sampler", "key": "x", "node_ids": ["3"]}], True),
    ("empty mapping caught", _WF, [], True),
    ("missing prompt node caught", _WF,
     [n for n in _OK_NODES if n["type"] != "prompt"], True),
)


def self_check() -> list[str]:
    """Prove the checker still catches each shape of breakage."""
    problems: list[str] = []
    for name, workflow, nodes, must_fail in SELF_CHECK_CASES:
        failed = bool(check_mapping(workflow, nodes, "selftest"))
        if failed != must_fail:
            verb = "did not flag" if must_fail else "wrongly flagged"
            problems.append(
                f"self-check {name!r}: checker {verb} it -- this gate can no "
                "longer detect the failure it exists for"
            )
    return problems


def main() -> int:
    failures: list[str] = self_check()

    main_vars = yaml.safe_load(MAIN_VARS.read_text(encoding="utf-8"))
    selected = main_vars.get("image_workflow")
    if not selected:
        print("image_workflow is not set in main.yml", file=sys.stderr)
        return 1

    available = sorted(p.stem for p in WORKFLOW_DIR.glob("*.json"))
    if not available:
        print(f"no workflows found in {WORKFLOW_DIR} -- this gate would pass "
              "everything", file=sys.stderr)
        return 1
    if selected not in available:
        failures.append(
            f"image_workflow={selected!r} names no file in {WORKFLOW_DIR} "
            f"(have: {available})"
        )

    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    env = catalog["infra_secret_apps"]["open-webui"]["env"]

    raw_nodes = env.get("COMFYUI_WORKFLOW_NODES")
    if raw_nodes is None:
        failures.append("open-webui env has no COMFYUI_WORKFLOW_NODES")
        nodes = None
    else:
        try:
            nodes = json.loads(raw_nodes)
        except json.JSONDecodeError as exc:
            failures.append(
                f"COMFYUI_WORKFLOW_NODES is not valid JSON ({exc}); Open WebUI "
                "would fall back to [] and configure nothing"
            )
            nodes = None

    # Every workflow is checked, not just the selected one: switching
    # image_workflow must never be the step that discovers a broken mapping.
    if nodes is not None:
        for path in sorted(WORKFLOW_DIR.glob("*.json")):
            try:
                workflow = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                failures.append(f"{path.name}: not valid JSON ({exc})")
                continue
            if "nodes" in workflow or "links" in workflow:
                failures.append(
                    f"{path.name}: looks like ComfyUI's editor format; Open "
                    "WebUI needs the API format (Workflow -> Export (API))"
                )
                continue
            failures.extend(check_mapping(workflow, nodes, path.name))

    if failures:
        print("Open WebUI image configuration validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(
        f"Open WebUI image config: OK ({len(available)} workflows, "
        f"selected {selected!r}, {len(nodes)} mapped nodes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it to verify it fails**

```bash
.venv/bin/python tests/validate_openwebui_image_config.py
```

Expected: FAIL with `image_workflow is not set in main.yml`. If it fails with a `KeyError` or `FileNotFoundError` instead, fix the validator before continuing — the gate must report the real problem, not crash.

- [ ] **Step 3: Add the `image_workflow` variable**

In `inventory/group_vars/all/main.yml`, immediately after `gpu_host_online` (line ~67):

```yaml
# Which ComfyUI workflow Open WebUI drives. Pony is SDXL-architecture and
# Chroma is Flux-architecture (UNET + T5-FLAN encoder + VAE as three separate
# files), and Open WebUI holds exactly one workflow at a time -- so this is a
# choice, not a preference, and switching is a `make infra` rather than a
# per-request dropdown.
#
# Pony is the default because it shares the VRAM profile already proven to
# coexist with a resident chat model. Chroma's fp8 stack is ~13.4 GB and has
# not been shown to fit alongside one; see docs/gpu-host.md. Chroma is always
# reachable directly at http://{{ gpu_host_ip }}:8188 regardless of this.
image_workflow: pony
```

- [ ] **Step 4: Create the Pony workflow**

Create `roles/svc_infra/files/comfyui/pony.json`. Derived from Open WebUI's own `COMFYUI_DEFAULT_WORKFLOW`, with nodes `10` and `11` added for Pony's score tags:

```json
{
  "3": {
    "inputs": {
      "seed": 0,
      "steps": 28,
      "cfg": 7,
      "sampler_name": "euler_ancestral",
      "scheduler": "normal",
      "denoise": 1,
      "model": ["4", 0],
      "positive": ["11", 0],
      "negative": ["7", 0],
      "latent_image": ["5", 0]
    },
    "class_type": "KSampler",
    "_meta": { "title": "KSampler" }
  },
  "4": {
    "inputs": { "ckpt_name": "ponyDiffusionV6XL_v6StartWithThisOne.safetensors" },
    "class_type": "CheckpointLoaderSimple",
    "_meta": { "title": "Load Checkpoint" }
  },
  "5": {
    "inputs": { "width": 1024, "height": 1024, "batch_size": 1 },
    "class_type": "EmptyLatentImage",
    "_meta": { "title": "Empty Latent Image" }
  },
  "6": {
    "inputs": { "text": "Prompt", "clip": ["4", 1] },
    "class_type": "CLIPTextEncode",
    "_meta": { "title": "Positive (user prompt)" }
  },
  "7": {
    "inputs": {
      "text": "score_6, score_5, score_4, worst quality, low quality, jpeg artifacts, watermark, signature, text",
      "clip": ["4", 1]
    },
    "class_type": "CLIPTextEncode",
    "_meta": { "title": "Negative" }
  },
  "8": {
    "inputs": { "samples": ["3", 0], "vae": ["4", 2] },
    "class_type": "VAEDecode",
    "_meta": { "title": "VAE Decode" }
  },
  "9": {
    "inputs": { "filename_prefix": "OpenWebUI", "images": ["8", 0] },
    "class_type": "SaveImage",
    "_meta": { "title": "Save Image" }
  },
  "10": {
    "inputs": { "text": "score_9, score_8_up, score_7_up", "clip": ["4", 1] },
    "class_type": "CLIPTextEncode",
    "_meta": { "title": "Pony score tags (fixed)" }
  },
  "11": {
    "inputs": { "conditioning_to": ["10", 0], "conditioning_from": ["6", 0] },
    "class_type": "ConditioningConcat",
    "_meta": { "title": "Score tags + user prompt" }
  }
}
```

**Why nodes 10 and 11 exist:** Pony V6 XL was trained with `score_9, score_8_up, score_7_up` quality tags and produces visibly worse output without them. Open WebUI overwrites the positive prompt node's text wholesale, so the tags cannot simply be typed into node 6 — they would be erased on every request. Node 10 holds them as fixed text, node 11 concatenates them with the user's prompt, and the mapping in Task 5 points `prompt` at node 6 only.

- [ ] **Step 5: Run the validator again**

```bash
.venv/bin/python tests/validate_openwebui_image_config.py
```

Expected: FAIL with `open-webui env has no COMFYUI_WORKFLOW_NODES`. The workflow and variable are now valid; the env wiring lands in Task 5. This is the correct intermediate state.

- [ ] **Step 6: Wire the validator into `make validate`**

In the `Makefile`, in the `validate-catalog` target, after the `validate_release_overrides.py` line (line 133):

```makefile
# Open WebUI applies its ComfyUI node mapping with a bare dict index and the
# caller swallows the KeyError, so a wrong node id yields no image and no
# error. Nothing else in the repo can see that before it deploys.
	$(PYTHON) tests/validate_openwebui_image_config.py
```

- [ ] **Step 7: Confirm the self-check has a positive control**

Verify the gate can actually fail, rather than passing because it never ran. In a scratch copy — **not** the repo file:

```bash
cp tests/validate_openwebui_image_config.py /tmp/vg.py
python - <<'EOF'
import pathlib
p = pathlib.Path("/tmp/vg.py")
p.write_text(p.read_text().replace(
    'if str(node_id) not in workflow:', 'if False:'))
EOF
python /tmp/vg.py
```

Expected: FAIL, naming the `missing node id caught` self-check case. If it passes, the self-check is not exercising the branch and must be fixed. Then `rm /tmp/vg.py`.

- [ ] **Step 8: Run the full gate suite**

```bash
make validate
```

Expected: PASS. The new validator prints `Open WebUI image config: ...` — except it will still fail on the missing env. Confirm the failure is *only* that, then proceed; Task 5 resolves it. If `make validate` fails for any other reason, fix it here.

- [ ] **Step 9: Commit**

```bash
git add tests/validate_openwebui_image_config.py roles/svc_infra/files/comfyui/pony.json inventory/group_vars/all/main.yml Makefile
git commit -m "feat: add the image workflow gate and the Pony pipeline

Open WebUI applies its ComfyUI node mapping as workflow[id]['inputs'][key]
and the caller catches the resulting KeyError in a broad except, returning
None. A wrong node id therefore produces no image and no error on a green
container. COMFYUI_WORKFLOW_NODES compounds it: config.py parses it with a
bare except JSONDecodeError that falls back to [], so malformed JSON
configures nothing silently.

The gate checks both JSON blobs parse, that image_workflow names a real
file, and that every mapped node id exists in every committed workflow --
not just the selected one, so switching is never the step that discovers a
broken mapping. It carries its own self-check for the same reason the
dashboard gate does; verified by reverting the node-id branch in a scratch
copy and watching exactly that case fail.

pony.json adds two nodes beyond Open WebUI's default workflow. Pony V6 XL
needs score_9/score_8_up/score_7_up quality tags, and Open WebUI overwrites
the positive prompt wholesale -- so the tags live in a fixed node and are
concatenated with the user prompt rather than being erased every request."
```

---

## Task 4: Add the Chroma workflow

**Files:**
- Create: `roles/svc_infra/files/comfyui/chroma.json`

**Interfaces:**
- Consumes: filenames confirmed in Task 2 Step 4; the validator from Task 3.
- Produces: a second workflow using **the same node IDs for the same roles** as `pony.json` (`3` sampler-ish, `5` latent, `6` positive, `7` negative), so one node mapping serves both. This is a hard requirement, not a convenience.

- [ ] **Step 1: Create the Chroma workflow**

Create `roles/svc_infra/files/comfyui/chroma.json`. Derived from the official `ComfyUI_Chroma1-HD_T2I-workflow.json` in the `lodestones/Chroma1-HD` repo, renumbered so its node IDs match `pony.json`'s roles:

```json
{
  "3": {
    "inputs": { "noise_seed": 0 },
    "class_type": "RandomNoise",
    "_meta": { "title": "Noise (seed)" }
  },
  "4": {
    "inputs": {
      "unet_name": "Chroma1-HD-fp8_scaled_defaultloader_hybrid_large_rev2.safetensors",
      "weight_dtype": "default"
    },
    "class_type": "UNETLoader",
    "_meta": { "title": "Load Chroma" }
  },
  "5": {
    "inputs": { "width": 1024, "height": 1024, "batch_size": 1 },
    "class_type": "EmptySD3LatentImage",
    "_meta": { "title": "Empty Latent" }
  },
  "6": {
    "inputs": { "text": "Prompt", "clip": ["12", 0] },
    "class_type": "CLIPTextEncode",
    "_meta": { "title": "Positive (user prompt)" }
  },
  "7": {
    "inputs": { "text": "", "clip": ["12", 0] },
    "class_type": "CLIPTextEncode",
    "_meta": { "title": "Negative" }
  },
  "8": {
    "inputs": { "samples": ["16", 0], "vae": ["13", 0] },
    "class_type": "VAEDecode",
    "_meta": { "title": "VAE Decode" }
  },
  "9": {
    "inputs": { "filename_prefix": "OpenWebUI", "images": ["8", 0] },
    "class_type": "SaveImage",
    "_meta": { "title": "Save Image" }
  },
  "12": {
    "inputs": {
      "clip_name": "t5xxl_flan_fp8_scaled.safetensors",
      "type": "chroma",
      "device": "default"
    },
    "class_type": "CLIPLoader",
    "_meta": { "title": "Load T5-FLAN (chroma)" }
  },
  "13": {
    "inputs": { "vae_name": "ae.safetensors" },
    "class_type": "VAELoader",
    "_meta": { "title": "Load VAE" }
  },
  "14": {
    "inputs": { "shift": 1, "model": ["4", 0] },
    "class_type": "ModelSamplingAuraFlow",
    "_meta": { "title": "Model Sampling" }
  },
  "15": {
    "inputs": { "steps": 26, "alpha": 0.45, "beta": 0.45, "model": ["14", 0] },
    "class_type": "BetaSamplingScheduler",
    "_meta": { "title": "Beta Scheduler" }
  },
  "16": {
    "inputs": {
      "noise": ["3", 0],
      "guider": ["18", 0],
      "sampler": ["17", 0],
      "sigmas": ["15", 0],
      "latent_image": ["5", 0]
    },
    "class_type": "SamplerCustomAdvanced",
    "_meta": { "title": "Sampler" }
  },
  "17": {
    "inputs": { "sampler_name": "euler" },
    "class_type": "KSamplerSelect",
    "_meta": { "title": "Sampler Select" }
  },
  "18": {
    "inputs": {
      "cfg": 3.8,
      "model": ["14", 0],
      "positive": ["6", 0],
      "negative": ["7", 0]
    },
    "class_type": "CFGGuider",
    "_meta": { "title": "CFG Guider" }
  }
}
```

**Three settings here are not guessable and come from the official workflow:**

- **CFG 3.8.** Chroma derives from FLUX.1-schnell but is not distilled the same way, so it needs real classifier-free guidance. A schnell-style CFG of 1.0 gives washed-out output that reads as a bad model rather than a bad setting.
- **`ModelSamplingAuraFlow` shift 1** and **`BetaSamplingScheduler` 26 steps** — Chroma's sampling does not work with a plain `KSampler`.
- **`CLIPLoader` type `chroma`** with the T5-**FLAN** encoder, not the plain `t5xxl_fp8_e4m3fn` used by stock Flux workflows.

Note the node IDs deliberately mirror `pony.json`: `3` carries the seed, `5` the dimensions, `6` the positive prompt, `7` the negative. `steps` lives on node `15` here versus node `3` in Pony — this is the one divergence, and Task 5's mapping lists both IDs for the `steps` type, which is safe because Open WebUI applies a mapping only to node IDs present in the workflow it is running. The validator's cross-check of *every* workflow is what keeps that honest.

- [ ] **Step 2: Run the validator**

```bash
.venv/bin/python tests/validate_openwebui_image_config.py
```

Expected: still FAIL with only `open-webui env has no COMFYUI_WORKFLOW_NODES`. No new failures naming `chroma.json`. A `chroma.json: not valid JSON` or an editor-format complaint means the file above was pasted wrong.

- [ ] **Step 3: Verify the workflow actually runs, before trusting it**

A structurally valid workflow can still be semantically wrong. Post it straight to ComfyUI:

```bash
python -c "
import json,urllib.request
wf=json.load(open('roles/svc_infra/files/comfyui/chroma.json'))
wf['3']['inputs']['noise_seed']=12345
wf['6']['inputs']['text']='a photograph of a red bicycle against a white wall'
req=urllib.request.Request('http://192.168.1.40:8188/prompt',
    data=json.dumps({'prompt':wf}).encode(), headers={'Content-Type':'application/json'})
print(urllib.request.urlopen(req).read().decode())
"
```

Expected: a JSON response containing a `prompt_id` and an empty `node_errors` object. **A populated `node_errors` names the exact node and input that is wrong** — fix it here rather than discovering it through Open WebUI, where the same fault returns nothing at all.

Then confirm an image actually appeared in `C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\output\`. A queued prompt is not a generated image.

- [ ] **Step 4: Repeat for the Pony workflow**

Same check, so both are proven before either is wired up:

```bash
python -c "
import json,urllib.request
wf=json.load(open('roles/svc_infra/files/comfyui/pony.json'))
wf['3']['inputs']['seed']=54321
wf['6']['inputs']['text']='a photograph of a red bicycle against a white wall'
req=urllib.request.Request('http://192.168.1.40:8188/prompt',
    data=json.dumps({'prompt':wf}).encode(), headers={'Content-Type':'application/json'})
print(urllib.request.urlopen(req).read().decode())
"
```

Expected: `prompt_id` present, `node_errors` empty, a new file in the output directory.

- [ ] **Step 5: Commit**

```bash
git add roles/svc_infra/files/comfyui/chroma.json
git commit -m "feat: add the Chroma image workflow

Derived from the official ComfyUI_Chroma1-HD_T2I-workflow.json in the
lodestones/Chroma1-HD repo rather than hand-built, then renumbered so its
node ids match pony.json's roles and one mapping serves both.

Three settings are not guessable and are the reason for deriving rather
than writing from scratch: CFG 3.8 (Chroma is schnell-derived but not
distilled the same way, so schnell's CFG 1.0 gives washed-out output that
reads as a bad model), ModelSamplingAuraFlow shift 1 with a 26-step Beta
scheduler, and CLIPLoader type 'chroma' against the T5-FLAN encoder rather
than the plain t5xxl_fp8_e4m3fn stock Flux workflows use.

The upstream workflow's own filenames are stale and 404, so both loaders
are repointed at the files actually downloaded. Both workflows were posted
directly to ComfyUI and confirmed to return an empty node_errors and write
a real file before either was wired to Open WebUI."
```

---

## Task 5: Wire Open WebUI's image environment and deploy

**Files:**
- Modify: `inventory/group_vars/all/infra-apps.yml` (the `open-webui` entry, lines ~548-582)

**Interfaces:**
- Consumes: `image_workflow` (Task 3), both workflow JSONs (Tasks 3 and 4).
- Produces: a deployed, working in-chat image generation path.

- [ ] **Step 1: Add the image environment to the catalog**

In `inventory/group_vars/all/infra-apps.yml`, replace the three existing image lines in the `open-webui` `env` block:

```yaml
      ENABLE_IMAGE_GENERATION: "{{ 'true' if gpu_host_online | bool else 'false' }}"
      IMAGE_GENERATION_ENGINE: comfyui
      COMFYUI_BASE_URL: "http://{{ gpu_host_ip }}:8188"
```

with:

```yaml
      ENABLE_IMAGE_GENERATION: "{{ 'true' if gpu_host_online | bool else 'false' }}"
      IMAGE_GENERATION_ENGINE: comfyui
      COMFYUI_BASE_URL: "http://{{ gpu_host_ip }}:8188"
      # Upstream defaults IMAGE_SIZE to 512x512. Both checkpoints here are
      # trained at 1024 and degrade badly below it -- and nothing reports the
      # difference, so this was silently producing poor images rather than
      # failing. IMAGE_STEPS defaults to 50, roughly double what either
      # pipeline needs.
      IMAGE_SIZE: "1024x1024"
      IMAGE_STEPS: "28"
      # Selects the checkpoint within the SDXL workflow. Ignored by the Chroma
      # workflow, whose model is named in its UNETLoader.
      IMAGE_GENERATION_MODEL: ponyDiffusionV6XL_v6StartWithThisOne.safetensors
      # One workflow at a time -- Pony is SDXL-architecture and Chroma is Flux,
      # so they cannot share one. See image_workflow in main.yml.
      COMFYUI_WORKFLOW: "{{ lookup('file', playbook_dir + '/roles/svc_infra/files/comfyui/' + image_workflow + '.json') }}"
      # Applied as workflow[node_id]['inputs'][key]. A node id absent from the
      # workflow raises KeyError, which the caller swallows -- no image, no
      # error. tests/validate_openwebui_image_config.py is the only thing that
      # catches that before it deploys.
      #
      # steps lists both 3 (Pony's KSampler) and 15 (Chroma's Beta scheduler);
      # Open WebUI only touches ids present in the workflow it is running.
      COMFYUI_WORKFLOW_NODES: >-
        [{"type": "model", "key": "ckpt_name", "node_ids": ["4"]},
         {"type": "prompt", "key": "text", "node_ids": ["6"]},
         {"type": "negative_prompt", "key": "text", "node_ids": ["7"]},
         {"type": "width", "key": "width", "node_ids": ["5"]},
         {"type": "height", "key": "height", "node_ids": ["5"]},
         {"type": "n", "key": "batch_size", "node_ids": ["5"]},
         {"type": "steps", "key": "steps", "node_ids": ["3", "15"]},
         {"type": "seed", "key": "seed", "node_ids": ["3"]}]
```

**One known wrinkle to check in Step 2, not to pre-solve:** the `model` mapping writes `ckpt_name` into node `4`, which is `CheckpointLoaderSimple` in Pony but `UNETLoader` in Chroma — where the input is `unet_name`. Under the Chroma workflow this writes an unused `ckpt_name` key rather than erroring, so it is harmless; the validator does not flag it because the node ID does exist. Likewise `seed` maps to node `3`'s `seed`, but Chroma's node `3` is `RandomNoise` whose input is `noise_seed`. **If Chroma is ever made the selected workflow, both of these need a second look** — record that in `docs/chat-models.md` in Task 6 rather than fixing it now.

- [ ] **Step 2: Run the validator**

```bash
.venv/bin/python tests/validate_openwebui_image_config.py
```

Expected: PASS, printing `Open WebUI image config: OK (2 workflows, selected 'pony', 8 mapped nodes)`.

- [ ] **Step 3: Run the full gate suite**

```bash
make validate
```

Expected: PASS, all gates.

- [ ] **Step 4: Deploy to svc-infra**

```bash
make infra
```

Expected: `changed` on the open-webui Quadlet and a container restart. A failure at the `lookup('file', ...)` line means `playbook_dir` did not resolve — check the path in the error rather than hardcoding around it.

- [ ] **Step 5: Verify the container actually took the new environment**

```bash
ssh svc-infra 'podman exec open-webui env | grep -E "IMAGE_SIZE|IMAGE_STEPS|IMAGE_GENERATION_MODEL"'
```

Expected: `IMAGE_SIZE=1024x1024`, `IMAGE_STEPS=28`, and the Pony filename. If these are absent the Quadlet did not restart — a green container proves nothing here.

- [ ] **Step 6: Generate an image from inside a conversation**

In `https://chat.fortwow.dev`, open a chat and generate an image. **Use a prompt you have not used before** — ComfyUI caches by workflow hash and returns the previous image in ~2 s, which is indistinguishable from success apart from an identical filename and byte size.

Expected: a real image within roughly 10-20 seconds.

- [ ] **Step 7: Confirm the dimensions**

Download the generated image and check it:

```bash
python -c "
import struct,sys
d=open(sys.argv[1],'rb').read(33)
print('PNG', struct.unpack('>II', d[16:24]))
" <downloaded.png>
```

Expected: `(1024, 1024)`. **This is the `IMAGE_SIZE` fix proving itself** — 512×512 means the environment did not apply.

- [ ] **Step 8: Commit**

```bash
git add inventory/group_vars/all/infra-apps.yml
git commit -m "feat: wire Open WebUI's image pipeline to the committed workflow

Three upstream defaults were silently wrong here. IMAGE_SIZE defaults to
512x512 against checkpoints trained at 1024, so in-chat images had been
quietly poor since the feature was enabled -- with no error anywhere.
IMAGE_STEPS defaults to 50, about double what either pipeline needs. And
the workflow had never been set at all, so generation used Open WebUI's
built-in placeholder.

All of it is set as environment rather than in the admin UI because
ENABLE_PERSISTENT_CONFIG=false makes the environment authoritative on every
container start -- a value clicked into the UI is discarded on the next
restart, the same trap the ENABLE_SIGNUP comment above already documents.

Verified by generating from inside a conversation with an unused prompt
(ComfyUI returns a cached image for a repeated workflow hash in ~2s, which
looks exactly like success) and confirming the result is 1024x1024."
```

---

## Task 6: Create the personas and write `docs/chat-models.md`

**Files:**
- Create: `docs/chat-models.md`

**Interfaces:**
- Consumes: the model tags from Task 1, the image behaviour from Task 5.
- Produces: the reference document; no code depends on it.

- [ ] **Step 1: Create the personas in Open WebUI**

In `https://chat.fortwow.dev` → **Workspace → Models → +**, create each of the following over base model `huihui_ai/gemma-4-abliterated:26b`. A persona is a saved system prompt; it adds no download and no VRAM, and switching between personas on the same base model is instant.

**`Therapist`:**

```
You are a warm, direct, non-judgmental listener with a background in
psychotherapy. Your purpose is to help the person think, not to protect
yourself from their material.

Ask before advising. When someone brings you something heavy, your first
several turns should be questions that help them say more, not solutions.
Reflect what you actually heard rather than paraphrasing it into something
tidier.

Do not deflect to crisis hotlines or professional referrals as a reflex.
That reflex arrives exactly when a conversation gets real, and it reads as
abandonment. Mention outside help only when the person asks for it, or when
there is a concrete, immediate risk to someone's safety -- and if you do,
say it plainly and stay in the conversation afterwards.

Do not moralize, do not add disclaimers about being an AI, and do not end
messages with a summary of what the person should do. Speak like a person
who has time.
```

**`Unfiltered`:**

```
Answer the question that was asked, completely and directly.

Do not add safety caveats, ethical commentary, or reminders about
consulting professionals. Do not hedge a factual answer to make it feel
safer. If a topic is genuinely dangerous, say what is dangerous about it
as part of the answer rather than as a preamble that replaces it.

If you do not know something, say so in one sentence and continue.
```

- [ ] **Step 2: Verify each persona actually applies**

A saved persona that failed to attach looks identical to one that worked. Send the same message to the base model and to `Therapist` and confirm the replies differ in the way the prompt describes.

Expected: the base model may deflect or add disclaimers where `Therapist` does not. If the two are indistinguishable, the system prompt did not attach — re-check the Workspace entry.

- [ ] **Step 3: Write the reference document**

Create `docs/chat-models.md` covering, with no placeholders:

- The model roster table from the spec, with the **exact** tags used in Task 1 including the DavidAU quant tag actually pulled.
- The one-model-at-a-time constraint and the ~20-30 s switching cost.
- The persona text above, verbatim, and this paragraph:

  > **These personas live in Open WebUI's database, not in git.** They are
  > created by hand in Workspace → Models, and `backup_paths: [open-webui]`
  > captures `webui.db` nightly — so they are recoverable, but they are not
  > rebuildable from a clean clone the way everything else in this repo is.
  > This is a deliberate exception, taken because the alternative (a
  > compare-before-write Ansible task against `/api/v1/models`, needed to
  > avoid reporting `changed` on every deploy and destroying the `changed=0`
  > proof) is disproportionate for four paragraphs of text. The copies above
  > are the source of truth for a rebuild; nothing detects drift between them
  > and the live copy. Revisit if the persona set grows.

- How to switch image pipeline: set `image_workflow` in `main.yml`, run `make infra`. Plus the caveat recorded in Task 5 Step 1:

  > Switching to `chroma` needs two mapping fixes first: `model` writes
  > `ckpt_name` to node 4, which is Chroma's `UNETLoader` where the input is
  > `unet_name`; and `seed` writes `seed` to node 3, which is Chroma's
  > `RandomNoise` where the input is `noise_seed`. Both node IDs exist, so
  > the validator passes and neither raises — the values are simply ignored,
  > and generation runs with a fixed seed and whatever model the workflow
  > file names. Fix the mapping in the same commit that flips the variable.

- That Chroma is always reachable directly at `http://192.168.1.40:8188`.

- [ ] **Step 4: Check the links gate**

```bash
.venv/bin/python tests/validate_links.py
```

Expected: PASS. This repo has a link validator; a new doc with a broken relative link fails it.

- [ ] **Step 5: Run the full gate suite**

```bash
make validate
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docs/chat-models.md
git commit -m "docs: record the model roster and the persona text

Therapy is a system prompt over the default chat model, not a fourth
download -- there is no local therapy model worth pulling, and what makes
one work is a persona plus long context, which Open WebUI already has. The
uncensored base matters for a specific reason: an aligned model breaks
character and emits hotline boilerplate exactly when a conversation gets
heavy, which is when it is least useful.

States plainly that these personas live in webui.db rather than git, why
that exception was taken, and that nothing detects drift between the live
copy and the text recorded here.

Also records the two node-mapping fixes that switching image_workflow to
chroma needs first. Both node ids exist so the gate passes and nothing
raises -- the values are just ignored, which would run generation with a
fixed seed and no obvious symptom."
```

---

## Task 7: Update `docs/gpu-host.md` and the Continue config

**Files:**
- Modify: `docs/gpu-host.md`

**Interfaces:**
- Consumes: recorded values from Task 1 (tags, VRAM) and Task 2 (filenames).

- [ ] **Step 1: Replace the model pull section**

In `docs/gpu-host.md`, replace the `ollama pull` block (lines ~53-69) with the six models from Task 1, including the `hf.co/` prefix explanation for the DavidAU model and the note that its quant tag must be confirmed against the repo's file listing first. Update the total download size to the real figure.

- [ ] **Step 2: Add the image model section**

After the ComfyUI installation section (after line ~117), add the four-file table, the `Invoke-WebRequest` commands, the SHA256 verification block from Task 2 Step 3, and the warning that `black-forest-labs/FLUX.1-schnell` is gated and returns 401. Note that `text_encoders/` may need creating on a portable build.

- [ ] **Step 3: Update the Continue config**

Replace the `qwen2.5-coder-14b` entry in the `~/.continue/config.yaml` block (lines ~186-206) with:

```yaml
  - name: qwen3-coder-30b
    provider: ollama
    model: qwen3-coder:30b
    apiBase: http://192.168.1.40:11434
    roles: [chat, edit, apply]
```

Leave the `1.5b-base` autocomplete and `nomic-embed-text` embedding entries unchanged.

- [ ] **Step 4: Apply the Continue config and test it in a real file**

Update `~/.continue/config.yaml` on the workstation, then trigger autocomplete in an actual source file.

Expected: a real completion. **The model list populates from `/api/tags` even when generation is broken**, so a populated dropdown proves nothing — the existing doc already says this and it still applies.

- [ ] **Step 5: Update the VRAM table**

Replace the measured table (lines ~151-160) with the figure recorded in Task 1 Step 6 for `gemma-4-abliterated:26b`, keeping the existing format. Leave the Chroma row for Task 8.

- [ ] **Step 6: Run the gates**

```bash
make validate
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add docs/gpu-host.md
git commit -m "docs: bring gpu-host.md up to the new roster

Records the six models actually pulled, including that the DavidAU one is a
Hugging Face GGUF repo rather than an Ollama registry entry -- ollama pull
without the hf.co/ prefix fails with a not-found error that reads as though
the model was withdrawn, and its quant tag has to be confirmed against the
repo listing because DavidAU publishes many per model.

Adds the four image files with SHA256 checks. Hugging Face returns each
file's checksum as its CDN etag, so the expected value is knowable in
advance -- a strictly stronger check than this document's existing 'if it
lands as a few KB it is an HTML error page', which catches an obviously
tiny file but not a truncated or substituted one. Also records that
black-forest-labs/FLUX.1-schnell is gated and returns 401, since that is
exactly the download that produces the HTML-page failure.

Continue moves to qwen3-coder:30b, verified by triggering autocomplete in a
real file rather than trusting the model list, which populates from
/api/tags even when generation is broken."
```

---

## Task 8: Measure whether Chroma fits alongside a resident chat model

The spec flags this as unverified. It is a measurement task whose deliverable is a recorded number, and it may legitimately conclude "no".

**Files:**
- Modify: `docs/gpu-host.md` (VRAM table)

- [ ] **Step 1: Load the default chat model and record baseline VRAM**

```powershell
ollama run huihui_ai/gemma-4-abliterated:26b "hello"
ollama ps
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

Record both figures.

- [ ] **Step 2: Generate with Chroma while the chat model stays resident**

With the chat model still loaded, post the Chroma workflow directly (a fresh seed each time):

```bash
python -c "
import json,urllib.request,random
wf=json.load(open('roles/svc_infra/files/comfyui/chroma.json'))
wf['3']['inputs']['noise_seed']=random.randint(1,10**9)
wf['6']['inputs']['text']='a lighthouse in heavy fog, photographic'
req=urllib.request.Request('http://192.168.1.40:8188/prompt',
    data=json.dumps({'prompt':wf}).encode(), headers={'Content-Type':'application/json'})
print(urllib.request.urlopen(req).read().decode())
"
```

While it runs, on the GPU host:

```powershell
nvidia-smi --query-gpu=memory.used --format=csv -l 1
```

- [ ] **Step 3: Record the outcome, whichever way it goes**

Three possible results, all valid:

1. **It completes and the chat model is still resident** (`ollama ps` still lists it) — Chroma can be the in-chat engine. Record the peak VRAM.
2. **It completes but evicted the chat model** — usable, but every image costs a chat reload. Record that.
3. **It OOMs** — Chroma stays direct-only. `ollama stop huihui_ai/gemma-4-abliterated:26b` frees the card.

- [ ] **Step 4: Add the measurement to the VRAM table**

Extend the table in `docs/gpu-host.md` with the rows measured, in the existing format. State the date, as the existing table does.

- [ ] **Step 5: Commit**

```bash
git add docs/gpu-host.md
git commit -m "docs: measure Chroma against a resident chat model

The existing table's finding -- that ComfyUI pages against system RAM
rather than demanding the whole card -- was measured with a 6.5 GB SDXL
checkpoint. Chroma's fp8 stack is roughly 13.4 GB, and extrapolating from
one to the other is the kind of assumption this document warns against, so
it is measured rather than assumed."
```

---

## Task 9: Final deploy from a clean tree, verify, and merge

**Files:** none

- [ ] **Step 1: Confirm the tree is clean**

```bash
git status --porcelain
```

Expected: **no output**. Untracked files count. If anything is listed, commit or remove it before continuing — the guarantee in the next steps depends on this.

- [ ] **Step 2: Run the full gate suite one more time**

```bash
make validate
```

Expected: PASS.

- [ ] **Step 3: Deploy from the clean tree**

```bash
make infra
```

Expected: `changed=3` on svc-infra — the nightly runner's `git archive` at `/opt/homelab-iac` still names the previous revision in `.deployed-rev`, so the sync block rebuilds the archive, unpacks it, and records the new revision. **Check which three tasks changed.** Anything beyond those three is a real diff and must be explained before merging.

- [ ] **Step 4: Deploy again and require `changed=0`**

```bash
make infra
```

Expected: `changed=0`. This is the proof that what is running equals what is committed. Anything else has to be explained, not papered over.

- [ ] **Step 5: Run the verification playbook**

```bash
make verify
```

Expected: PASS.

- [ ] **Step 6: End-to-end check through the real service**

Not a container check. In `https://chat.fortwow.dev`:

1. Send a message to `huihui_ai/gemma-4-abliterated:26b` and get a real reply.
2. Switch to the `Therapist` persona and confirm it behaves per its prompt.
3. Generate an image with a prompt not used before, and confirm it is 1024×1024.
4. Re-run the Task 1 Step 5 refusal prompt through the web UI, not just the API — confirm it is answered.

All four must pass. Item 4 matters most: it is the only one that distinguishes a working roster from a plausible-looking wrong one.

- [ ] **Step 7: Merge, push, and delete the branch**

```bash
git switch main
git merge --ff-only feat/uncensored-models
git push origin main
git branch -d feat/uncensored-models
git push origin --delete feat/uncensored-models
```

- [ ] **Step 8: Confirm CI**

CI runs on push to `main`, after the merge rather than before it, so it is an alarm and not a gate. Check the run:

```bash
gh run list --limit 1
```

Expected: green. A red run means something already on `main` is broken and needs a follow-up commit — most likely `systemd-analyze verify`, which only CI can perform.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| Chat roster (4 models) | 1 |
| Coding roster (2 models) | 1, 7 |
| Retirements | 1 Step 7 |
| DavidAU `hf.co/` prefix | 1 Step 3 |
| Disk check | 1 Step 1 |
| Image file downloads + checksums | 2 |
| Gated FLUX.1-schnell avoidance | 2 Step 2, 7 Step 2 |
| `text_encoders/` not `clip/` | 2 Step 1 |
| `image_workflow` variable | 3 Step 3 |
| `pony.json` | 3 Step 4 |
| `chroma.json` from official workflow | 4 Step 1 |
| Stale upstream filenames | 4 Step 1 |
| CFG 3.8 and sampling settings | 4 Step 1 |
| Validator + self-check | 3 Steps 1-2, 6-7 |
| `IMAGE_SIZE` / `IMAGE_STEPS` fixes | 5 Step 1, verified 5 Step 7 |
| `COMFYUI_WORKFLOW` / `_NODES` | 5 Step 1 |
| Personas | 6 Steps 1-2 |
| Persona git exception documented | 6 Step 3 |
| Positive control (uncensored) | 1 Step 5, 9 Step 6 |
| Fresh-seed image check | 5 Step 6, 8 Step 2 |
| Chroma VRAM measurement | 8 |
| Continue update | 7 Steps 3-4 |
| `changed=0` rollout | 9 |
| Exposure note | spec only — no action, correctly |

**Placeholder scan:** `<REFUSAL_PROMPT>` in Task 1 Step 5 and `<downloaded.png>` in Task 5 Step 7 are intentional user-supplied values, both with the surrounding procedure fully specified. Every other step carries literal content.

**Type consistency:** node IDs `3`/`5`/`6`/`7` carry the same roles in both workflows; `steps` diverges to node `15` in Chroma and the mapping in Task 5 lists both, which Task 4 Step 1 explains and Task 6 Step 3 records the residual caveat for. `check_mapping(workflow, nodes, label)` is defined once in Task 3 and called from `self_check()` and `main()` with that signature.
