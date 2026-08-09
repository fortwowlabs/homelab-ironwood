# Uncensored model roster for chat.fortwow.dev

**Date:** 2026-08-09
**Status:** design approved, not yet implemented

Open WebUI at `chat.fortwow.dev` currently offers one chat model (`qwen3:30b`),
one coding model two generations old (`qwen2.5-coder:14b`), and stock SDXL for
images. All three are safety-aligned upstream. This replaces that roster with
abliterated models across chat, coding and image generation, and adds saved
personas so that "therapy" is a system prompt rather than a fourth download.

## What abliteration is, and why "heretic" appears in every model name

Refusal behaviour in an aligned transformer localises to a direction in the
residual stream. Abliteration removes that direction by orthogonalisation —
no retraining, no fine-tuning, just a weight edit. [Heretic][heretic] is the
tool that automates it with Optuna-driven parameter search, and it reports
substantially lower KL divergence from the base model than hand-tuned
abliterations, meaning it decensors while damaging the model less. It has
produced thousands of models on Hugging Face since release, which is why the
word is now effectively a category label rather than a brand.

[heretic]: https://github.com/p-e-w/heretic

Two framings to keep straight, because they behave differently:

- **Abliterated / heretic** — a weight edit on an existing instruct model.
  Preserves the base model's character; occasionally leaves scars on
  instruction-following.
- **Uncensored fine-tunes and merges** (DavidAU's line, Dolphin) — retrained
  or merged for a target style. Stronger personality, less predictable on
  factual work.

Both are in the roster below, for different jobs.

## The governing constraint

24 GB of VRAM holds exactly one large model. Measured on this host
(`docs/gpu-host.md`), `qwen3:30b` occupies **~22.8 GB resident** — 18 GB of
weights plus a 32768-token KV cache. There is no second slot.

**Everything below is a catalogue to swap between, not a set of things running
concurrently.** Switching models in the Open WebUI dropdown evicts and reloads,
costing roughly 20–30 seconds. This was chosen deliberately over running
smaller models that could co-reside: on a single-user homelab, an occasional
reload pause is cheaper than permanently running a weaker model than the card
can handle.

## Model roster

### Chat

| Ollama ref | Disk | Role |
|---|---|---|
| `huihui_ai/gemma-4-abliterated:26b` | ~15 GB | **Default.** MoE, vision, tool calling, warmest prose |
| `huihui_ai/gemma-4-abliterated:31b` | ~17 GB | Dense — slower, stronger on hard reasoning |
| `huihui_ai/Qwen3.6-abliterated:27b` | ~16 GB | Technical and agentic work |
| `DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF` | ~16 GB | Creative writing, immersive roleplay |

`huihui_ai` is the namespace that matters — the only publisher of abliterated
models on the Ollama registry with pull counts in the hundreds of thousands
(861k on the Gemma 4 line, 495k on Qwen 3.5, 302k on Qwen 3.6), and it
maintains variants across sizes rather than dumping a single quant.

The Gemma line is the default rather than Qwen because prose quality carries
the therapy personas, and Gemma reads warmer. Qwen 3.6 is retained for
technical work, where its dryness is an asset.

**26b and 31b are the same family**, differing mainly in speed against
reasoning depth. One of them will likely go unused. Prune whichever loses
after a month of real use rather than leaving both installed forever.

### Coding

| Ollama ref | Disk | Role |
|---|---|---|
| `qwen3-coder:30b` | ~18 GB | Continue's default. Stock weights, 256K context, A3B MoE |
| `aratan/qwen3.6-claude-coder-35b-A3b-mlx-Q4KM-abliterated` | ~20 GB | On demand, when a stock model balks |

The default is **deliberately not abliterated.** Coding models rarely refuse,
so abliteration buys almost nothing here while costing measurable quality on
ordinary work. The abliterated variant exists for security-research prompts
where a stock model declines, selectable in the Open WebUI dropdown.

### Retired

`qwen3:30b` and `qwen2.5-coder:14b`, freeing ~27 GB.
`qwen2.5-coder:1.5b-base` and `nomic-embed-text` stay — autocomplete and
embeddings are small, always-on, and unaffected by any of this.

### Disk

New pulls total ~102 GB. Net of the ~27 GB retired and the ~27 GB of image
models, the GPU host ends up carrying roughly **130 GB of models**, up from
about 35 GB today. Unremarkable for a modern SSD,
but it is not a rounding error on a small boot drive — check before pulling.

## Personas

A model is raw weights with no personality. A **persona** is a system prompt —
invisible text prepended to every turn — saved under a name in Open WebUI's
Workspace screen, where it appears in the model dropdown as though it were its
own model. It is not: it is a shortcut meaning *that model, with this
paragraph in front*.

This is why therapy is not a fourth model download. There is no local
"therapy model" worth pulling; the searches surface consumer apps, not weights.
What makes a therapy assistant is a system prompt plus long context plus
persistent memory, all of which Open WebUI already provides. The uncensored
base matters for a specific reason: a stock aligned model breaks character and
emits crisis-hotline boilerplate exactly when a conversation gets heavy, which
is when it is least useful.

Planned personas, all over `gemma-4-abliterated:26b`: `Therapist`,
`Unfiltered`, and others as they earn their place.

### Where the persona text lives, and the tradeoff accepted

**Personas are created in the Open WebUI Workspace screen and transcribed into
`docs/chat-models.md`.**

This is an explicit, knowing exception to the repo's rule that deployed state
equals committed state. The live copy lives in `webui.db`, which
`backup_paths: [open-webui]` already captures, so it is not at risk of loss —
but "restore a backup" is a weaker guarantee than "re-run `make infra`", and
nothing detects drift between the live persona and the transcribed one.

The alternative considered was defining personas as YAML in group_vars and
POSTing them to Open WebUI's `/api/v1/models` endpoint from an Ansible task.
That preserves the guarantee but requires a compare-before-write task; a naive
POST-every-run reports `changed` on every deploy and destroys the `changed=0`
proof the whole repo depends on. Rejected as disproportionate for what is
ultimately four paragraphs of text. Revisit if the persona set grows or starts
mattering operationally.

## Image generation

### The constraint

Open WebUI holds exactly one ComfyUI workflow (`COMFYUI_WORKFLOW`). Its
per-request model dropdown maps to a checkpoint-name input *within* that one
workflow, so it can only switch between models of the same architecture.

- **Pony Diffusion V6 XL** is SDXL-architecture: one checkpoint file,
  `CheckpointLoaderSimple`.
- **Chroma1-HD** is Flux-architecture (8.9B, retrained from FLUX.1-schnell
  with safety alignment removed): three files across three directories,
  `UNETLoader` + `CLIPLoader` + `VAELoader`.

**They cannot share a workflow.** Having both in one chat dropdown is not
achievable, so the design picks which is reachable from inside a conversation.

### The split

**Pony V6 XL is the in-chat engine.** ~6.5 GB — the same VRAM profile as the
SDXL base already proven to coexist with a resident chat model — and it works
with Open WebUI's built-in default workflow. It also has by far the deepest
LoRA ecosystem on CivitAI.

**Chroma1-HD is driven directly from ComfyUI** at `http://192.168.1.40:8188`
for quality work. No Open WebUI involvement, full node-graph control.

**A repo variable selects which workflow Open WebUI uses** —
`image_workflow: pony | chroma` in group_vars, with both workflow JSONs
committed. Switching is a `make infra`, not a per-request choice, which keeps
the decision in git and makes trying Chroma in-chat later a one-line change.

### Files on the GPU host

```
models/checkpoints/ponyDiffusionV6XL.safetensors           ~6.5 GB
models/diffusion_models/chroma1-hd-fp8-scaled.safetensors    ~9 GB
models/clip/t5xxl_fp8_e4m3fn.safetensors                     ~5 GB
models/vae/ae.safetensors                                  ~335 MB
```

The existing `sd_xl_base_1.0.safetensors` stays on disk as a known-good
control. It costs 6.5 GB and is the fastest way to tell "the new checkpoint is
bad" from "the workflow is bad".

### The risk in this section, stated plainly

fp8 rather than fp16 is deliberate: Chroma's stack is **~14 GB in fp8, ~28 GB
in fp16**. Alongside a 15 GB resident chat model, even the fp8 figure exceeds
the card.

`docs/gpu-host.md` measured ComfyUI paging its weights against system RAM
rather than demanding the whole card, which is the only reason this is
plausible at all. But **that was measured with a 6.5 GB SDXL checkpoint, and
extrapolating it to a 14 GB Flux stack is exactly the kind of assumption that
document warns against.** Chroma-alongside-chat is unverified and must be
measured, not assumed. `ollama stop` frees ~20 GB instantly if it OOMs. Pony
carries no such risk, which is a second reason it is the in-chat default.

### Three defects in the current config this fixes

1. **`IMAGE_SIZE` is unset and defaults to `512x512`.** SDXL is trained at
   1024 and degrades badly below it. In-chat images have likely been quietly
   poor since the feature was enabled, and this would never surface as an
   error. Set `1024x1024`.
2. **`IMAGE_STEPS` is unset and defaults to `50`** — roughly double what Pony
   needs. 28 gives the same result in half the time.
3. **`COMFYUI_WORKFLOW_NODES` is parsed with a bare `except JSONDecodeError`
   that falls back to `[]`.** A typo in that JSON configures nothing, logs
   nothing, and silently leaves image generation on defaults. This is the
   repo's canonical failure mode — a clean result because the thing never ran
   — and it is why the validator below exists.

All three are set through the environment rather than the admin UI, because
`ENABLE_PERSISTENT_CONFIG: "false"` makes the environment authoritative on
every container start: anything clicked in the admin UI is silently discarded
on the next restart. The `ENABLE_SIGNUP` comment in `infra-apps.yml` already
documents this trap; image config falls into the same one.

## Files that change

| File | Change |
|---|---|
| `inventory/group_vars/all/main.yml` | New `image_workflow: pony` |
| `inventory/group_vars/all/infra-apps.yml` | `open-webui` env: add `IMAGE_SIZE`, `IMAGE_STEPS`, `IMAGE_GENERATION_MODEL`, `COMFYUI_WORKFLOW`, `COMFYUI_WORKFLOW_NODES` |
| `roles/svc_infra/files/comfyui/pony.json` | New — API-format workflow, SDXL |
| `roles/svc_infra/files/comfyui/chroma.json` | New — API-format workflow, Flux |
| `tests/validate_openwebui_image_config.py` | New validator, wired into `make validate` |
| `docs/chat-models.md` | New — roster, persona text, swap procedure |
| `docs/gpu-host.md` | Updated pull list, Continue config, VRAM table |

Workflow selection:

```yaml
COMFYUI_WORKFLOW: "{{ lookup('file', playbook_dir + '/roles/svc_infra/files/comfyui/' + image_workflow + '.json') }}"
```

One variable, both workflows in git, no hand-editing of JSON in YAML.

## The validator

`tests/validate_openwebui_image_config.py` asserts what Open WebUI will not:

- Both the workflow JSON and the nodes JSON parse.
- **Every node ID referenced in the nodes mapping exists in the workflow.**
- `image_workflow` names a file that exists.

Open WebUI's own failure mode is a silent fallback to `[]`, so this test is the
only mechanism that can distinguish a typo from a working config before it
reaches the host.

## Verification

Everything else here can be checked by a green container. The first item
cannot, and it is the one that matters.

**Positive control: prove the models are actually uncensored.** A pulled tag, a
loaded model and a plausible chat reply are byte-identical between a working
abliteration and the wrong model pulled by mistake. So verification is a prompt
that the outgoing `qwen3:30b` refuses, run against each new chat model, which
must come back answered. If it refuses, either the abliteration did not take or
the tag was wrong — and nothing else in the stack would reveal it.

This mirrors the reasoning behind the credential canary in `CLAUDE.md`, with
the advantage that the control here is a prompt rather than a deliberately
insecure service, so it costs nothing to keep permanently.

The rest:

- `curl http://192.168.1.40:11434/api/tags` lists all six new models.
- One real chat turn per chat model. The dropdown populates from `/api/tags`
  even when generation is broken, so a populated list proves nothing.
- One in-chat image **with a fresh seed** — ComfyUI caches by workflow hash and
  returns the previous image in ~2 s, indistinguishable from success.
- Confirm the returned image is actually 1024×1024. That is the `IMAGE_SIZE`
  fix proving itself.
- Continue autocomplete in a real file against `qwen3-coder:30b`.
- `nvidia-smi` with a chat model resident and Chroma generating — the
  measurement that decides whether Chroma can ever become the in-chat engine.

## Rollout

Standard workflow from `CLAUDE.md`: branch → edit → `make validate` →
`make infra` → commit → clean tree → final `make infra` (expect `changed=3` on
svc-infra's first run after a commit, then `changed=0` on the second) →
`make verify` → merge → push → delete branch.

Model pulls and checkpoint downloads happen by hand on the GPU host, documented
in `docs/gpu-host.md` but not automated — consistent with that machine
deliberately not being under Ansible management.

## Exposure note

`chat.fortwow.dev` is published through Caddy with a public certificate and is
deliberately **not** behind Authelia — it has its own account system, and
fronting it would mean two logins and would break API clients. Its front door
is therefore Open WebUI's own login, with `ENABLE_SIGNUP: "false"` and
`DEFAULT_USER_ROLE: pending` as the whole of the control.

That arrangement predates this change and this design does not alter it. It is
recorded here only because the change makes the service materially more
interesting to reach: the same single login now stands in front of models with
no refusal behaviour at all. If signup is ever re-enabled, or the secret key
leaks, the blast radius is larger than it was yesterday.

## Out of scope

- Automating the GPU host. It stays hand-managed by deliberate decision.
- Per-request switching between SDXL and Flux workflows in Open WebUI. Not
  achievable without upstream changes.
- Personas as committed configuration. Deferred; see the tradeoff above.
- Any model above the 24 GB single-card budget — GLM 5.2, DeepSeek V4,
  Kimi K3 and Llama 4 Scout are all out of reach on this hardware regardless
  of how they benchmark.
