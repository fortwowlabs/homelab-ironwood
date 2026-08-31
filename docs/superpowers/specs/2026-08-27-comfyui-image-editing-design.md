# In-chat image editing (Qwen Image Edit), designed

**Date:** 2026-08-27
**Status:** designed, not implemented. Supersedes the "not investigated"
section of [docs/plans/image-editing.md](../../plans/image-editing.md), whose
four open questions this doc answers by reading upstream source and querying
public registries rather than by inference.

**Builds on:**
[2026-08-14-comfyui-image-generation-design.md](2026-08-14-comfyui-image-generation-design.md)
("the generation design"), which shipped Pony Diffusion V6 XL generation
2026-08-20 and predicted editing would be cheap because it shares
`ImagesConfig`. That prediction is correct on the mechanism and wrong on
scope: the validator's cross-workflow consistency check does not generalize
to a second, structurally different workflow without a real code change — see
*The gap the generation design didn't see* below.

## 1. The mechanism, verified against upstream source at `main` (fetched 2026-08-27)

Read in full: `backend/open_webui/utils/images/comfyui.py` (255 lines) and the
edit path in `backend/open_webui/routers/images.py` (lines 842–1191).

**Confirmed:** `ComfyUIEditImageForm` and `comfyui_edit_image` are a parallel
construction to `ComfyUICreateImageForm`/`comfyui_create_image`, both driving
the same `_apply_workflow_nodes(workflow, nodes, model, payload)` and the same
`_ws_get_images` WebSocket completion path. No new mechanism. The generation
design's central claim holds.

**Three things the generation design did not anticipate, because nothing in
it needed to look at the edit-specific code:**

- **The source image goes through ComfyUI's own upload endpoint first.**
  `image_edits()` calls `comfyui_upload_image()` (`POST /api/upload/image`,
  `type: input`) for each submitted image before building
  `ComfyUIEditImageForm`, and passes the *filename ComfyUI returns* as
  `payload.image`. The `image`-type mapping entry therefore targets a
  `LoadImage` node's `image` input — a plain filename string, not a data URL —
  and `LoadImage` must be added to the validator's `CLASS_INPUTS`.
- **`ComfyUIEditImageForm` has no `negative_prompt` field at all** — not
  optional, absent. `EditImageForm` (the public API schema) accepts one, but
  `image_edits()`'s `comfyui` branch never puts it in the `data` dict handed
  to `ComfyUIEditImageForm(**data)`. A `negative_prompt`-type entry in the edit
  mapping would hit `_apply_workflow_nodes`'s `payload.negative_prompt` and
  raise `AttributeError`, caught by the same broad `except Exception` that
  swallows the `image`-in-generation mistake the existing validator already
  guards against — silently, no image, no error. **The edit mapping must
  forbid `negative_prompt`,** the mirror image of forbidding `image` in
  generation. A negative prompt for editing has to be a fixed value baked into
  the workflow file, the same technique already used for Pony's score tags.
- **`width`/`height`/`n` reach ComfyUI only if set.** `image_edits()` builds
  `data` with `**({'width': width} if width is not None else {})` — `width`
  stays `None` unless `IMAGE_EDIT_SIZE` (or the per-request `size`) is
  explicitly configured. Left unset, `ComfyUIEditImageForm.width` is `None`
  and a `width`-type mapping entry would write `None` into the target node.
  **Do not map `width`/`height` for editing** unless `IMAGE_EDIT_SIZE` is
  deliberately set — the official workflow (below) derives the output size
  from the input image instead, which is the semantically correct behavior
  for an edit.

## 2. The checkpoint, verified against the HuggingFace API (not the model card)

`Qwen/Qwen-Image-Edit` — Apache-2.0, ungated, `pipeline_tag: image-to-image`,
a 20B-parameter MMDiT model from the Qwen team, confirmed via
`GET https://huggingface.co/api/models/Qwen/Qwen-Image-Edit`.

ComfyUI-ready split files are repackaged first-party by Comfy-Org (not a
third-party mirror the way Pony's CivitAI-only distribution forced) in two
repos, both Apache-2.0:

| File | Repo | Bytes | SHA256 |
|---|---|---|---|
| `qwen_image_edit_fp8_e4m3fn.safetensors` | `Comfy-Org/Qwen-Image-Edit_ComfyUI`, `split_files/diffusion_models/` | 20430635136 | `393c6743d1de2e9031b5197027b36116f2096958ccc0223526d34e1860266021` |
| `qwen_2.5_vl_7b_fp8_scaled.safetensors` | `Comfy-Org/Qwen-Image_ComfyUI`, `split_files/text_encoders/` | 9384670680 | `cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4` |
| `qwen_image_vae.safetensors` | `Comfy-Org/Qwen-Image_ComfyUI`, `split_files/vae/` | 253806246 | `a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f` |

These SHA256 values come from `lfs.sha256` in each repo's
`?blobs=true` API response — the real hash, not the Xet `etag` the generation
design's checksum trap warns about.

**Why this tier and not another.** `Comfy-Org/Qwen-Image-Edit_ComfyUI` also
ships `qwen_image_edit_2509_*` and `qwen_image_edit_2511_*` (newer training
runs, multi-image and ControlNet support per their template descriptions) at
the same fp8 size, and `bf16` variants at 40.86 GB — too big for this card
under any plan. `fp8_e4m3fn` (original release) is chosen because it is the
pairing ComfyUI's own stock template (`image_qwen_image_edit`, §3) ships with
by default — the most conservative, most-tested combination available, and
the newer dated releases remain available as a follow-up once this baseline
is proven working. This is a judgment call, not a measurement; revisit if the
2509/2511 releases turn out to matter for editing quality.

## 3. The workflow: ComfyUI's official template, converted like H3 was

`Comfy-Org/workflow_templates` ships `templates/image_qwen_image_edit.json` —
confirmed via the GitHub API and fetched directly. Same shape problem as the
Z-Image/Flux templates already handled on this host
(`docs/gpu-host.md#the-saved-workflows`): the top level holds only a handful
of UI nodes: the real graph lives in `definitions.subgraphs[0]`, one subgraph
named `Qwen-Image-Edit`.

Nodes in that subgraph, read directly from the fetched JSON:

| id | class_type | role |
|---|---|---|
| 37 | `UNETLoader` | diffusion model — widget default `qwen_image_edit_fp8_e4m3fn.safetensors` |
| 38 | `CLIPLoader` | text encoder — widget default `qwen_2.5_vl_7b_fp8_scaled.safetensors`, type `qwen_image` |
| 39 | `VAELoader` | widget default `qwen_image_vae.safetensors` |
| 78 (top-level) | `LoadImage` | the edited-in image; the `image`-type mapping target |
| 93 (top-level) | `ImageScaleToTotalPixels` | resizes the input before editing — this is *why* width/height mapping is unnecessary |
| 76 | `TextEncodeQwenImageEdit` | positive — inputs `clip, vae, image, prompt`; widget carries the template's demo prompt |
| 77 | `TextEncodeQwenImageEdit` | negative — same inputs, widget is an empty string |
| 66 | `ModelSamplingAuraFlow` | model-shaping node between the loader and the sampler |
| 75 | `CFGNorm` | CFG normalization, sits alongside `ModelSamplingAuraFlow` |
| 3 | `KSampler` | template defaults: `seed=344147753686358 (randomize), steps=4, cfg=1, sampler=euler, scheduler=simple` |
| 8 | `VAEDecode` | |
| 60 (top-level) | `SaveImage` | the output node the existing `OUTPUT_CLASSES` rule already checks for |
| 89 | `LoraLoaderModelOnly` | optional 4-step "Lightning" turbo LoRA — **not carried into our workflow, see below** |

**The Lightning LoRA is deliberately dropped, not wired in behind a switch.**
The template's `steps=4, cfg=1` KSampler defaults are tuned for that LoRA.
Sourcing it would mean a second unverified checkpoint from a third-party
mirror (`Osrivers/Qwen-Image-Edit-Lightning-4steps-V1.0.safetensors` is the
only match for the exact template filename, and it is not from Comfy-Org or
Qwen) — the same class of provenance question the generation design accepted
once, for Pony, and does not need to accept a second time when an
authoritative alternative exists. `Qwen/Qwen-Image-Edit`'s own model card
gives real inference defaults for the un-accelerated model:

```python
inputs = {
    "true_cfg_scale": 4.0,
    "negative_prompt": " ",
    "num_inference_steps": 50,
}
```

So the committed workflow uses `steps=50`, `cfg` set from `true_cfg_scale`
(the `CFGNorm` node exists specifically to make ComfyUI's CFG application
match diffusers' `true_cfg_scale` semantics — verify this empirically in Task
2 of the implementation plan rather than trusting the mapping blind), and the
negative `TextEncodeQwenImageEdit` node's prompt hardcoded to `" "`. This
follows Qwen's own documented defaults instead of a value inferred from a
template tuned for a LoRA we are not using — the discipline this repo already
applies to Pony's score tags and to not guessing tags for
`image_generation_model`.

**The graph is flattened to API format by hand** (node-id → `{class_type,
inputs}`), the same conversion already done once for the H3 templates
(`docs/gpu-host.md`: "submitting API-format graphs derived from the official
templates directly to `/prompt`, because the templates ship in editor format
... and `/prompt` cannot accept [a subgraph] as-is"). The `LoraLoaderModelOnly`
node and its `ComfySwitchNode`/`Primitive*` UI plumbing are omitted entirely
rather than reproduced switched-off, because we are not driving this through
ComfyUI's own UI — there is no switch to preserve.

## 4. The gap the generation design didn't see

`tests/validate_openwebui_image_config.py`'s `check_config()` loads **every**
workflow in `inventory/comfyui-workflows/` via `load_all()` and checks the
*generation* mapping (`image_workflow_nodes`) against **all of them**,
deliberately — so that switching `image_workflow` between `sdxl` and `pony`
can never be the step that discovers a broken mapping. That rule assumes
every committed workflow is a member of the same family, sharing the mapping's
node IDs by convention (`sdxl.json` and `pony.json` both use node `"4"` for
the checkpoint, `"5"` for the latent size, etc., stated explicitly in the
generation design's Rollout §Step 2).

**A Qwen Image Edit workflow is not a member of that family.** It has no node
serving `width`/`height` in the mapped sense, no node at whatever ID the
generation mapping expects for `model` (`"4"`), and an entirely different
graph shape. Dropping `qwen-image-edit.json` into
`inventory/comfyui-workflows/` alongside `sdxl.json`/`pony.json` would make
the *existing* generation validator fail — correctly, by its own logic, on a
workflow it was never meant to check — or, worse, if the IDs happened to
collide by accident, pass while checking nothing meaningful.

**Fix: a second, sibling directory**, `inventory/comfyui-edit-workflows/`,
holding only edit-family workflows, with its own mapping key
(`image_edit_workflow_nodes` in `images.yml`) checked only against files in
that directory. `check_config()` gains a second pass structurally identical
to the first — same rules (API format, output node, explicit `key` on
`model`/`seed`/`image`, node IDs present in every file *of that family*,
class/key agreement via `CLASS_INPUTS`) — with two differences in the rule
set itself:

- `image` is in `EDIT_REQUIRED_TYPES` and **forbidden** from
  `image_workflow_nodes` (already true today); `negative_prompt` is
  **forbidden** from the edit mapping for the reason in §1, where today it is
  merely optional-and-safe for generation.
- `width`/`height` are not required for the edit family (§1); `EDIT_REQUIRED_TYPES
  = {model, prompt, image, steps, seed}`.

`CLASS_INPUTS` gains entries for every class_type the edit workflow
introduces that the generation validator has never had to know about:
`UNETLoader`, `CLIPLoader`, `VAELoader`, `LoadImage`,
`TextEncodeQwenImageEdit`, `ModelSamplingAuraFlow`, `CFGNorm`,
`ImageScaleToTotalPixels`. The exact input names for the less common ones
(`TextEncodeQwenImageEdit`, `CFGNorm`, `ModelSamplingAuraFlow`) are read from
the template JSON's `inputs`/`widgets_values` in §3 above, but per this file's
own existing comment ("ComfyUI's `/object_info` is the live source of truth,
but this gate runs with no network"), **they must be confirmed against the
live host's `/object_info` before being trusted**, not taken from the
template alone — the implementation plan's Task 2 does this before Task 3
writes the workflow file.

## 5. VRAM: comparable to Flux by file size, genuinely unmeasured

Per-file sizes: diffusion model 19.03 GiB, text encoder 8.74 GiB, VAE 0.24
GiB. The single largest file (19.03 GiB) is *smaller* than
`flux1-dev.safetensors` (22.17 GiB), which measured 23564 MiB peak / 1000 MiB
headroom on this exact card sequenced the same way ComfyUI already sequences
multi-file models (`docs/gpu-host.md#measured-2026-08-27-all-three-run-and-flux-is-the-tight-one`).
That comparison makes headroom *plausible*, not proven — this repo's own rule
is "budget from the resident figure... never from the tag"
(`docs/gpu-capacity.md`), and nothing has loaded this model yet. Two
differences from Flux that could push the real number either way: `CLIPLoader`
here is a vision-*language* model that also has to process the input image's
tokens (not just encode text, the way Flux's T5 does), and `TextEncodeQwenImageEdit`
runs twice (positive and negative) rather than once.

**What is not in question: this cannot coexist with a resident chat model.**
Unlike Pony/SDXL (6–7 GB, proven to coexist with a 17–21 GB chat model
resident, `docs/gpu-host.md#sharing-the-card-with-image-generation`), a ~28 GB
on-disk stack whose single largest file alone is 19 GB puts editing in the
same bucket as Chroma1-HD and MiniMax H3: **the chat model must be stopped
first.** This is a UX/operational fact worth stating plainly before shipping:
editing an image mid-chat-conversation will, at minimum, evict whatever chat
model was resident, the same tradeoff already accepted for video generation.

## 6. Catalog shape

Mirrors the generation catalog's managed-keys pattern
(`scripts/owui_image_config.py:managed_keys()`), on the `images.edit.*`
config subtree instead of `images.generation.*`:

| Open WebUI field | Source |
|---|---|
| `ENABLE_IMAGE_EDIT` | `image_edit_enabled` (duplicates `gpu_host_online`, same rule as generation) |
| `IMAGE_EDIT_ENGINE` | hardcoded `"comfyui"` |
| `IMAGE_EDIT_MODEL` | `image_edit_model` — the `UNETLoader` filename |
| `IMAGES_EDIT_COMFYUI_BASE_URL` | duplicates `gpu_host_ip`, same rule as generation |
| `IMAGES_EDIT_COMFYUI_WORKFLOW` | the new workflow file, JSON-encoded |
| `IMAGES_EDIT_COMFYUI_WORKFLOW_NODES` | `image_edit_workflow_nodes`, passed through as list[dict] like generation's mapping |

`IMAGE_EDIT_SIZE` is deliberately left unset (empty string, the upstream
default) — per §1, setting it would turn on width/height forwarding that this
workflow's mapping does not use.

## 7. What this changes about "cheap"

The generation design's closing claim — "the push tool, the validator's
structure and the check's shape all carry over unchanged" — is right about
the push tool (`owui_image_config.py` gains keys, not new logic) and right in
spirit about the validator and check *shape*, but wrong that nothing new is
written. The validator needs a real second pass (§4), not just more managed
keys, and the runtime check needs a different positive-proof strategy: the
generation check's strongest assertion is "the image came back at exactly the
requested dimensions, which the compiled-in default workflow would not
produce" — editing has no equivalent fixed-dimension target (§1: dimensions
come from the input image via `ImageScaleToTotalPixels`), so
`image_edit_check.py` instead reads back ComfyUI's `/history` for the executed
prompt and asserts the `UNETLoader` node's `unet_name` matches
`IMAGE_EDIT_MODEL` — the "available strengthening" the generation design
suggested as a secondary check for generation becomes the *primary* proof for
editing.
