# Uncensored model roster for chat.fortwow.dev

**Date:** 2026-08-09
**Status:** design approved, not yet implemented

Open WebUI at `chat.fortwow.dev` currently offers one chat model (`qwen3:30b`)
and one coding model two generations old (`qwen2.5-coder:14b`), both safety
aligned upstream. This replaces that roster with abliterated models and adds
saved personas so that "therapy" is a system prompt rather than a fourth
download.

**Image generation is deliberately out of scope.** It was designed alongside
this and split out on 2026-08-09 to keep the change to models only; the full
design, the verified download URLs and checksums, and the non-obvious ComfyUI
findings are preserved in
[docs/plans/uncensored-image-generation.md](../../plans/uncensored-image-generation.md).
That page also records a **live defect** found while designing this — Open WebUI's
`IMAGE_SIZE` defaults to `512x512` against a checkpoint trained at 1024, so
in-chat images have been quietly poor since the feature was enabled. Fixing it
is two lines and needs no new models, but it is not part of this change.

## What this change actually touches

Worth stating up front, because it is smaller than it looks: **every model here
is installed by hand on the Windows GPU host, which is deliberately not
Ansible-managed, and the personas are created in Open WebUI's web UI.** No
deployed configuration changes. The repo's entire contribution is
documentation — `docs/chat-models.md` and an updated `docs/gpu-host.md`.

That is a real limitation, not an oversight. It is discussed under
*Where the persona text lives* below.

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

### Obtaining the models

Five of the six are plain `ollama pull` against the Ollama registry, needing no
account and no token:

```powershell
ollama pull huihui_ai/gemma-4-abliterated:26b
ollama pull huihui_ai/gemma-4-abliterated:31b
ollama pull huihui_ai/Qwen3.6-abliterated:27b
ollama pull qwen3-coder:30b
ollama pull aratan/qwen3.6-claude-coder-35b-A3b-mlx-Q4KM-abliterated
```

**The DavidAU model is the exception: it is a Hugging Face GGUF repo, not an
Ollama registry entry**, so it needs the `hf.co/` prefix and an explicit quant
tag. `ollama pull DavidAU/...` alone fails with a not-found error that reads
like the model was withdrawn:

```powershell
ollama pull hf.co/DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF:Q4_K_M
```

Confirm the quant tag exists in that repo's file list before pulling — DavidAU
publishes many quants per model and the available tag names vary between them.

### Disk

New pulls total ~102 GB. Net of the ~27 GB retired, the GPU host ends up
carrying roughly **110 GB of models**, up from about 35 GB today.
Unremarkable for a modern SSD, but it is not a rounding error on a small boot
drive — check before pulling.

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

Note that with image generation deferred, this is now the *only* state this
change creates outside git. That makes the exception more visible than it was
when it sat beside a set of committed workflow files, and it is a fair reason
to revisit the decision — but it does not make the Ansible task any less
disproportionate today.

## Files that change

| File | Change |
|---|---|
| `docs/chat-models.md` | New — roster, persona text, model-switching cost |
| `docs/gpu-host.md` | Updated pull list, Continue config, VRAM table |
| `docs/plans/uncensored-image-generation.md` | New — the deferred image work |

No Ansible, no catalog, no validator. The change is entirely hand-work on the
GPU host and the Open WebUI web UI, with the repo recording what was done.

## Verification

**Positive control: prove the models are actually uncensored.** A pulled tag, a
loaded model and a plausible chat reply are byte-identical between a working
abliteration and the wrong model pulled by mistake. So verification is a prompt
that the outgoing `qwen3:30b` refuses, run against each new chat model, which
must come back answered. If it refuses, either the abliteration did not take or
the tag was wrong — and nothing else in the stack would reveal it.

The prompt must be calibrated first: confirm `qwen3:30b` actually refuses it
before retiring that model, or the control proves nothing.

This mirrors the reasoning behind the credential canary in `CLAUDE.md`, with
the advantage that the control here is a prompt rather than a deliberately
insecure service, so it costs nothing to keep permanently.

The rest:

- `curl http://192.168.1.40:11434/api/tags` lists all six new models.
- One real chat turn per chat model. The dropdown populates from `/api/tags`
  even when generation is broken, so a populated list proves nothing.
- Each persona sends a message and behaves per its prompt. A persona that
  failed to attach looks identical to one that worked, so compare against the
  bare base model rather than reading the reply on its own.
- Continue autocomplete in a real file against `qwen3-coder:30b`.

## Rollout

Standard workflow from `CLAUDE.md`: branch → edit → `make validate` →
`make infra` → commit → clean tree → final `make infra` (expect `changed=3` on
svc-infra's first run after a commit, then `changed=0` on the second) →
`make verify` → merge → push → delete branch.

The deploy steps still apply even though the change is documentation-only: the
nightly runner keeps a `git archive` of the tree at `/opt/homelab-iac` with the
deployed revision in `.deployed-rev`, so a commit still makes the sync block
fire. Running it keeps that copy current rather than leaving it a revision
behind.

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

- **Image generation** — deferred in full to
  [docs/plans/uncensored-image-generation.md](../../plans/uncensored-image-generation.md).
- Automating the GPU host. It stays hand-managed by deliberate decision.
- Personas as committed configuration. Deferred; see the tradeoff above.
- Any model above the 24 GB single-card budget — GLM 5.2, DeepSeek V4,
  Kimi K3 and Llama 4 Scout are all out of reach on this hardware regardless
  of how they benchmark.
