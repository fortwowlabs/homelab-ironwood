# Uncensored image generation — deferred

**Status: not implemented.** Split out of the uncensored model roster work on
2026-08-09 to keep that change to models only. Everything below was researched
and verified as part of that design; nothing here has been deployed.

The model roster it was split from shipped as
`docs/superpowers/specs/2026-08-09-uncensored-model-roster-design.md`.

## There is a live defect here, independent of any new checkpoint

**Open WebUI's `IMAGE_SIZE` defaults to `512x512`, and this deployment does not
set it.** The current checkpoint is stock SDXL, which is trained at 1024 and
degrades badly below it. In-chat image generation has therefore been quietly
producing poor output since the feature was enabled, and it surfaces as no
error anywhere — the container is green, the request succeeds, the image is
just bad.

`IMAGE_STEPS` has the same shape of problem in the other direction: it defaults
to `50`, roughly double what SDXL needs, so every generation costs about twice
the time it should.

Both are two lines in the `open-webui` env in
`inventory/group_vars/all/infra-apps.yml`, need no new models, and would
improve the existing pipeline immediately:

```yaml
      IMAGE_SIZE: "1024x1024"
      IMAGE_STEPS: "28"
```

That is worth doing whether or not the rest of this page ever happens.

## The constraint that shapes everything else

Open WebUI holds exactly one ComfyUI workflow at a time (`COMFYUI_WORKFLOW`).
Its per-request model dropdown maps to a checkpoint-name input *within* that
one workflow, so it can only switch between models of the same architecture.

- **Pony Diffusion V6 XL** is SDXL-architecture: one checkpoint file,
  `CheckpointLoaderSimple`.
- **Chroma1-HD** is Flux-architecture (8.9B, retrained from FLUX.1-schnell with
  safety alignment removed): three files across three directories,
  `UNETLoader` + `CLIPLoader` + `VAELoader`. Its text encoder is a **T5-FLAN**
  variant loaded with `CLIPLoader` type `chroma`, *not* the plain
  `t5xxl_fp8_e4m3fn` used by stock Flux workflows, and it belongs in
  `models/text_encoders/`, not the legacy `models/clip/`.

**They cannot share a workflow.** Both in one chat dropdown is not achievable.

## The design that was agreed

**Pony V6 XL as the in-chat engine** — ~6.5 GB, the same VRAM profile as the
SDXL base already proven to coexist with a resident chat model, and it works
with Open WebUI's built-in default workflow shape.

**Chroma1-HD driven directly from ComfyUI** at `http://192.168.1.40:8188` for
quality work, with no Open WebUI involvement.

**An `image_workflow: pony | chroma` variable** in `main.yml` selecting between
two committed workflow JSONs, so switching is a `make infra` and the decision
lives in git.

## Files, verified public and ungated on 2026-08-09

| Destination | File | Size |
|---|---|---|
| `models/checkpoints/` | [`ponyDiffusionV6XL_v6StartWithThisOne.safetensors`](https://huggingface.co/LyliaEngine/Pony_Diffusion_V6_XL/resolve/main/ponyDiffusionV6XL_v6StartWithThisOne.safetensors) | 6.46 GB |
| `models/diffusion_models/` | [`Chroma1-HD-fp8_scaled_defaultloader_hybrid_large_rev2.safetensors`](https://huggingface.co/silveroxides/Chroma1-HD-fp8-scaled/resolve/main/Chroma1-HD-fp8_scaled_defaultloader_hybrid_large_rev2.safetensors) | 8.56 GB |
| `models/text_encoders/` | [`t5xxl_flan_fp8_scaled.safetensors`](https://huggingface.co/silveroxides/t5xxl_flan_enc/resolve/main/t5xxl_flan_fp8_scaled.safetensors) | 4.80 GB |
| `models/vae/` | [`ae.safetensors`](https://huggingface.co/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors) | 0.31 GB |

SHA256, read from Hugging Face's CDN etag so the expected value is knowable in
advance — verify with `Get-FileHash -Algorithm SHA256` before wiring anything:

| File | SHA256 |
|---|---|
| `ponyDiffusionV6XL_v6StartWithThisOne` | `614f55e8bd8701b9168957361a00c7a76c5de1aa625ade08edfca3db2675b2cc` |
| `Chroma1-HD-fp8_scaled_…_rev2` | `377eff193fc866064ed587bd4140b3fd59bad0555b32b02224d60353b3049ebc` |
| `t5xxl_flan_fp8_scaled` | `e9b22d1142585f501864671e07af481f8800415296f6f54c10a88e71e05a7a60` |
| `ae` | `f73eecf7c469ff442523dc712cc161d631df071bf4d9d793494fbf00cdd80a82` |

**Do not use `black-forest-labs/FLUX.1-schnell` for the VAE. It is gated and
returns 401**, which is exactly the download that lands as an HTML error page
wearing a `.safetensors` name — the failure `docs/gpu-host.md` already warns
about. The Comfy-Org repackage above is the ungated equivalent.

Sizes were re-checked against the live URLs; the plain-`.safetensors` variants
in the same repos are not interchangeable, so use these exact filenames.

## Things that are not guessable

Recording these is most of the value of this page. Each one was found by
reading source or upstream artifacts rather than documentation, and each fails
in a way that looks like something else.

**Chroma needs CFG 3.8.** It derives from FLUX.1-schnell but is not distilled
the same way, so it needs real classifier-free guidance. A schnell-style CFG of
1.0 produces washed-out output that reads as a bad model rather than a bad
setting. Its official workflow also uses `ModelSamplingAuraFlow` shift 1 and a
26-step `BetaSamplingScheduler` with `euler` — a plain `KSampler` does not work.

**The official Chroma workflow ships in the model repo** as
`ComfyUI_Chroma1-HD_T2I-workflow.json` in
[`lodestones/Chroma1-HD`](https://huggingface.co/lodestones/Chroma1-HD)
(ungated, Apache-2.0). Derive from it rather than hand-building. **But its
filenames are stale** — it references `Chroma1-HD-fp8_scaled_rev2.safetensors`
and `t5xxl_flan_latest_float8_e4m3fn_scaled_stochastic.safetensors`, neither of
which exists upstream any more (both 404 as of 2026-08-09). Unedited it loads
fine and fails at generation.

**Pony V6 XL needs its score tags.** It was trained with
`score_9, score_8_up, score_7_up` and produces visibly worse output without
them. Open WebUI overwrites the positive prompt node's text wholesale, so the
tags cannot simply be typed into that node — they are erased on every request.
The workaround is a fixed `CLIPTextEncode` holding the tags plus a
`ConditioningConcat` merging it with the user's prompt node, with the `prompt`
mapping pointed at the user node only.

**Open WebUI's node mapping fails silently.**
`backend/open_webui/utils/images/comfyui.py` applies it as
`workflow[node_id]["inputs"][key] = value`. A node ID absent from the workflow
raises `KeyError`, and `comfyui_create_image` swallows it in a broad
`except Exception` and returns `None`: **no image, no error message, green
container.** `COMFYUI_WORKFLOW_NODES` compounds it — `config.py` parses it with
a bare `except json.JSONDecodeError` falling back to `[]`, so malformed JSON
configures nothing and reports nothing.

The `seed`, `model` and `image` node types read `node.key` with **no fallback**,
so an absent `key` writes to `inputs[None]` and the value never reaches the
sampler. `prompt`, `width`, `height`, `steps` and `n` do have fallbacks.

**Everything must be set as environment, not in the admin UI.**
`ENABLE_PERSISTENT_CONFIG: "false"` makes the environment authoritative on
every container start, so image config clicked into the admin UI is silently
discarded on the next restart — the same trap the `ENABLE_SIGNUP` comment in
`infra-apps.yml` already documents.

**Both workflow files must be ComfyUI's API format** (`Workflow → Export
(API)`), not the editor format. Open WebUI cannot read the editor format and
the two look similar enough to confuse.

## The validator this needs

`tests/validate_openwebui_image_config.py`, wired into `validate-catalog`.
Given the silent-failure behaviour above, it is the only mechanism that can
distinguish a typo from a working configuration before it reaches the host. It
should assert:

- Both the workflow JSON and the nodes JSON parse.
- `image_workflow` names a file that exists.
- **Every node ID in the mapping exists in *every* committed workflow**, not
  just the selected one — so switching the variable is never the step that
  discovers a broken mapping.
- Node types are ones Open WebUI actually handles; an unknown type is ignored
  silently, which is the same failure as a bad ID.
- `seed`, `model` and `image` nodes carry an explicit `key`.
- The file is not in ComfyUI's editor format.

It needs its own self-check — a case table proving it still catches each shape
of breakage — for the same reason `tests/validate_grafana_dashboards.py:112`
has one: a gate against silent failure is not allowed to fail silently itself.

## Two open risks

**Chroma alongside a resident chat model looks impossible, not merely
unverified.** Measured on TERRA 2026-08-09, once the new roster's default was
actually installed:

| State | GPU used of 24564 MiB | Free |
|---|---|---|
| `huihui_ai/gemma-4-abliterated:26b` resident (17 GB, 32768 ctx, 100% GPU) | 20853 MiB | **~3.6 GiB** |

This supersedes the estimate the design was written against. The chat model was
projected at ~15 GB leaving ~7 GB of headroom; it is 18.0 GB on disk, 17 GB
resident, and leaves **3.6 GiB**. Chroma's fp8 stack is ~13.4 GB, so it cannot
coexist — it would have to evict the chat model. In fp16 (~28 GB) it does not
fit the card at all.

Pony at ~6.5 GB is also above 3.6 GiB on paper. The reason SDXL works today
anyway is the paging behaviour `docs/gpu-host.md` measured — ComfyUI pages
weights against system RAM rather than demanding the whole card. **That was
measured with a 6.5 GB SDXL checkpoint against a smaller chat model, so it
should not be extrapolated to a 13.4 GB Flux stack.** `ollama stop` frees the
card instantly if something does OOM.

Practical consequence: if image generation returns, the realistic shape is
Pony in-chat (paging, as today) and Chroma with the chat model stopped.

**A shared node mapping across two architectures has two latent mismatches.**
If one mapping serves both workflows, `model` writes `ckpt_name` — correct for
Pony's `CheckpointLoaderSimple`, wrong for Chroma's `UNETLoader`, where the
input is `unet_name`. Likewise `seed` writes `seed`, correct for Pony's
`KSampler` and wrong for Chroma's `RandomNoise`, which wants `noise_seed`. Both
node IDs exist, so nothing raises and a validator checking IDs alone passes —
the values are simply ignored, and generation runs with a fixed seed against
whatever model the workflow file names. **Fix the mapping in the same commit
that first selects `chroma`.**

## Provenance note

Chroma, the FLAN encoder and the VAE all trace to their upstreams. **Pony V6 XL
does not:** it is officially distributed on CivitAI only, and CivitAI requires
an API token for downloads, so `LyliaEngine/Pony_Diffusion_V6_XL` is an
unaffiliated third-party mirror. Two things make that acceptable rather than
reckless — `.safetensors` is a data-only format that cannot execute code on
load, unlike the `.ckpt` pickles it replaced, and the checksum above pins
exactly which bytes were reviewed. The alternative is a CivitAI API token and a
download from source.

## Not investigated

- **WAI-Illustrious v17** — anime/illustration specialist, SDXL-based ~7 GB.
  Considered and dropped as out of scope, not rejected on merit.
- **Per-request switching between SDXL and Flux workflows.** Not achievable
  without upstream changes to Open WebUI.
- **Image editing.** Open WebUI has a separate `IMAGES_EDIT_COMFYUI_*` family
  of settings that was never examined.
