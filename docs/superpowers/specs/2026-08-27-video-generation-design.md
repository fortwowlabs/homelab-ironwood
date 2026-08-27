# MiniMax H3 video generation — design

**Status: approved, not yet implemented.** Split out of
`docs/plans/uncensored-image-generation.md` on 2026-08-27 as its own design
effort, separate from Qwen Image Edit (`docs/plans/image-editing.md`) —
past their shared blocker (in-chat image generation not working, fixed
2026-08-20) the two have nothing in common. This is a new subsystem: its own
model, its own runtime question, its own VRAM measurement, no existing flow
in this estate to extend.

Background reading: `docs/gpu-host.md` (the GPU host itself),
`docs/gpu-capacity.md` (measured VRAM discipline this doc follows),
`docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md`
(confirms H3 is not reachable through Open WebUI's `ImagesConfig`).

## What MiniMax H3 actually is

Verified 2026-08-27, not assumed — the original deferred note never sourced
it. MiniMax released H3 as open weights 2026-08-02, with native day-0 ComfyUI
support landing 2026-08-03. It is a 33B-parameter omni-modal model: text,
image, video and audio in one context, generating video with **native
stereo audio** (voice, effects, music in one forward pass, not layered on
after). Three capability modes: text-to-video (T2V), image-to-video (I2V),
and reference-to-video (R2V, up to 9 images / 3 clips / 3 audio references).
Local open weights top out at 768p; 2K is API-only.

Sources: [MiniMax H3 Day-0 Support in ComfyUI](https://blog.comfy.org/p/minimax-h3-day-0-support-in-comfyui),
[MiniMax H3 official page](https://design.minimax.io/h3),
[ComfyUI setup guide](https://docs.comfy.org/tutorials/video/minimax/minimax-h3),
[model file listing](https://comfyui-wiki.com/en/news/2026-08-03-minimax-h3-open-weights-comfyui).

## License and territory — resolved, recorded for the record

The MiniMax H3 Community License's open-weight grant excludes the EU, UK,
US, and South Korea. This estate was confirmed by its operator on 2026-08-27
to be outside all four, so the open weights may be used. Free for research
and commercial use; commercial use above $20M/year revenue needs separate
authorization; commercial UIs must display "MiniMax H3"; outputs/weights may
not be used to train other models. None of the commercial clauses bind a
homelab, and are recorded here only so a future reader does not have to
re-derive them.

## Architecture

**Driven directly from ComfyUI's own web UI** at `http://192.168.1.40:8188`
— not through Open WebUI, which has no video-generation config surface at
all (`docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md`
confirmed H3 is not in `ImagesConfig`). This is the Chroma1-HD pattern from
the image-generation design, not the Pony in-chat pattern: no catalog entry
in `images.yml`, no push through the admin API, no `make` target, no
Ansible involvement of any kind. The GPU host is not Ansible-managed and
this does not change that.

The consequence worth stating plainly: **this design has almost no repo
footprint.** The work is host setup (ComfyUI version, model files, official
templates) plus documentation. There is no code for `make validate` to gate,
because there is nothing here for it to reach.

## Model files

From `Comfy-Org/MiniMax-H3` on Hugging Face — the ComfyUI-packaged form,
**not** `MiniMaxAI/MiniMax-H3` (that is the raw Diffusers-format release and
is not what ComfyUI's native nodes load).

All at the **pruned-int8** tier. This is not a quality choice — it is the
only tier that has any chance of fitting a 24GB card at all. The non-pruned
int8 diffusion model alone is 31.7 GB and bf16 is 61.7 GB; both exceed the
card before a text encoder or VAE is even loaded.

| File | Size | Destination |
|---|---|---|
| `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | ~19.5 GB | `models/diffusion_models/` — T2V + I2V |
| `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | ~19.5 GB | `models/diffusion_models/` — R2V |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | ~14.6 GB | `models/text_encoders/` — shared by all three |
| `minimax_h3_video_vae_fp16.safetensors` | ~4.9 GB | `models/vae/` |
| `minimax_h3_audio_vae_fp32.safetensors` | ~0.6 GB | `models/vae/` |

~59 GB total, against a 2.2 TB disk — not the constraint. The text encoder
is the 4-bit NVFP4 tier specifically (not int8 at 25.3 GB, not bf16 at
48 GB) because third-party reporting describes it as usable "on any GPU";
that claim is unverified here and stays unverified until measured, same as
everything else in this table.

**Verify every download the way Pony and Chroma were verified before this
repo trusted them**: confirm `Comfy-Org/MiniMax-H3` is ungated (no HF token
required — unconfirmed as of this writing, and the FLUX VAE 401 in
`docs/gpu-host.md` is exactly the failure mode an assumption here would
walk into), and hash what lands against the repo's own `lfs.sha256` /
`X-Linked-ETag` field, never the CDN `etag` — the Xet-hash mixup that
produced four wrong checksums earlier on this branch of work
(`docs/plans/uncensored-image-generation.md`) is the same trap here.

## Prerequisites

- **ComfyUI ≥ 0.30.0.** The installed version was never recorded in
  `docs/gpu-host.md`; treat this as needing a check regardless of what is
  currently there, not an assumed pass.
- **System RAM.** Third-party guidance suggests 32–64 GB; nothing in this
  repo records TERRA's installed RAM. Check `Get-CimInstance
  Win32_ComputerSystem` before assuming headroom, the same discipline
  `docs/gpu-host.md` already applies to disk (`Get-PSDrive C`) before every
  large Ollama pull.
- **Native nodes**, confirmed present in ComfyUI ≥0.30.0 with no custom-node
  install required: `EmptyMiniMaxH3LatentAV`, `MiniMaxH3ImageToVideo`,
  `MiniMaxH3ReferenceToVideo`, `MiniMaxH3SigmaShift`. Sage Attention via
  KJNodes is optional speed-only tuning and out of scope for a first pass.

## VRAM strategy

**Stop the resident Ollama chat model before generating** (`ollama stop
<model>`). The pruned-int8 stack is ~39.6 GB on disk before any VRAM
overhead — nowhere near coexisting with a 17–21 GB resident chat model, the
conclusion this estate already reached for Chroma1-HD at a much smaller
~13.4 GB.

**Whether it fits the 24GB card even alone is a genuine open question, not
a promise.** Disk size is not VRAM footprint — `docs/gpu-host.md` and
`docs/gpu-capacity.md` exist because that gap has been wrong, repeatedly, in
both directions on this estate. ComfyUI may keep only part of the pipeline
resident at once (diffusion model, text encoder, VAE are used in sequence,
not simultaneously, in principle), which is the only reason third-party
reports of a 12–24 GB working range are plausible against a 39.6 GB disk
footprint. The first real generation attempt is the measurement. If it does
not fit, the fallback (further offload flags, accepting failure and
documenting it, or revisiting the variant choice) is a finding to record
during implementation, not a guess to bake into this design.

Record whatever is actually measured — idle baseline first, then loaded —
in `docs/gpu-host.md`'s checkpoint section, the same discipline
`docs/gpu-capacity.md` already documents for chat models.

## Workflow templates

All three official templates, pulled from ComfyUI's built-in Template
Library ("Template Library → Video") rather than hand-built or sourced from
a model-card JSON. This is deliberately lower-risk than the SDXL/Pony/Chroma
history: those needed a hand-built workflow (SDXL), a fixed-node workaround
for prompt tags Open WebUI overwrites (Pony), or hand-correcting stale
filenames in an upstream JSON (Chroma). A day-0-supported official template
in ComfyUI's own library has no equivalent known defect — but "no known
defect" is not "verified defect-free," so each template still gets a real
generation, not a load-without-erroring, before being trusted.

## Storage and retention

Output stays in ComfyUI's default output folder on the GPU host with a
`homelab-h3` filename prefix, mirroring the `homelab-owui` convention from
image generation so output is identifiable. **No backup, no automated
pruning.** This is a deliberate choice, not an oversight: the GPU host is
hand-managed and explicitly not infrastructure (`docs/gpu-host.md`), and
video output is large enough that automated retention would be its own
design question this estate does not need yet. Review and clean up by hand.

## Verification

No automated check — nothing here is reachable by `make validate`, there is
no Ansible-managed host to run a nightly timer on, and there is no repo code
to gate the way `tests/validate_openwebui_image_config.py` gates the image
pipeline. Verification is manual and one-time per workflow, done once during
implementation:

For each of T2V, I2V, R2V — generate one real clip and confirm, not assume:

1. A video file actually lands (not an empty result, not an error swallowed
   into silence — the exact failure mode that hid the image-generation
   defect for eleven days).
2. It has audio, at the resolution/duration the workflow claims.
3. `http://192.168.1.40:8188/history` shows the graph executed with the
   expected checkpoint names — the same direct read
   `docs/superpowers/plans/2026-08-14-comfyui-image-generation.md` used to
   prove Pony's checkpoint and score tags actually reached ComfyUI, not
   merely that *something* returned a file at the right size.

## Open risks, recorded rather than resolved

- **Real VRAM footprint is unmeasured.** See "VRAM strategy" above.
- **System RAM is unverified** against the 32–64 GB third-party guidance.
- **Whether `Comfy-Org/MiniMax-H3` is a gated HF repo is unconfirmed.**
- **The NVFP4 text encoder's "works on any GPU" claim is third-party marketing,
  unverified against this card.**

## Out of scope

- Sage Attention / KJNodes speed tuning.
- Any Open WebUI integration — confirmed impossible, not merely deferred.
- Automated backup, retention, or a nightly proof-of-life check.
- The 2K tier (API-only; not available in the open weights at all).
- Qwen Image Edit — tracked separately in `docs/plans/image-editing.md`.
