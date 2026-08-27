# Image editing (Qwen Image Edit) — deferred

**Status: not implemented.** Split out of
`docs/plans/uncensored-image-generation.md` on 2026-08-27, where it had been
filed alongside MiniMax H3 video generation under one "requested but
deferred" heading since 2026-08-12. The two are unrelated in size and
mechanism — this is a small, cheap extension of the pipeline that already
works; video is a new subsystem with its own runtime and VRAM question. They
were splitting apart in every way that matters except which file they lived
in, which made this page harder to find than it should have been. Video
generation now has its own design effort — see
`docs/superpowers/specs/2026-08-27-video-generation-design.md` once it lands,
or the `docs/video-generation-design` branch in the meantime.

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

## Not investigated — this is genuinely the starting point

Nobody has yet:

- Read `IMAGES_EDIT_COMFYUI_*` in Open WebUI's `config.py` /
  `utils/images/comfyui.py` the way the generation path was read line by
  line before it was trusted. The claim above is inference from the
  generation design, not a verified reading of the edit code path.
- Sourced a "Qwen Image Edit" checkpoint — confirmed it exists as a public,
  ungated, ComfyUI-loadable file, the way Pony and Chroma were verified
  (repo, filename, license, checksum) before this repo trusted them. Nothing
  in this repo has downloaded or hashed anything for this yet.
- Measured its VRAM footprint against the card. Same discipline as
  `docs/gpu-capacity.md` and the generation design: idle baseline first,
  then measured, never estimated from a model card.
- Checked whether it can share ComfyUI's single-workflow constraint with
  Pony (the same one that stopped Pony and Chroma coexisting in-chat) or
  needs its own selection story.

## Suggested first step

Read the `IMAGES_EDIT_COMFYUI_*` code path and confirm the claim above before
sourcing any checkpoint — if editing turns out to need its own mechanism
after all, that changes the cost estimate that makes this worth doing before
video.
