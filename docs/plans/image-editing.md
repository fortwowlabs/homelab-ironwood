# Image editing (Qwen Image Edit) — deferred

**Status: implemented 2026-08-31.** See
[docs/superpowers/specs/2026-08-27-comfyui-image-editing-design.md](../superpowers/specs/2026-08-27-comfyui-image-editing-design.md)
for the design and
[docs/superpowers/plans/2026-08-27-comfyui-image-editing.md](../superpowers/plans/2026-08-27-comfyui-image-editing.md)
for the implementation. Verified working by `make image-edit-check`.

**One defect only a live end-to-end check caught.** The catalog originally
mapped `type: steps` in `image_edit_workflow_nodes`, but Open WebUI's public
edit API (`EditImageForm`) has no `steps` field at all — `payload.steps` was
always `None`, and unlike `seed` (which has a random fallback in
`_apply_workflow_nodes`), `steps` has none, so it wrote `null` into
ComfyUI's KSampler and ComfyUI rejected the request with a 400. Fixed
(`ffb4c6f`) by removing the mapping entirely and letting the workflow's own
hardcoded `steps: 50` stand — the same treatment already applied to
`width`/`height`/`negative_prompt`. The direct-to-ComfyUI proof run earlier in
implementation never exercised `_apply_workflow_nodes`/`payload.steps` at all,
so it could not have caught this; only the later check that goes through Open
WebUI's real API did.

Split out of
`docs/plans/uncensored-image-generation.md` on 2026-08-27, where it had been
filed alongside MiniMax H3 video generation under one "requested but
deferred" heading since 2026-08-12. The two are unrelated in size and
mechanism — this is a small, cheap extension of the pipeline that already
works; video is a new subsystem with its own runtime and VRAM question. They
were splitting apart in every way that matters except which file they lived
in, which made this page harder to find than it should have been. Video
generation now has its own design effort, implemented — see
`docs/superpowers/specs/2026-08-27-video-generation-design.md`.

## What actually changed since this was requested

**It is unblocked.** The reason both items sat untouched from 2026-08-12
onward was that in-chat image *generation* had never produced an image —
`COMFYUI_WORKFLOW_NODES` was unset, so ComfyUI rejected every request at
validation. That was fixed 2026-08-20: see
`docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md` and
the implementation plan it shipped from. In-chat generation runs on Pony
Diffusion V6 XL today, proven nightly by `make image-gen-check`.

## Why this is expected to be cheap

Open WebUI's editing feature is a separate `IMAGES_EDIT_COMFYUI_*` family of
settings, but it is **not a separate mechanism** — from the design doc
(`docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md:423-428`):

> Image editing needs no new mechanism at all. Every `IMAGES_EDIT_COMFYUI_*`
> key lives in the same `ImagesConfig` object, so editing is more managed
> keys in `images.yml`, an edit workflow file, and the `image`-node rule
> inverted — structure and the check's shape all carry over unchanged.

Concretely, this should mean reusing, not rebuilding:

- The catalog pattern in `inventory/group_vars/all/images.yml` (a few more
  keys, same file).
- `tests/validate_openwebui_image_config.py` (extended, not replaced —
  the `image`-type node rule currently exists specifically to *reject* an
  `image` node in a generation mapping; an edit mapping is where it becomes
  required instead).
- `scripts/owui_image_config.py` (more managed keys in `managed_keys()`).
- `scripts/image_generation_check.py`'s shape, for an analogous
  `image_edit_check.py` — submit a source image, assert a changed image of
  the expected size comes back.

Everything the "Not investigated" section here once listed as outstanding —
reading the `IMAGES_EDIT_COMFYUI_*` code path, sourcing the checkpoint,
measuring VRAM, and the single-workflow question — is now answered by the
design doc and implementation plan linked at the top of this page.
