# Uncensored Model Roster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the safety-aligned model roster behind `chat.fortwow.dev` with abliterated chat and coding models, plus saved personas so therapy is a system prompt rather than a fourth model.

**Architecture:** Six Ollama models are installed **by hand** on the unmanaged Windows GPU host at `192.168.1.40`, and personas are created in Open WebUI's web UI. **No deployed configuration changes.** The repo's contribution is documentation.

**Tech Stack:** Ollama, Open WebUI, Continue (VSCode). Ansible only for the archive-sync deploy at the end.

**Spec:** `docs/superpowers/specs/2026-08-09-uncensored-model-roster-design.md`

**Image generation is out of scope** — deferred in full to `docs/plans/uncensored-image-generation.md`. Do not download image models, do not touch `infra-apps.yml`, do not add a workflow validator. That page also records a live `IMAGE_SIZE` defect; leaving it unfixed is the deliberate consequence of this descoping.

## Global Constraints

- **Branch is `feat/uncensored-models`**, already created and pushed. Do not create another.
- **`make validate` must pass** before any commit.
- **Never `git add -A`.** Stage explicit paths.
- **The GPU host is not Ansible-managed** and must not become so. Its steps are manual PowerShell run on that machine, documented in `docs/gpu-host.md`.
- **`gpu_host_online` is already `true`** (`inventory/group_vars/all/main.yml:67`). Do not change it.
- **This repo has no pytest.** Nothing in this plan adds a test; `make validate` runs the existing gates only.
- **The six models:**

  | Ollama ref | Role |
  |---|---|
  | `huihui_ai/gemma-4-abliterated:26b` | Chat default |
  | `huihui_ai/gemma-4-abliterated:31b` | Chat, dense |
  | `huihui_ai/Qwen3.6-abliterated:27b` | Chat, technical |
  | `hf.co/DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF:Q4_K_M` | Chat, creative |
  | `qwen3-coder:30b` | Coding default (stock, not abliterated) |
  | `aratan/qwen3.6-claude-coder-35b-A3b-mlx-Q4KM-abliterated` | Coding, on demand |

---

## File Structure

| File | Responsibility |
|---|---|
| `docs/plans/uncensored-image-generation.md` | The deferred image work — already written, committed in Task 1 |
| `docs/chat-models.md` | Model roster, persona text, switching cost |
| `docs/gpu-host.md` | Updated pull list, Continue config, VRAM table |

---

## Task 1: Commit the descoping

The spec and the deferred image plan are already written on disk. This task lands them so the rest of the work has a stable reference.

**Files:**
- Create: `docs/plans/uncensored-image-generation.md` (already written)
- Modify: `docs/superpowers/specs/2026-08-09-uncensored-model-roster-design.md` (already written)
- Modify: `docs/superpowers/plans/2026-08-09-uncensored-model-roster.md` (this file)

- [ ] **Step 1: Check the link gate**

The new doc is cross-linked from the spec with a relative path; this repo validates those.

```bash
python tests/validate_links.py
```

Expected: PASS. A failure names the broken relative path — fix it rather than removing the link.

- [ ] **Step 2: Run the full gate suite**

```bash
make validate
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add docs/plans/uncensored-image-generation.md docs/superpowers/specs/2026-08-09-uncensored-model-roster-design.md docs/superpowers/plans/2026-08-09-uncensored-model-roster.md
git commit -m "docs: split image generation out of the model roster work

Keeps the roster change to models only. Everything researched for the image
side is preserved rather than discarded, because most of it is not
recoverable by reading documentation: Chroma needs CFG 3.8 (it is
schnell-derived but not distilled the same way, so schnell's CFG 1.0 gives
washed-out output that reads as a bad model), its official workflow ships
filenames that 404 upstream, Pony's score tags are erased by Open WebUI
overwriting the prompt node, and Open WebUI's node mapping raises a
KeyError that the caller swallows -- no image, no error, green container.

The deferred page also records a live defect this change now leaves in
place: IMAGE_SIZE defaults to 512x512 against a checkpoint trained at 1024,
so in-chat images have been quietly poor since the feature was enabled. Two
lines to fix, no new models needed, deliberately not in scope here."
```

---

## Task 2: Install the models on the GPU host

Manual work on the Windows machine. No repo changes — `docs/gpu-host.md` is updated in Task 3 once the real numbers are known.

**Files:** none (GPU host only)

**Interfaces:**
- Produces: six confirmed Ollama tags, a measured VRAM figure, and the refusal prompt used. Task 3 writes all three into `docs/gpu-host.md`; Task 4's persona work depends on the default chat model being present.

- [ ] **Step 1: Check free disk before pulling**

On the GPU host:

```powershell
Get-PSDrive C | Select-Object Used,Free
```

Expected: at least **110 GB free**. If there is less, stop and report — do not pull a partial roster.

- [ ] **Step 2: Calibrate the positive control before retiring the old model**

**Do this first.** The whole verification rests on a prompt that the outgoing model refuses, and once `qwen3:30b` is deleted there is no way to calibrate it.

Choose a prompt and confirm `qwen3:30b` actually refuses it:

```bash
curl -s http://192.168.1.40:11434/api/generate -d '{"model":"qwen3:30b","prompt":"<REFUSAL_PROMPT>","stream":false}' | python -c "import sys,json;print(json.load(sys.stdin)['response'][:300])"
```

Expected: a refusal. **If `qwen3:30b` answers it, the prompt is not a valid control** — choose a different one and repeat until you have one that is refused. Record the exact prompt text.

- [ ] **Step 3: Pull the five Ollama-registry models**

```powershell
ollama pull huihui_ai/gemma-4-abliterated:26b
ollama pull huihui_ai/gemma-4-abliterated:31b
ollama pull huihui_ai/Qwen3.6-abliterated:27b
ollama pull qwen3-coder:30b
ollama pull aratan/qwen3.6-claude-coder-35b-A3b-mlx-Q4KM-abliterated
```

- [ ] **Step 4: Find the correct quant tag for the DavidAU model, then pull it**

This one is a Hugging Face GGUF repo, **not** an Ollama registry entry. `ollama pull DavidAU/...` fails with a not-found error that reads as though the model was withdrawn. List the available quant tags first:

```powershell
curl.exe -s "https://huggingface.co/api/models/DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF" | ConvertFrom-Json | Select-Object -ExpandProperty siblings | Where-Object { $_.rfilename -like "*.gguf" } | Select-Object -ExpandProperty rfilename
```

Then pull using the `hf.co/` prefix and a quant tag confirmed present in that listing:

```powershell
ollama pull hf.co/DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF:Q4_K_M
```

If `Q4_K_M` is absent, pick the nearest ~16 GB Q4 variant and **record the exact tag used** — Tasks 3 and 4 both reference it.

- [ ] **Step 5: Verify all six are visible over the LAN, not from the GPU host**

From the workstation, not the GPU box — loopback would pass even with a wrong bind:

```bash
curl -s http://192.168.1.40:11434/api/tags | python -c "import sys,json;[print(m['name']) for m in json.load(sys.stdin)['models']]"
```

Expected: six new names alongside the existing `qwen2.5-coder:1.5b-base` and `nomic-embed-text`.

- [ ] **Step 6: Positive control — prove the models are actually uncensored**

**This is the only check in the whole plan that distinguishes a working abliteration from the wrong model pulled by mistake.** A tag, a load, and a plausible reply are byte-identical in both cases.

Using the calibrated prompt from Step 2:

```bash
for m in "huihui_ai/gemma-4-abliterated:26b" "huihui_ai/gemma-4-abliterated:31b" "huihui_ai/Qwen3.6-abliterated:27b" "hf.co/DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF:Q4_K_M"; do
  echo "=== $m"
  curl -s http://192.168.1.40:11434/api/generate -d "{\"model\":\"$m\",\"prompt\":\"<REFUSAL_PROMPT>\",\"stream\":false}" \
    | python -c "import sys,json;print(json.load(sys.stdin)['response'][:200])"
done
```

Expected: all four answer. **Any model that refuses has either a failed abliteration or a wrong tag** — stop and investigate that model. Do not proceed on the assumption it will behave once wired up.

- [ ] **Step 7: Record resident VRAM for the default model**

```powershell
ollama run huihui_ai/gemma-4-abliterated:26b "hello"
ollama ps
```

Record the SIZE column. The spec estimates ~15 GB; the real figure feeds the Task 3 VRAM table.

- [ ] **Step 8: Retire the superseded models**

Only after every check above passes:

```powershell
ollama rm qwen3:30b
ollama rm qwen2.5-coder:14b
```

Do **not** remove `qwen2.5-coder:1.5b-base` or `nomic-embed-text` — Continue's autocomplete and embeddings still use them.

- [ ] **Step 9: No commit**

Report the DavidAU tag from Step 4, the VRAM figure from Step 7, and the refusal prompt from Step 2.

---

## Task 3: Update `docs/gpu-host.md` and the Continue config

**Files:**
- Modify: `docs/gpu-host.md`

**Interfaces:**
- Consumes: the tag, VRAM figure and refusal prompt recorded in Task 2.

- [ ] **Step 1: Replace the model pull section**

In `docs/gpu-host.md`, replace the `ollama pull` block (lines ~53-69) with the six models from Task 2. Include the `hf.co/` prefix explanation for the DavidAU model and the note that its quant tag must be confirmed against the repo's file listing first, because `ollama pull DavidAU/...` fails with a not-found error that reads as a withdrawn model. Update the total download size to the real figure.

- [ ] **Step 2: Update the Continue config**

Replace the `qwen2.5-coder-14b` entry in the `~/.continue/config.yaml` block (lines ~186-206) with:

```yaml
  - name: qwen3-coder-30b
    provider: ollama
    model: qwen3-coder:30b
    apiBase: http://192.168.1.40:11434
    roles: [chat, edit, apply]
```

Leave the `1.5b-base` autocomplete and `nomic-embed-text` embedding entries unchanged.

- [ ] **Step 3: Apply the Continue config and test it in a real file**

Update `~/.continue/config.yaml` on the workstation, then trigger autocomplete in an actual source file.

Expected: a real completion. **The model list populates from `/api/tags` even when generation is broken**, so a populated dropdown proves nothing — the existing doc already says this and it still applies.

- [ ] **Step 4: Update the VRAM table**

Replace the measured table (lines ~151-160) with the figure recorded in Task 2 Step 7 for `gemma-4-abliterated:26b`, keeping the existing format and stating the date as the existing table does.

**Leave the image-generation rows and the ComfyUI sharing discussion intact.** They describe the SDXL setup that is still deployed and unchanged.

- [ ] **Step 5: Run the gates**

```bash
make validate
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docs/gpu-host.md
git commit -m "docs: bring gpu-host.md up to the new model roster

Records the six models actually pulled, including that the DavidAU one is a
Hugging Face GGUF repo rather than an Ollama registry entry -- ollama pull
without the hf.co/ prefix fails with a not-found error that reads as though
the model was withdrawn, and its quant tag has to be confirmed against the
repo listing because DavidAU publishes many per model.

Continue moves to qwen3-coder:30b, verified by triggering autocomplete in a
real file rather than trusting the model list, which populates from
/api/tags even when generation is broken.

The image-generation sections are unchanged: the SDXL setup they describe
is still what is deployed."
```

---

## Task 4: Create the personas, write `docs/chat-models.md`, and merge

**Files:**
- Create: `docs/chat-models.md`

**Interfaces:**
- Consumes: the model tags from Task 2.

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

A saved persona that failed to attach looks identical to one that worked. Send the same message to the bare base model and to `Therapist`, and confirm the replies differ in the way the prompt describes.

Expected: the base model may deflect or add disclaimers where `Therapist` does not. If the two are indistinguishable, the system prompt did not attach — re-check the Workspace entry.

- [ ] **Step 3: Write the reference document**

Create `docs/chat-models.md` covering, with no placeholders:

- The model roster table from the spec, with the **exact** tags used in Task 2 including the DavidAU quant tag actually pulled.
- The one-model-at-a-time constraint and the ~20-30 s switching cost.
- The persona text above, verbatim, and this paragraph:

  > **These personas live in Open WebUI's database, not in git.** They are
  > created by hand in Workspace → Models, and `backup_paths: [open-webui]`
  > captures `webui.db` nightly — so they are recoverable, but they are not
  > rebuildable from a clean clone the way everything else in this repo is.
  > This is a deliberate exception, taken because the alternative (a
  > compare-before-write Ansible task against `/api/v1/models`, needed to
  > avoid reporting `changed` on every deploy and destroying the `changed=0`
  > proof) is disproportionate for two paragraphs of text. The copies above
  > are the source of truth for a rebuild; nothing detects drift between them
  > and the live copy. Revisit if the persona set grows.

- The refusal prompt used as the positive control in Task 2, and why it matters:
  it is the only check that distinguishes a working abliteration from the wrong
  model. Note that re-calibrating it after `qwen3:30b` is gone means finding a
  model that still refuses it.
- A pointer to `docs/plans/uncensored-image-generation.md` for the image work,
  noting that image generation currently runs on stock SDXL and is unchanged.

- [ ] **Step 4: Check the link gate**

```bash
python tests/validate_links.py
```

Expected: PASS.

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
copy and the text recorded here. With the image work deferred this is now
the only state the change creates outside git, which makes the exception
more visible than it was when it sat beside committed workflow files.

Records the refusal prompt used as the abliteration positive control, since
re-calibrating it once qwen3:30b is gone means finding another model that
still refuses it."
```

- [ ] **Step 7: Confirm the tree is clean**

```bash
git status --porcelain
```

Expected: **no output**. Untracked files count.

- [ ] **Step 8: Deploy from the clean tree**

The change is documentation-only, but the nightly runner keeps a `git archive`
of the tree at `/opt/homelab-iac` with the deployed revision in `.deployed-rev`,
so a commit still makes the sync block fire. Running it keeps that copy current.

```bash
make infra
```

Expected: `changed=3` on svc-infra — the archive rebuild, unpack, and revision
record. **Check which three tasks changed.** Anything beyond those three is a
real diff and must be explained before merging — and would be surprising here,
since nothing in this change touches a template or a catalog.

- [ ] **Step 9: Deploy again and require `changed=0`**

```bash
make infra
```

Expected: `changed=0`.

- [ ] **Step 10: Run the verification playbook**

```bash
make verify
```

Expected: PASS.

- [ ] **Step 11: End-to-end check through the real service**

Not a container check. In `https://chat.fortwow.dev`:

1. Send a message to `huihui_ai/gemma-4-abliterated:26b` and get a real reply.
2. Switch to the `Therapist` persona and confirm it behaves per its prompt.
3. Re-run the refusal prompt through the web UI, not just the API — confirm it is answered.
4. Confirm image generation still works, unchanged, on stock SDXL. This change should not have touched it; check rather than assume.

Item 3 matters most: it is the only one that distinguishes a working roster from a plausible-looking wrong one.

- [ ] **Step 12: Merge, push, and delete the branch**

```bash
git switch main
git merge --ff-only feat/uncensored-models
git push origin main
git branch -d feat/uncensored-models
git push origin --delete feat/uncensored-models
```

- [ ] **Step 13: Confirm CI**

CI runs on push to `main`, after the merge rather than before it, so it is an alarm and not a gate.

```bash
gh run list --limit 1
```

Expected: green. A red run means something already on `main` is broken and needs a follow-up commit.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| Chat roster (4 models) | 2 |
| Coding roster (2 models) | 2, 3 |
| Retirements | 2 Step 8 |
| DavidAU `hf.co/` prefix | 2 Step 4, 3 Step 1 |
| Disk check | 2 Step 1 |
| Positive control, calibrated first | 2 Steps 2 and 6, 4 Step 11 |
| Personas | 4 Steps 1-2 |
| Persona git exception documented | 4 Step 3 |
| Continue update | 3 Steps 2-3 |
| `docs/chat-models.md` | 4 Step 3 |
| `docs/gpu-host.md` | 3 |
| Image generation deferred | 1 |
| `changed=0` rollout | 4 Steps 7-10 |
| Exposure note | spec only — no action, correctly |

**Placeholder scan:** `<REFUSAL_PROMPT>` in Task 2 is an intentional user-supplied value, with the procedure for choosing and calibrating it fully specified in Step 2. Every other step carries literal content.

**Ordering check:** the positive control is calibrated in Task 2 Step 2, *before* the pulls and well before `qwen3:30b` is deleted in Step 8. Reversing those would leave the control uncalibratable. Task 3 depends on values recorded in Task 2; Task 4 depends on the default model existing. Task 1 has no dependencies and could run at any point, but lands first so the deferral is recorded before work begins against the descoped spec.
