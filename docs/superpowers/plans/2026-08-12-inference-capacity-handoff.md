# Handoff: finishing the inference capacity and roster work

**Written 2026-08-12 from TERRA (the Windows GPU host).**
**Branch: `docs/inference-capacity-roster`, 20 commits, pushed to `origin`, tree clean.**

Everything that could be done on TERRA is done. What remains needs a machine
that can run Ansible and reach svc-infra over SSH, which TERRA cannot — see
[Why TERRA stopped](#why-terra-could-not-finish).

Pick this up with:

```bash
git fetch origin && git switch docs/inference-capacity-roster
```

The spec is
[`docs/superpowers/specs/2026-08-11-inference-capacity-and-roster-design.md`](../specs/2026-08-11-inference-capacity-and-roster-design.md)
and the task-by-task plan is
[`docs/superpowers/plans/2026-08-11-inference-capacity-and-roster.md`](2026-08-11-inference-capacity-and-roster.md).
Tasks 1–5 are complete. Task 6 is complete except its live steps; Task 7 is
untouched.

---

## Read this first: the deploy surface here is almost nothing

That is unusual for this repo and it is the fact that should shape your
expectations.

**No play or role reads anything this branch adds.** Verified:

```bash
grep -rn "model_roster" --include="*.yml" --include="*.j2" roles/ site.yml verify.yml
# no matches
```

`inventory/group_vars/all/models.yml` is a catalog consumed only by
`tests/validate_model_roster.py` and `scripts/roster_reconcile.py`. Nothing
renders it into a Quadlet, a template or a unit. The other changes are five
scripts, five docs, two tests and one Makefile target.

**So `make infra` should report the three git-archive-sync tasks and nothing
else.** Per `CLAUDE.md`, the first deploy after any commit reports `changed=3`
on svc-infra (the runner rebuilds `/opt/homelab-iac` and records the new
revision), then `changed=0` on a second run. **If anything beyond those three
changes, stop and explain it before merging** — on this branch there is no
legitimate fourth change.

---

## What is already true (done on TERRA, no action needed)

### The GPU host is on quantized KV cache

`OLLAMA_KV_CACHE_TYPE=q8_0` with `OLLAMA_FLASH_ATTENTION=1`, adopted on
measured evidence and **verified surviving a full reboot**. Read it back with:

```powershell
Select-String 'server config' "$env:LOCALAPPDATA\Ollama\server.log" | Select-Object -Last 1
```

Require `OLLAMA_FLASH_ATTENTION:true` and `OLLAMA_KV_CACHE_TYPE:q8_0`.

The payoff: `huihui_ai/gemma-4-abliterated:31b` measures `SPILLED` at 32768
under `f16` and `MEASURED` under `q8_0`, so **its `num_ctx` cap is gone**.
`models.yml` records that this depends on the host setting — if the cache ever
returns to `f16`, the 16384 cap must return with it.

### Eleven models, each measured, each with a written reason

`inventory/group_vars/all/models.yml` is the catalog, gated by
`tests/validate_model_roster.py` inside `make validate`. Every figure comes
from the same q8_0 pass of 2026-08-12; `docs/gpu-capacity.md` is generated and
carries the f16/q8_0/q4_0 comparison.

New since `main`: `muse-glimmer:30b` (vision, aligned),
`huihui_ai/qwen3-vl-abliterated:8b` (vision, uncensored) and
`huihui_ai/qwen3-coder-abliterated:30b-a3b-instruct-q4_K_M` (uncensored
coding, which fills the gap `chat-models.md` had carried since `aratan`).

### Three controls, all green on TERRA as of 2026-08-12

```bash
scripts/abliteration_control.py --host http://localhost:11434 --roster    # 5/5 ANSWERED
scripts/vision_control.py       --host http://localhost:11434 --roster    # 2/2 SEES
scripts/abliteration_control.py --host http://localhost:11434 --baseline  # REFUSED
```

The baseline is the one that matters: every other check would look identical if
it had quietly stopped working, and that one must come back `REFUSED` or the
whole abliteration story is unverified.

Each script also has `--self-check`, which proves its own verdict logic still
fires without touching a GPU. Run those anywhere.

---

## What remains

### 1. Live roster reconciliation — needs svc-infra

`scripts/roster_reconcile.py` compares the catalog against Ollama's `/api/tags`
and Open WebUI's `model` table. Run it where `webui.db` lives:

```bash
make roster-check
# or: scripts/roster_reconcile.py --webui-db /opt/homelab/appdata/open-webui/webui.db
# DB path overridable: make roster-check DB=/some/other/webui.db
```

**The success condition is that it FINDS something.** `chat-models.md` records
that `aratan/qwen3.6-claude-coder-35b-A3b-mlx-Q4KM-abliterated` was deleted
from Ollama on 2026-08-10 and its row is still live in Open WebUI with
`is_active = 1`. The first run must report that as `BROKEN`. **If it reports
`OK`, the query is wrong** — do not treat a clean first run as good news.

Then deactivate that row in Open WebUI's admin UI, re-run until clean, and
**correct `docs/chat-models.md`**, which currently states the row is still
live. That sentence becomes false the moment you clear it.

### 2. Tell Open WebUI about the four new models

`ENABLE_PERSISTENT_CONFIG` is `"true"`, so this is a UI action, not a deploy.
The models exist on the GPU host but will not appear in the dropdown until
Open WebUI knows them. Confirm each returns a real reply at
`https://chat.fortwow.dev` — a green container proves nothing.

### 3. Deploy and merge (Task 7)

```bash
make validate            # full gate; see below for what TERRA could not run
make infra               # expect changed=3 (git archive sync)
make infra               # expect changed=0 — required before merging
make verify
git status --porcelain   # must print nothing
git switch main && git merge --ff-only docs/inference-capacity-roster && git push
git branch -d docs/inference-capacity-roster
git push origin --delete docs/inference-capacity-roster
```

Deleting the branch is step 8 of the workflow and the step this repo skipped
for its first 75 commits.

### 4. Verify the firewall fix from a tailnet peer

The two permissive `ollama.exe` rules (`Remote: Any`, Private + Public) were
disabled by hand on 2026-08-12 after an Ollama upgrade found them live again.
Confirmed from TERRA: they read `enabled=False` and the LAN path still answers.
**The closed path is unproven** — that needs a peer:

```bash
curl -m 6 http://100.107.5.66:11434/api/tags   # must now FAIL
curl -m 6 http://192.168.1.40:11434/api/tags   # must still return the model list
```

Both halves. The first alone cannot distinguish a narrowed scope from a dead
service. Active peers at time of writing: `brandons-macbook-pro`
(100.110.75.114) and `edgar` (100.93.219.13) — both on the LAN, so the airtight
version is a peer on cellular.

---

## Why TERRA could not finish

- **Ansible does not support Windows as a control node** (needs POSIX `fcntl`,
  `pwd`). No pip install fixes it; WSL is the documented route and this account
  is not an administrator, so `wsl --install` cannot run.
- **The SSH key on TERRA is not authorized on svc-infra**
  (`straderb@192.168.1.32` → permission denied), so the reconciliation cannot
  read `webui.db`.

**What TERRA did run**, so you know what is already covered: all six Python
catalog validators, all five script self-checks, and `tests/validate_links.py`.
**Not covered:** `ansible --syntax-check`, `ansible-lint`, `shellcheck`,
`gitleaks`, `systemd-analyze verify`. Those fire for the first time in your
`make validate`.

---

## Traps found the hard way — do not rediscover these

**Setting `OLLAMA_KV_CACHE_TYPE` is not the same as it taking effect.** Windows
builds a process environment at launch, so a registry write does not reach
Ollama if it is started from a shell that predates it. Worse, **an Ollama
upgrade silently reverts it** — the installer relaunches from its own
environment. Always read it back from `server.log`. A pass run against a server
that never received the variable looks identical to a card that cannot do
quantized cache: everything `FALLBACK`, and the wrong conclusion recorded as
fact.

**`llama-server` on TERRA is Ollama's own inference runner, not a foreign
process.** It was misidentified as a separate llama.cpp server and killed,
which took the inference backend down. If it appears to be holding VRAM while
`ollama ps` shows nothing loaded, that is an **orphaned runner** — restart
Ollama, do not kill it. `scripts/vram_survey.py` now prints the holder's full
path on abort so this is obvious.

**`ollama ps` can report nothing resident while a runner still pins ~20 GB.**
That matters because `gpu-host.md` tells you to trust `ollama ps` for spill
detection.

**`ollama pull` failures are easy to mask.** Piping it (`| tail`) reports the
pipe's exit code, not Ollama's. Check `$?` on the pull itself.

**Reasoning models return an empty `response` with a small `num_predict`** —
the whole budget goes to a separate `thinking` field. Budget ~1200. This bit
both control scripts and is encoded in their self-checks now.

**An abliterated Muse Glimmer was tried and rejected.** It fits the card
comfortably but emits the literal string ` to=self` and stops after three
tokens for every prompt. Full reasoning in `docs/chat-models.md`. The weights
are still on disk; re-check when llama.cpp support lands upstream. Note it also
initially *passed* the abliteration control, which is why that script gained a
minimum-answer-length rule and a self-check table.

---

## Wanted next, already written down — not part of this branch

These were requested during this work and deliberately not started. They are in
git so they survive this machine.

- **Image and video generation: MiniMax H3, Qwen Image Edit.** See the
  [requested-but-deferred section](../../plans/uncensored-image-generation.md#requested-but-deferred-minimax-h3-and-qwen-image-edit).
  Both are blocked behind the same thing: **in-chat image generation has never
  produced an image**, because `COMFYUI_WORKFLOW_NODES` is unset. Fix that
  first; a better checkpoint behind a broken mapping produces the same nothing.
  Suggested order is on that page — Qwen Image Edit before H3, since editing
  uses a separate config surface and may be independent of the broken path.

- **An abliterated Muse Glimmer.** Rejected on 2026-08-12 for emitting
  ` to=self` and stopping after three tokens under Ollama 0.32.9, despite
  fitting the card comfortably. Reasoning and numbers are in
  [`docs/chat-models.md`](../../chat-models.md). The weights are on disk;
  re-check when llama.cpp support lands upstream and Ollama vendors it. Note
  that stock `muse-glimmer:30b` **refuses** the control prompt, so it is a
  second aligned baseline alongside `qwen3:30b` — replacing it with an
  abliterated build rather than adding one would give that up.

- **The always-on tier.** The two-tier contract in the spec assumes a second
  host for models that must answer when TERRA is gaming or off. The intended
  machine is a headless 16 GB M1 Pro MacBook Pro; at ~11 GB usable it can hold
  a small tool-calling model and an embedder, but **not** a 26 GB-class chat
  model. Until it exists, the `mbp` tier in `models.yml` has no entries and
  Home Assistant and RAG embedding have nowhere to land.

## Repo rules that apply to the rest of this

- **Never `git add -A`.** The repo root holds gitignored working notes quoting
  live credentials. Stage explicit paths.
- **`vault.yml` is gitignored**, so a clean tree does not fully describe the
  deployed state. Rebuilding from a bare clone needs the vault out of band.
- **Commit before the final deploy**, so `changed=0` against a clean tree is
  proof that what runs equals what is committed.
- A `backup/pre-msg-rewrite` tag exists locally on TERRA only. Three commit
  messages were rewritten to strip a stray `@` subject line; the branch was
  force-pushed once, on 2026-08-12. If you already had it checked out
  elsewhere, re-fetch rather than merging.
