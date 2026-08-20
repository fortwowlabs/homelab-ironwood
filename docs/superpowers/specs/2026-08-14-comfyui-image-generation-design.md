# In-chat image generation, actually working

**Date:** 2026-08-14
**Status:** implemented for SDXL and **verified working 2026-08-20** — a real
1024x1024 PNG in 10.5s, with ComfyUI's `/history` confirming the executed graph.
Still outstanding: retiring the superseded env keys (Task 7), the nightly check
(Task 8), and the Pony checkpoint (Task 9).

In-chat image generation at `chat.fortwow.dev` has never produced an image.
Not a poor one — none. The container is green, the request is accepted, and
nothing reaches ComfyUI. This change makes it work with the checkpoint already
on the GPU host, then swaps in Pony Diffusion V6 XL, and leaves behind a check
that would have caught the original defect on the day it was introduced.

The research this builds on lives in
[docs/plans/uncensored-image-generation.md](../../plans/uncensored-image-generation.md):
verified download URLs, checksums, and the non-obvious ComfyUI findings. That
page remains the reference for the model files. **Where this spec and that page
disagree about Open WebUI's internals, this spec is correct** — see *Corrections
to the plan page* below.

**Image editing and video generation are out of scope**, but the design was
chosen partly on how cheaply they slot in afterwards. See *What this buys the
next two features*.

## The diagnosis, corrected

Open WebUI injects prompt, size, steps, seed and checkpoint into a ComfyUI
workflow only through `COMFYUI_WORKFLOW_NODES`. That variable is unset, so
`config.py` parses `''`, raises `json.JSONDecodeError`, and lands on `[]`.
`_apply_workflow_nodes` iterates that empty list and substitutes nothing. What
ComfyUI receives is Open WebUI's compiled-in default workflow verbatim, naming a
checkpoint (`model.safetensors`) that does not exist on the host, so ComfyUI
rejects it at validation.

That much the plan page already established. Reading
[`utils/images/comfyui.py`][comfyui-src] and [`routers/images.py`][images-src]
during this design changed the rest.

**The mapping alone would not have fixed it.** Four things are missing, not one:

1. The node mapping (`COMFYUI_WORKFLOW_NODES`).
2. A committed workflow for it to map onto (`COMFYUI_WORKFLOW`).
3. `IMAGE_GENERATION_MODEL` — the source of the checkpoint name. `_apply_workflow_nodes`
   writes its `model` argument into the mapped node, and that argument is
   `image_config.IMAGE_GENERATION_MODEL`. It is set nowhere. A flawless mapping
   with this key empty submits an empty checkpoint name.
4. A delivery path that beats the database rows — see *Why the environment no
   longer works*.

### Five findings not previously recorded

- **Open WebUI reaches ComfyUI over a WebSocket**, not only HTTP. It queues over
  `POST /prompt`, then blocks on `ws://192.168.1.40:8188/ws?clientId=…` for the
  completion event. `docs/gpu-host.md` does not record that the WS path must
  work; a proxy or firewall permitting HTTP but not WebSocket upgrade would
  produce exactly the observed symptom.
- **The workflow must contain a `SaveImage` or `PreviewImage` node.**
  `_ws_get_images` collects outputs only from those two `class_type`s. A
  workflow that generates correctly but ends in neither returns an empty image
  list: success, no image, no error.
- **`set_image_model` does not validate the checkpoint** for the comfyui engine.
  It writes the config row and returns. A misspelled checkpoint name is accepted
  silently and fails later, somewhere else.
- **`GET /api/v1/images/models` is a free live probe of mapping correctness.**
  It evaluates `workflow[model_node_id]['class_type']`, so a mapping naming a
  node absent from the workflow returns HTTP 400 rather than failing silently.
- **`POST /api/v1/images/config/update` is whole-object, not a patch.** Its body
  is the entire `ImagesConfig` model with every field required, and it ends in
  `Config.upsert(updates)` across all ~40 image keys at once.

### Corrections to the plan page

**The `inputs[None]` claim is wrong.** The plan page states that the `seed`,
`model` and `image` node types read `node.key` with no fallback, so an absent
`key` writes to `inputs[None]`. In fact `ComfyUINodeInput.key` defaults to
`'text'`. An absent `key` writes `inputs['text']` — a key `CheckpointLoaderSimple`
does not have and ComfyUI quietly ignores. The operational advice is unchanged
(always set `key` explicitly for those three types), but the failure is
*quieter* than documented, not louder: the workflow's hardcoded checkpoint runs
instead of erroring.

**The `/history` positive control no longer exists.** The plan page cites
ComfyUI's history holding exactly one entry ever — `homelab-verify-063550`,
hand-submitted on 2026-08-09 — as proof that no Open WebUI generation had ever
arrived. Probed on 2026-08-13, `/history` holds **zero** entries. ComfyUI's
history is in-memory and a restart clears it, so "history is empty" no longer
distinguishes "never worked" from "recently restarted." Nothing in this design
may lean on it.

**`IMAGE_SIZE` and `IMAGE_STEPS` are not "two lines that are still required".**
They are superseded entirely — see the next section.

## Why the environment no longer works

The plan page was written when `ENABLE_PERSISTENT_CONFIG` was `false` and the
environment was authoritative on every container start. It flipped to `true` on
2026-08-10 ([infra-apps.yml:579](../../../inventory/group_vars/all/infra-apps.yml#L579)).
Resolution is now:

- A key with **no database row** takes the environment value.
- A key **with a row** ignores the environment permanently.
- Rows appear the moment that section is saved in the admin UI.

So adding `COMFYUI_WORKFLOW_NODES` to `infra-apps.yml` and running `make infra`
may change nothing at all: the quadlet updates, the deploy reports `changed`,
and image generation stays exactly as broken. This is the failure
[openwebui-settings-as-code.md](../../plans/openwebui-settings-as-code.md)
exists to describe.

A second, independent problem rules the environment out even if no rows existed:
quadlets render env as `Environment="NAME=value"` on a single line. A ComfyUI
workflow is multi-line JSON full of `"` characters. Minifying and escaping it
onto one systemd line is possible and unreadable.

**Decision: push the configuration through Open WebUI's admin API**, from a
`make` target outside the Ansible play. It wins whether or not a row exists,
keeps the workflow a normal diffable file, adds no per-run churn to `make infra`,
and reuses the mechanism the settings-as-code work already needs.

### The cost, stated plainly

Because the update endpoint upserts every image key at once, the first push
writes database rows for the **entire** image subtree, permanently retiring the
environment for all of them. Concretely, this stops working:

```yaml
ENABLE_IMAGE_GENERATION: "{{ 'true' if gpu_host_online | bool else 'false' }}"
```

**Flipping `gpu_host_online` to false and running `make infra` will no longer
disable image generation.** That behaviour moves into `images.yml` and is
applied by the push. The superseded keys are **deleted** from `infra-apps.yml`
rather than left in place, with a comment pointing at the new authority — a
stale environment variable that looks authoritative and is not is precisely the
trap this repo keeps documenting.

## Architecture

Following the `models.yml` precedent: catalog data in `group_vars` that **no
play or role reads**, consumed only by `scripts/` and `tests/`. The deploy
surface is unchanged, so `make infra` keeps proving `changed=0`.

| Path | Purpose |
|---|---|
| `inventory/group_vars/all/images.yml` | New. The catalog: `image_workflow` selector, node mapping as YAML, scalar settings. |
| `inventory/comfyui-workflows/<name>.json` | New. API-format workflows, one per selector value. |
| `scripts/owui_image_config.py` | New. Pushes catalog → Open WebUI admin API. |
| `scripts/image_generation_check.py` | New. End-to-end proof, one-shot and nightly. |
| `tests/validate_openwebui_image_config.py` | New. Offline gate, wired into `validate-catalog`. |
| `inventory/group_vars/all/infra-apps.yml` | Edited. Superseded image env keys deleted. |
| `roles/svc_infra/` | Edited. Deploys the admin token for the nightly check. |
| `Makefile` | Edited. `owui-image-config`, `image-gen-check`. |

The mapping is declared as YAML rather than JSON so diffs are readable; the push
tool serialises it on the way out.

**Workflows live outside `group_vars/all/` deliberately.** Ansible auto-loads
every file in that directory as variables, and a workflow JSON dropped there
would be parsed as top-level vars.

## The push tool

`scripts/owui_image_config.py`, run by `make owui-image-config`.

1. Load `images.yml` and the workflow named by `image_workflow`.
2. `GET /api/v1/images/config`.
3. Overlay only the managed keys.
4. `POST /api/v1/images/config/update`.
5. **Re-`GET` and assert the managed keys came back exactly as sent.**

Step 2 is mandatory rather than an optimisation: the POST body is the entire
`ImagesConfig` with every field required, so constructing it from scratch would
blank every key we do not manage.

Step 5 is the point of the tool. A 200 is not proof. `update_config` performs
its own validation and normalisation — stripping trailing slashes from base
URLs, enforcing `^\d+x\d+$` on `IMAGE_SIZE`, rejecting negative `IMAGE_STEPS` —
so a readback disagreeing with what was sent is the signal that something was
rejected or silently rewritten. Without it, this is another green check proving
only that a request was accepted.

**Running it twice must exit 0 with "no change."** That is this tool's analogue
of `changed=0`, and it is what makes the push safe to re-run after any
`make infra`.

### Secret handling

The GET response carries `COMFYUI_API_KEY`, `IMAGES_OPENAI_API_KEY` and both
Gemini keys. The tool holds the full config in memory by construction.
Therefore: it never prints the payload, never writes it to disk, and on any
error reports **key names only, never values**. `--dry-run` diffs the managed
keys with values redacted except for a short allowlist of known-safe keys
(workflow, mapping, size, steps, model name).

The admin token is read from `OWUI_ADMIN_TOKEN` in the environment, or
`--token-file`. The Makefile does **not** extract it from `vault.yml`: this
repo's standing rule is that vault secrets never reach a terminal or a log, and
a `make` recipe that pipes a secret can echo it on failure. The token is
recorded in the vault for the record and retrieved by hand. One manual step per
operator machine, in exchange for the secret never passing through a recipe.

### Exit codes

Following the `homelab-metric-write` convention of making "could not look" a
state of its own:

| Code | Meaning |
|---|---|
| 0 | Pushed and read back identical, or already identical — nothing to do |
| 1 | Bad arguments, Open WebUI unreachable, or auth rejected — *could not look* |
| 2 | Pushed, but the readback disagrees — rejected or silently rewritten |
| 3 | Catalog or workflow file invalid — the validator should have caught this |

## The offline validator

`tests/validate_openwebui_image_config.py`, wired into `validate-catalog`. Every
failure mode in this feature is silent, so this is the only mechanism that can
distinguish a typo from a working configuration before it reaches the host.

**Structural:**

- `images.yml` parses; `image_workflow` names a file that exists.
- Every workflow parses and is **API format, not editor format**. The
  discriminator is concrete: API format is a flat map of node-id →
  `{class_type, inputs}`; editor format carries top-level `nodes` and `links`
  arrays. Reject the latter explicitly — the two look similar enough to confuse
  and Open WebUI cannot read the editor form.
- Every workflow contains at least one `SaveImage` or `PreviewImage` node.

**Mapping:**

- **Every node ID in the mapping exists in *every* committed workflow**, not
  only the selected one, so changing `image_workflow` is never the step that
  discovers a broken mapping.
- Node `type` is one of the nine Open WebUI handles (`model`, `prompt`,
  `negative_prompt`, `image`, `width`, `height`, `n`, `steps`, `seed`) or absent
  (the static-value form). An unrecognised type is skipped silently, which is
  indistinguishable from a bad ID.
- `model`, `seed` and `image` nodes carry an **explicit `key`**, because the
  default of `'text'` is wrong for all three.
- **`image`-type nodes are rejected in the generation mapping.**
  `ComfyUICreateImageForm` has no `image` field, so `_apply_workflow_nodes`
  raises `AttributeError` on `payload.image`, which `comfyui_create_image`
  swallows into `None`. That type belongs only to the editing mapping.
- Required coverage: `model`, `prompt`, `width`, `height`, `steps`, `seed`.

**Class/key agreement — the check that replaces a warning.** The plan page warns
that a shared mapping has two latent mismatches: `model` writes `ckpt_name`,
right for `CheckpointLoaderSimple` and wrong for `UNETLoader` (`unet_name`); and
`seed` writes `seed`, right for `KSampler` and wrong for `RandomNoise`
(`noise_seed`). Both node IDs exist, so nothing raises and a validator checking
IDs alone passes — the values are simply ignored. The page's advice is to
remember to fix it. Since the workflow file records each node's `class_type`,
**the validator checks it instead**: a mapped `key` must be an input the node's
class actually accepts. Same for `steps`, `width` and `height`.

**Mirrored server-side rules**, so they fail at `make validate` rather than at
push time: `IMAGE_SIZE` matches `^\d+x\d+$`, `IMAGE_STEPS` ≥ 0,
`IMAGE_GENERATION_MODEL` non-empty.

**Its own self-check.** A `VALIDATION_CASES` table with one deliberately-broken
configuration per rule above, asserted at the top of `main()` — the convention
`tests/validate_grafana_dashboards.py:124` already uses. A gate against silent
failure does not get to fail silently itself.

**Out of its reach by construction:** whether the named checkpoint exists on the
GPU host. That is live state and belongs to the runtime check.

## The runtime check

`scripts/image_generation_check.py`, run by `make image-gen-check`. Named so
because `image-check` already belongs to the container-digest tooling.

Four steps, cheapest first:

1. `GET /api/v1/images/models` — returns 400 if the mapping names a node absent
   from the workflow, and otherwise returns ComfyUI's live checkpoint list,
   which lets the check assert `IMAGE_GENERATION_MODEL` is actually present on
   the host. This is the live half the offline validator cannot do.
2. `POST /api/v1/images/generations` with a fixed prompt.
3. Fetch the returned image; assert the bytes **are a PNG** (magic number,
   plausible size), not merely that a 200 arrived.
4. **Assert the image is 1024×1024.**

Step 4 is the strongest assertion in this design. A 512×512 result means the
`width`/`height` mapping never reached ComfyUI and the compiled-in default
workflow ran — precisely the original defect. So the check does not merely prove
that *an* image appeared; it proves **our** workflow produced it. Had it
existed, the defect would have been caught the day it was introduced rather than
surviving until someone read upstream source.

### Tri-state verdict

| Verdict | Meaning |
|---|---|
| `ok` | PNG returned at the expected dimensions |
| `broken` | Reachable, but no image, wrong dimensions, or a 400 from `/models` |
| `inconclusive` | GPU host asleep, Open WebUI unreachable, auth rejected, timeout |

`inconclusive` escalates rather than passing. "Could not look" reading as "fine"
is how every other check in this repo has failed at least once.

### Trending

Emit `homelab_image_generation_ok`, `homelab_image_generation_duration_seconds`
and `homelab_image_generation_width` through `homelab-metric-write`.

Two clarifications, because this check has two callers. Run by an operator via
`make image-gen-check`, it emits nothing — it is a local diagnostic. Run by the
nightly timer on svc-infra, the **script itself** writes the textfile. Neither
path templates a metrics file from `roles/svc_infra`, which is what the
`changed=0` rule actually forbids: a file that changes every run would make
every `make infra` report `changed`.

**Emit before asserting**, so the chart does not go blank exactly when something
is wrong, and emit the same numbers the alert used rather than re-deriving them.

**On `inconclusive`, publish nothing.** `homelab-metric-write` leaves the
previous file in place when given no input, and a stale number is detectable
where a fabricated zero reads as good news — an `_ok 0` written because the GPU
host was asleep is a false alarm, and an `_width 0` is a measurement nobody
took.

### Two consequences of running it nightly

- **The token must exist on svc-infra**, not only on an operator's machine: a
  vault secret deployed as a 0600 root-owned file with `no_log: true`, the
  pattern `roles/svc_infra/tasks/files.yml` already uses. This is the only part
  of the feature touching the deploy surface; being a static file, it settles to
  `changed=0` after the first deploy.
- **Each run leaves a PNG on the GPU host.** ComfyUI's output directory grows by
  one image a day forever. The workflow's `SaveImage` node uses a distinct
  `filename_prefix` so the check's own output is identifiable and prunable.

## Rollout

### Step 0 — measured 2026-08-20

Done. Read with an admin-authenticated `GET /api/v1/images/config` rather than
`/api/v1/configs/export`, which would have put four API keys on the terminal to
answer a question about three.

**Five of the eight managed keys already held their catalog values** —
`ENABLE_IMAGE_GENERATION`, `IMAGE_GENERATION_ENGINE`, `COMFYUI_BASE_URL`,
`IMAGE_SIZE` and `IMAGE_STEPS`. So the environment had seeded, and no
admin-UI save had ever overridden them. Three differed:

| Key | Found | Meaning |
|---|---|---|
| `COMFYUI_WORKFLOW_NODES` | `[]` | The mapping was genuinely unset, as diagnosed. |
| `IMAGE_GENERATION_MODEL` | `""` | **Empty.** No checkpoint name to submit. |
| `COMFYUI_WORKFLOW` | the compiled-in default | Never replaced. |

`IMAGE_GENERATION_MODEL` being empty is the finding that matters most, because
the plan page never mentions it. It confirms by measurement what this spec
argued by reading source: **the node mapping alone would not have fixed this.**
A flawless `COMFYUI_WORKFLOW_NODES` with this key empty submits an empty
checkpoint name.

The measurement also settled the payload shape. Had `ImagesConfig` been the
nested form, every flat key lookup would have missed and all eight keys would
have read as differing; five matched by flat name. The shape is flat, as
`routers/images.py` at the pinned revision `01f4282f` says.

**Nothing was overridden in the database beforehand, so the environment path
was viable for the five scalars** — but not for the three that mattered, and
never for the workflow, which cannot go on a systemd `Environment=` line. The
push then wrote rows for the whole subtree, which is what retires the
`gpu_host_online` gate described above.

### Step 1 — make it work with the checkpoint already present

`sd_xl_base_1.0.safetensors` is the only checkpoint on the host, confirmed by
probing `/object_info/CheckpointLoaderSimple` on 2026-08-13. Build the catalog,
`sdxl.json`, the mapping, the validator, the push tool and the check against it.
No downloads, no new nodes: if no image appears, the mapping is the only
suspect.

### Step 2 — Pony Diffusion V6 XL

Download to `models/checkpoints/` and verify SHA256
`614f55e8bd8701b9168957361a00c7a76c5de1aa625ade08edfca3db2675b2cc` with
`Get-FileHash` before wiring anything. Add `pony.json`, flip `image_workflow`
and `IMAGE_GENERATION_MODEL`, push, re-run the check.

The score-tag problem is solved in the workflow file, not the mapping. Pony was
trained with `score_9, score_8_up, score_7_up` and degrades visibly without
them, but Open WebUI overwrites the positive prompt node wholesale on every
request. So: a second fixed `CLIPTextEncode` holding the tags, a
`ConditioningConcat` merging it with the user's prompt node, and the `prompt`
mapping pointed at the user node only.

**This creates an authoring constraint.** Because the validator requires every
mapped node ID to exist in every committed workflow, `sdxl.json` and `pony.json`
must use the **same node IDs** for the mapped nodes. That is a small discipline
when hand-editing an exported workflow, and it is the price of the guarantee
that switching `image_workflow` can never be the step that discovers a broken
mapping.

**Available strengthening at this step:** after generating, read ComfyUI's most
recent `/history` entry and confirm the executed graph carried the expected
`ckpt_name`. That upgrades "our workflow ran" from a dimension proxy to a direct
read. History is in-memory and the read races with concurrent use, so it is a
secondary assertion; the dimension check stays primary.

## Verification

Per `CLAUDE.md`, a green container and a 200 prove a process started, not that a
service functions. The claim "image generation works" is established by
`make image-gen-check` returning `ok` — a real PNG at 1024×1024, fetched and
inspected — and by nothing weaker.

The deploy guarantee is unaffected: no play or role reads `images.yml` or the
workflow files, so `make infra` reports the three git-archive-sync tasks and
nothing else. The one exception is the token file added to `roles/svc_infra`,
which is static and settles after its first deploy.

## What this buys the next two features

Choosing the API push turns out to buy more than it cost.

**Image editing needs no new mechanism at all.** Every `IMAGES_EDIT_COMFYUI_*`
key lives in the same `ImagesConfig` object, so editing is more managed keys in
`images.yml`, an edit workflow file, and the `image`-node rule inverted
(required there, forbidden in generation). The push tool, the validator's
structure and the check's shape all carry over unchanged.

**Video is genuinely separate.** MiniMax H3 is not in `ImagesConfig`, almost
certainly needs its own runtime, and has unmeasured VRAM against a card that
holds one 17–21 GB chat model with roughly 3.5–4.5 GiB spare. Nothing here helps
or hinders it. The only obligation was to avoid painting into a corner, and
workflow-format specifics stay confined to the workflow files.

## Risks

- ~~**The measurement in Step 0 may find no rows**~~ — **resolved 2026-08-20.**
  Nothing had been overridden in the admin UI, so environment variables were in
  effect for the five scalar keys. They were still not sufficient: the two keys
  that were wrong are the two the environment had never been given, and the
  workflow itself cannot travel on a systemd `Environment=` line. The tooling
  earns its place.
- **A future admin-UI save silently overwrites the pushed configuration.**
  Nothing prevents it. The nightly check catches the effect rather than the
  cause, which is the honest guard. A config-drift comparison belongs with the
  settings-as-code exporter, not here.
- **Unattended generation against a resident 17 GB chat model has never run.**
  SDXL pages weights against system RAM, which is why hand-submitted generation
  works today with ~3.6 GiB free. Nightly-while-something-else-is-resident is
  untested. An OOM must produce `broken`, not a stack trace.
- **An Open WebUI image bump could change `ImagesConfig`'s shape.**
  Read-modify-write degrades well: new fields are echoed back untouched, and a
  removed field 422s loudly rather than silently.
- **Pony's provenance is a third-party mirror.** CivitAI requires an API token
  for downloads, so `LyliaEngine/Pony_Diffusion_V6_XL` is unaffiliated.
  `.safetensors` is data-only and cannot execute on load, and the checksum pins
  exactly which bytes were reviewed. The plan page's reasoning, carried forward
  unchanged.
- ~~**The WebSocket path is newly documented and previously unverified.**~~ —
  **verified 2026-08-20.** Generation completed in 10.5s and returned a real
  1024x1024 PNG, which is only reachable through `_ws_get_images`; ComfyUI's
  `/history` holds the matching entry. The `ws://` upgrade to the GPU host
  works.

## Out of scope

- **Image editing** (`IMAGES_EDIT_COMFYUI_*`) — next, and cheap given this.
- **MiniMax H3 video generation** — needs its own VRAM measurement, runtime
  decision, and a storage and retention story that does not exist.
- **Chroma1-HD** — Flux-architecture, ~13.4 GB fp8, cannot coexist with a
  resident chat model and cannot share a workflow with SDXL. Unchanged from the
  plan page: if it returns, it runs directly against ComfyUI with the chat model
  stopped.
- **A shim between Open WebUI and ComfyUI** that would allow per-request
  switching between architectures. Considered and rejected: a bespoke service to
  maintain forever, for a constraint nothing in scope actually hits. Worth
  revisiting only if editing or video make the single-workflow limit
  intolerable.
- **Open WebUI settings-as-code generally** — tracked separately in
  [openwebui-settings-as-code.md](../../plans/openwebui-settings-as-code.md).

[comfyui-src]: https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/images/comfyui.py
[images-src]: https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/images.py
