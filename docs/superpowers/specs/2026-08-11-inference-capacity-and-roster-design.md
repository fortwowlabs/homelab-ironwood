# Inference capacity and the model roster

Designed 2026-08-11. Nothing here is implemented yet.

This is the first of several pieces split out of a broader "expand local AI"
request. It exists because every other piece — RAG, Home Assistant, image
generation — turned out to depend on one unanswered question: **how much of the
4090 is actually available, and to what.**

## The problem in one paragraph

TERRA's RTX 4090 is a box with 24 GB of RAM and no swap. About 2 GB is held by
the desktop, so call it 22 GB usable. A model is a 17–21 GB file that must be
resident to run at full speed, so the card holds exactly one, and switching
costs 20–30 seconds. When you exceed the limit **nothing fails** — Ollama
spills layers to system RAM and generation slows by roughly an order of
magnitude, with no error, no log line and no alert. `ollama ps` is the only
place it is visible, and it is the equivalent of having to run `free -m` by
hand to discover you are swapping.

Every capacity table in this repo was produced by hand against that constraint,
and there are now two of them ([`gpu-host.md`](../../gpu-host.md),
[`chat-models.md`](../../chat-models.md)) that already disagree in detail.

## Scope

**In:**

1. A measurement pass on TERRA producing real numbers, including a
   memory-saving option nobody here has tested.
2. Two roster additions — an uncensored coding model and a vision model —
   added or rejected on evidence, against rules written down in advance.
3. The roster recorded in git as data, replacing the hand-maintained tables.

**Out, with reasons:**

| Deferred | Why |
|---|---|
| Provisioning the M1 Pro MBP as the always-on host | The machine is not set up. Recorded in the working TODO list. |
| The RAG pipeline | Its own project. This spec only decides *where the reranker runs*, not how RAG works. |
| Home Assistant integration | Depends on the MBP existing. |
| Image generation | Already has [`plans/uncensored-image-generation.md`](../../plans/uncensored-image-generation.md). |
| Consolidating the four overlapping chat models | Considered and declined. They differ by temperament and disk is not scarce. |

## The two-tier contract

Recorded here so that the RAG and Home Assistant specs do not each invent their
own topology and invent it differently.

| | **TERRA** (RTX 4090, 24 GB) | **MBP** (M1 Pro, 16 GB → ~11 GB usable) |
|---|---|---|
| Up when | at the desk, not gaming | always |
| Holds | one 17–21 GB model at a time | several small models at once |
| Serves | Open WebUI chat, Continue, vision, image generation | Home Assistant, RAG embedding |
| Ansible-managed | **no** — it is a desktop that gets gamed on | open, decided in its own spec |
| Known to the repo as | `gpu_host_ip` + `gpu_host_online` | same pattern, a new pair of variables |

Two rules follow, and both constrain later work:

**Nothing that must answer reliably may depend on TERRA.** It is a gaming PC
that gets rebooted and switched off. A voice assistant that stops working when
a game launches fails unpredictably, which is worse than not existing.

**Neither host can cover for the other.** ~11 GB cannot hold a 26 GB-class chat
model, and TERRA cannot be always-on. This is two machines doing two different
jobs, not redundancy.

### The honest gap

Until the MBP is set up, **the always-on tier does not exist.** RAG embedding
and Home Assistant either run on TERRA and inherit its uptime, or they are not
built. This spec does not pretend otherwise; it defines the slot so those
projects have somewhere to land.

---

# Part 1 — The measurement pass

## The untested lever

Loading a model allocates two separate things:

| | What it is | Sized by |
|---|---|---|
| Weights | the model file | which model |
| Context cache | scratch space for the conversation | how many tokens of history |

[`chat-models.md`](../../chat-models.md) already established that the `31b`
model's spill was the *second* one — the weights fit, the scratch space pushed
it over — and fixed it by halving the conversation length from 32768 to 16384.

There is another option that was never tried: **store the context cache at
lower precision.** `q8_0` uses about half the memory of the `f16` default,
`q4_0` about a quarter. Same conversation length, less memory. If it works on
this card, every model on the box gets several GB back, and that number decides
whether the new models coexist or merely join the swap queue.

## Constraint: the setting is global

`OLLAMA_KV_CACHE_TYPE` is a **server-wide environment variable**, exactly like
`OLLAMA_HOST` already is on TERRA. There is no per-model or per-request
override. It also requires `OLLAMA_FLASH_ATTENTION=1` to take effect at all.

Two consequences:

- **Measuring it means restarting the Ollama service** between passes. Three
  passes: `f16` (today's state), `q8_0`, `q4_0`.
- **It is one decision for the whole host.** The coding model cannot run at
  full precision while chat runs compressed. If you compress, everything is
  compressed — including the coder, where precision plausibly matters most.

## It fails silently, and that gives us a free control

Upstream documentation states plainly that on architectures which do not
support a quantized cache, it **falls back to `f16` without telling you**. The
variable is set, the service restarts, models load and answer, and nothing
changed. No error, no log line.

That is the failure mode this repo writes warnings about everywhere else, and
here it comes with its own detector: **if compression took effect, the same
model at the same context length must use measurably less memory.** If the
number does not move, the setting did nothing. The script therefore reports
`FALLBACK` rather than a number, and the positive control is built into the
measurement instead of bolted alongside it.

## The script

`scripts/vram_survey.py`, following the pattern
[`scripts/abliteration_control.py`](../../../scripts/abliteration_control.py)
established — Python, talks to Ollama over HTTP, runs on TERRA.

For each model at each context length it:

1. Loads the model with that context.
2. **Sends one short generation.** A model that loads can still fail at 32768,
   and the cache must be genuinely allocated rather than merely reserved.
3. Reads whole-card usage from `nvidia-smi` and the CPU/GPU split from Ollama.
4. Unloads before the next measurement.

Grid: **7 generative models × 2 contexts (16384, 32768) × 3 cache types** — 42
measurements, 35–45 minutes, plus two service restarts. One session.

**`nomic-embed-text` is not in that grid.** It is an embedding model with no
generation endpoint, so step 2 above would fail against it and a context sweep
is meaningless for it. It is measured once per cache-type pass, load-and-read
only. Writing the grid as "8 models" would have produced a script that errors
on the eighth.

**The two candidate models are measured the same way before the Part 2 rules
are applied to them.** The rules are thresholds against measurements, so the
survey has to cover the candidates, not only what is already installed — pull,
measure, then decide, and uninstall if the rule rejects them.

`nvidia-smi` delta against the idle baseline is the measurement of record,
because it is guaranteed available. Ollama's `/api/ps` may also expose a
`size_vram` field, which would be cleaner than parsing the `ollama ps` column
layout; whether it does is for the implementation to determine on first
contact, not to assume here.

## Three rules it enforces, each from a mistake already made

**It refuses to run unless the card is idle.** [`gpu-host.md`](../../gpu-host.md)
records the first version of its table being taken while a game held VRAM,
invalidating it on its face. So the script reads the baseline before anything
else and **aborts** if the card is not idle. A survey you cannot trust is worse
than no survey, because it looks the same as one you can.

The threshold needs stating precisely, because the two existing docs disagree
about what idle *is*: `gpu-host.md` says "~2.5 GB" and `chat-models.md` says
"an idle card (1920 MiB baseline)". **Use 2560 MiB as the abort threshold** —
above the observed 1920 MiB idle by a workable margin, below anything a game
would hold. The measured baseline is written into the output either way, so a
future reader can judge the run rather than trusting the threshold. Reconciling
the two docs is a side-effect of Part 1 replacing both tables.

**Three outcomes, not two:** `MEASURED`, `SPILLED`, `INCONCLUSIVE`. Same
tri-state reasoning as the scan probes in `CLAUDE.md`. A model that timed out
is not a model that fits.

**The idle baseline is written into the output.** Every row is meaningless
without it, and the existing tables demonstrate that it gets forgotten.

## Output

One generated file, `docs/gpu-capacity.md`, carrying the date, the idle
baseline and the full table.

[`gpu-host.md`](../../gpu-host.md) and [`chat-models.md`](../../chat-models.md)
then **stop carrying their own copies and link to it.** That is the real fix
for two tables disagreeing: not reconciling them, but removing the duplication
so there is one place to be wrong.

## What this cannot tell you

It measures **whether a model fits**. It says nothing about whether a
compressed cache made the model *worse*. Memory is easy to measure; quality is
not. `q8_0` is widely reported as having no noticeable impact and `q4_0` as
"small-medium" loss, but if the numbers make `q4_0` tempting, that is a
judgement made by using the thing. This survey cannot settle it, and should not
be quoted as though it had.

---

# Part 2 — Roster decisions

## Rules written before the numbers

Deciding the threshold in advance is what prevents "well, 23 GB is nearly
fine", which is how a 35 GB coder stayed installed long enough to be measured
three times.

| Model | Size | Rule to add it |
|---|---|---|
| `huihui_ai/qwen3-coder-abliterated:30b-a3b-instruct-q4_K_M` | 19 GB | 100% GPU at 16384 context or better, **and** passes the abliteration control. |
| `huihui_ai/qwen3-vl-abliterated`, 8B | ~6 GB | 100% GPU, **and** reads text off a real image correctly. |

If either fails, it is not installed, and the reason is recorded the way
`aratan` was — a rejected model with a written reason is more useful than a
silent absence.

### The coder fills a gap this repo already documented

[`chat-models.md`](../../chat-models.md) states that uncensored coding is
unserved since `aratan/qwen3.6-claude-coder-35b` was removed. That model failed
because its weights genuinely exceeded the card at any context. This one is a
different architecture at a smaller size — 19 GB, and a mixture-of-experts
design with about 3B parameters active per token, which is why it is both large
on disk and fast in use. 19 GB is under the 21 GB practical ceiling the
existing table measured.

It is abliterated, so it joins `abliteration_control.py --roster`. It does not
count as installed until it answers the lockpicking prompt.

### The vision model, and the trap that will catch it

The 8B is chosen over the 32B deliberately. Reading documents is the use case —
receipts, scanned pages, screenshots — and the 8B scores 96.1 on DocVQA,
ahead of Gemma 3 at every size including 27B. It is also ~6 GB, the only model
in this roster small enough that it might sit *alongside* a chat model rather
than evicting it. The 32B would consume the whole card to do the same job
marginally better.

**It must be Qwen3-VL, not Qwen3.6.** Ollama runs Qwen 3.6 as a text model
without wiring up its vision sidecar. This matters far more than it sounds,
because:

> A vision model whose image half did not load answers text questions
> perfectly. It looks entirely healthy and fails only when sent a picture.

So the acceptance check cannot be "it loaded" or "it replied". It must be **an
image containing text whose answer is known in advance**, asserting that the
text comes back. Same discipline as every other check in this repo: a model
that loaded is not a model that can see.

## What stays

No model is removed. In particular `qwen3:30b` stays — it is the only aligned
model on the host and therefore the only thing that can calibrate the
abliteration control, as [`chat-models.md`](../../chat-models.md) already
argues. The four overlapping chat models stay as well; consolidation was
considered and declined.

---

# Part 3 — The roster as data

## The file

`inventory/group_vars/all/models.yml`, following the catalog pattern already
used by `apps.yml` and `infra-apps.yml`. Per model: name, host tier, role,
whether it is abliterated **on purpose**, measured footprint, context cap, and
why it is there.

## The offline validator

`tests/validate_model_roster.py`, wired into `make validate`. It checks schema
and, importantly, that deliberate exceptions are *stated*: `qwen3-coder` being
un-abliterated and `qwen3:30b` being aligned are both decisions, and a catalog
that cannot distinguish a decision from an oversight is not worth maintaining.

It cannot check whether a model is installed. That is not an offline question.

## The check that earns the file

A three-way reconciliation against the live estate — the catalog, Ollama's
`/api/tags`, and Open WebUI's `model` table:

| Mismatch | Means |
|---|---|
| in git, not in Ollama | declared but not installed |
| in Ollama, not in git | undeclared drift |
| **in Open WebUI, not in Ollama** | **selectable in the dropdown, fails at generation** |

The last row is not hypothetical. [`chat-models.md`](../../chat-models.md)
records that `aratan` was deleted from Ollama and its row is **still live in
Open WebUI today**, `is_active = 1`, because Open WebUI's model list is its own
table rather than a view over Ollama. A user who picks it gets a failure at
generation time rather than an absence in the dropdown, and nothing in the
estate would ever have reported it.

---

# The reranker: placement decided, implementation deferred

A reranker re-sorts RAG search results by actual relevance before the chat
model sees them. It is usually the difference between RAG that answers and RAG
that returns three irrelevant paragraphs.

**It does not run on TERRA, and it is not a roster item.** Open WebUI does not
support Ollama models for reranking; its retrieval router loads cross-encoders
from `sentence-transformers` **inside the Open WebUI container**, or calls an
external HTTP engine. On svc-infra, which has no GPU, that means **CPU inside
the container** — the GPU host is not in the path at all.

Configuration would be one setting, `RAG_RERANKING_MODEL`, pointing at
`BAAI/bge-reranker-v2-m3`.

Recording the placement here and building it in the RAG project, because three
of its properties belong to that project rather than this one:

- it consumes svc-infra CPU on every RAG query, on 6 vCPUs already shared with
  Open WebUI, Prometheus, Grafana and Authelia;
- it downloads roughly 2 GB into the container at first use — more state
  outside git, and a runtime internet dependency;
- `ENABLE_PERSISTENT_CONFIG: "true"` means setting it in Ansible only *seeds*
  it, the trap already documented at length in `infra-apps.yml`.

An external rerank service is the obvious eventual resident of the MBP tier,
but that cannot be designed before the MBP exists.

---

# Risks

**Cache compression is all-or-nothing.** If `q8_0` helps chat but hurts coding
output, there is no split available. One setting serves the whole host.

**The vision model may not coexist after all.** ~6 GB alongside a 17 GB chat
model is plausible only if compression frees real headroom. If it does not, the
vision model is simply another 20–30 second swap and the "small enough to sit
alongside" argument evaporates. That does not make it not worth having; it
makes one of its selling points false, which should be recorded rather than
quietly dropped.

**Compression may silently do nothing**, which is why the survey detects it
rather than assuming it.

**New models must be told to Open WebUI**, and because persistent config is on,
that is a UI action rather than a deploy. It is therefore state outside git, in
the same category as the personas.

**The survey needs Python on TERRA.** The working notes record 11 Python
validators running there after `pip install --user`, so this is expected to
work, but `gpu-host.md` states there is no Python on that machine's `PATH`.
Those two claims need reconciling before the session, not during it.

---

# Verification

Per this repo's standing rule, a green process proves nothing. What must be
demonstrated:

| Claim | Evidence required |
|---|---|
| The survey ran | Output file exists, carries an idle baseline, and no row reads `INCONCLUSIVE` unexplained |
| Compression took effect | Same model, same context, measurably less memory than the `f16` pass — otherwise `FALLBACK` |
| The coder is installed and uncensored | `abliteration_control.py --roster` exits 0 with it included |
| The vision model can see | Returns known text from a known image |
| The catalog is honest | Three-way reconciliation reports no unexplained mismatch — including clearing the stale `aratan` row |
| Nothing regressed | `make validate`, then `make infra` reporting `changed=0` on the second run |

## Follow-on work this unblocks

- Provisioning the MBP as the always-on host (its own spec).
- The RAG pipeline, which now has a decided embedding tier and reranker
  placement.
- Home Assistant integration, which needs the MBP first.
