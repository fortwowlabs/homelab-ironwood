# The GPU host (Windows 11 + RTX 4090)

Inference for [Open WebUI](services.md) does not happen on any of the service
VMs. It happens on a Windows 11 workstation with an RTX 4090, running Ollama
and ComfyUI natively.

**This machine is not managed by Ansible and will not be.** It is a desktop
that gets rebooted, gamed on, and turned off; modelling it as infrastructure
would mean pretending otherwise. Everything below is done by hand, once. The
repo's only knowledge of it is two variables in
`inventory/group_vars/all/main.yml`:

| Variable | Meaning |
|---|---|
| `gpu_host_ip` | Its reserved LAN address — `192.168.1.40` |
| `gpu_host_online` | Whether Open WebUI should try to talk to it at all |

## TERRA's addresses

The machine answers on **two** addresses, and only one of them is configured
anywhere in this repo. Both are recorded here because the difference between
them is the difference between an intended path and an open finding, and until
now the tailnet address appeared only inside the incident write-ups further
down — which is the wrong place to look it up.

| Address | What it is | Who can reach it |
|---|---|---|
| `192.168.1.40` | Reserved LAN address (DHCP reservation in pfSense). Held by the **Wi-Fi** adapter, not the wired one. | The LAN. This is `gpu_host_ip`, and it is the address every configured path uses. |
| `100.107.5.66` | Its Tailscale address. Nothing in this repo configures it. | **All 8 tailnet peers.** |

**Neither Ollama nor ComfyUI has any authentication, and both answer on the
tailnet address as well as the LAN one.** That is not a design decision — it is
the unresolved finding recorded in [§4](#4-open-the-firewall-narrowly) below,
where a fix was applied on 2026-08-12 and measured *not to have worked* on
2026-08-13. Treat `100.107.5.66:11434` and `100.107.5.66:8188` as reachable by
every peer until a tailnet ACL says otherwise.

Two consequences that are easy to get wrong:

- **`comfyui.fortwow.dev` does not change this.** That vhost (added 2026-08-27)
  puts Authelia in front of the *hostname*; Caddy never gates the port. Both
  `192.168.1.40:8188` and `100.107.5.66:8188` stay open exactly as before. The
  vhost is a convenience, not a control.
- **A tailnet-address test is the only one that proves scoping.** Testing from
  the LAN cannot distinguish a correctly scoped firewall rule from a broken
  one, because the LAN is allowed either way. `docs/gpu-host.md`'s own
  measurements were taken from `brandons-macbook-pro` (`100.110.75.114`) for
  this reason — and note that `tailscale status` may report the path to TERRA
  as `direct 192.168.1.40:41641` while the test is still genuinely exercising
  the tailnet, because Windows Firewall evaluates the decapsulated packet.

`gpu_host_online: false` is the default and is the state to leave it in until
the PC actually exists and answers. While it is false, Open WebUI deploys with
`ENABLE_OLLAMA_API` switched off, so the chat UI offers no models rather than
throwing connection errors at a machine that isn't there.

**Image generation is no longer covered by that flag.** It moved to
`inventory/group_vars/all/images.yml` as `image_generation_enabled`, applied by
`make owui-image-config` rather than by `make infra` — Open WebUI's config rows
override the environment, so the env key that used to gate it was deleted.
Taking the GPU host offline therefore means setting `image_generation_enabled:
false` there and running that target as well. `make infra` alone will not do it.

## Setup, in order

### 1. Reserve the address

Give the PC's NIC a DHCP reservation for `192.168.1.40` in pfSense. The address
is already baked into the deployed config, so a different one means editing
`gpu_host_ip` and re-running `make infra`.

As built, `.40` is held by the **Wi-Fi** adapter, not the wired one. That is
fine — model responses are text and generated images are about a megabyte. But
if you ever move to Ethernet, **move the reservation to the Ethernet MAC rather
than adding a second one**: with both, Windows prefers the wired route outbound
while `.40` stays on Wi-Fi inbound, which is a confusing way to spend an
afternoon.

### 2. Install Ollama

Install from [ollama.com](https://ollama.com/download/windows), then set a
system environment variable so it listens on the LAN rather than loopback
only:

```text
OLLAMA_HOST = 0.0.0.0:11434
```

Set it under *System Properties → Environment Variables → System variables*,
not in a shell — Ollama runs as a background service and will not see a
variable set in one terminal. Restart Ollama afterwards.

**That advice is necessary but not sufficient, and the gap bit us.** See
[the KV cache section](#setting-it-is-not-the-same-as-it-taking-effect) below:
writing the variable to the registry does not reach a process launched from a
shell that predates the write. The same failure applies to `OLLAMA_HOST`.

### Minimum version: 0.32.9

`muse-glimmer:30b` needs **Ollama ≥ 0.32.9** (released 2026-08-11, the first
build handling it). On 0.32.6 the pull fails with a bare *"Please download the
latest version"* and no mention of which model or why.

The upgrade is a per-user install — `OllamaSetup.exe /VERYSILENT /NORESTART`
into `%LOCALAPPDATA%\Programs\Ollama`, no administrator needed. Two things
follow it, neither optional:

- **Re-apply and verify the KV cache setting**, which the installer drops.
- **Re-check the firewall.** The installer creates `ollama.exe` rules scoped
  to `Remote: Any` on the Private and Public profiles, which override the
  narrow LAN rule entirely. They were found enabled again on 2026-08-12 and
  disabled by hand:

  ```powershell
  Get-NetFirewallRule -DisplayName "ollama.exe" | Disable-NetFirewallRule
  ```

  That needs an elevated shell. Confirm afterwards from a **tailnet peer**
  that `http://100.107.5.66:11434/api/tags` stops answering while
  `http://192.168.1.40:11434/api/tags` still does — both halves, because the
  first alone cannot distinguish a narrowed scope from a dead service.

  **Do not assume disabling them closes the tailnet path.** It was done on
  2026-08-12 and the path still answered on 2026-08-13 — see
  [the measurement below](#-that-fix-was-applied-and-it-did-not-work--measured-2026-08-13).
  Run the confirmation, and believe the curl rather than the rule state.

Pull the four chat models Open WebUI offers, the coding model, and the two
small models Continue needs for autocomplete and embeddings:

```powershell
# Chat — all four are abliterated (see docs/chat-models.md)
ollama pull huihui_ai/gemma-4-abliterated:26b     # default
ollama pull huihui_ai/Qwen3.6-abliterated:27b     # technical work
ollama pull huihui_ai/gemma-4-abliterated:31b     # see the CPU-spill warning
# Vision + agentic. NEEDS OLLAMA >= 0.32.9 - see the version note below.
ollama pull muse-glimmer:30b                      # the only model here that can see
# Coding
ollama pull qwen3-coder:30b                       # Continue chat/edit/apply
# (an abliterated coder was tried here and removed - see "What actually fits")
# Small, always-on
ollama pull qwen2.5-coder:1.5b-base               # Continue autocomplete
ollama pull nomic-embed-text                      # Continue embeddings
```

About 112 GB in total once the DavidAU model below is added, which is why the
disk check comes first: `Get-PSDrive C | Select-Object Used,Free`.

**A model's download size is not its VRAM footprint**, and on this card the
difference decides whether a model is usable at all. Ollama allocates a
32768-token KV cache alongside the weights, so budget from the resident figure
in `ollama ps`, never from the tag. The measured table further down is the one
to plan against.

### The fourth chat model cannot be pulled the normal way

`DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF`
is a Hugging Face GGUF repo rather than an Ollama registry entry, so it needs
the `hf.co/` prefix and an explicit quant tag. **That pull currently fails**,
and it fails late and confusingly:

```powershell
# Downloads 18 GB + 927 MB successfully, then dies:
#   Error: context deadline exceeded
ollama pull hf.co/DavidAU/Qwen3.6-27B-Fable-Fusion-711-Uncensored-Heretic-NM-DAU-NEO-MAX-MTP-GGUF:Q4_K_M
```

The weights are not the problem. It is a **479-byte config blob** whose
`hf.co/v2/.../blobs/sha256:2a30fe37...` endpoint consistently answers in ~40
seconds — Hugging Face generates it on demand — against Ollama's hardcoded 30
second deadline. Fetching that URL by hand returns HTTP 200 every time; Ollama
simply gives up first. Retrying does not help.

The weights *do* land in the blob store, so register them directly instead of
downloading them again. Find the 16.81 GB blob and point a Modelfile at it:

```powershell
$blob = Get-ChildItem "$env:USERPROFILE\.ollama\models\blobs" -File |
        Where-Object { $_.Length -gt 16.5GB -and $_.Length -lt 17GB -and $_.Name -notlike '*partial*' }
"FROM $($blob.FullName)" | Out-File -Encoding utf8 Modelfile.davidau
ollama create davidau-fable-fusion:27b-q4km -f Modelfile.davidau
```

Two things follow from this and both matter:

- **The model is named `davidau-fable-fusion:27b-q4km`**, not the upstream
  path. That name is local to this machine and appears in Open WebUI's
  dropdown as such.
- **`:Q4_K_M` resolves to the non-MTP build** (16.81 GB), not the 17.23 GB MTP
  one — the two share a quant suffix and Ollama picks by that suffix alone.
  Confirm by size if it ever matters.

`ollama create` copies the blob rather than referencing it, so this leaves an
orphaned ~17 GB copy from the failed pull. Harmless, reclaimable.

Confirm from another machine, not from the PC itself — loopback would pass
even with the default bind:

```bash
curl http://192.168.1.40:11434/api/tags
```

### 3. Install ComfyUI

Use the **portable build** — it bundles its own Python, and there is no Python
on this machine's `PATH`. Take `ComfyUI_windows_portable_nvidia.7z` from
[ComfyUI's releases](https://github.com/comfyanonymous/ComfyUI/releases) and
extract to `C:\ComfyUI\`, giving `C:\ComfyUI\ComfyUI_windows_portable\`.

It is a **`.7z`, not a `.zip`**. Windows 11 handles it; so does the bundled
`tar.exe`, which is the scriptable option:

```powershell
tar.exe -xf ComfyUI_windows_portable_nvidia.7z -C C:\ComfyUI
```

**A fresh ComfyUI ships no image models, and without one the UI loads
perfectly while every generation fails.** Download a checkpoint before
believing the install works:

```powershell
Invoke-WebRequest -Uri "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors" `
  -OutFile "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\checkpoints\sd_xl_base_1.0.safetensors"
```

~6.5 GB. If it lands as a few KB, that is an HTML error page wearing a
`.safetensors` name — delete it and retry.

#### The second checkpoint: Pony Diffusion V6 XL

In-chat generation runs on Pony V6 XL, not on stock SDXL. Both live in
`models\checkpoints\`; which one is submitted is decided by `image_workflow`
in `inventory/group_vars/all/images.yml` and applied with
`make owui-image-config`. SDXL stays installed because it is the fallback the
workflow gate is exercised against.

```powershell
Invoke-WebRequest -Uri "https://huggingface.co/LyliaEngine/Pony_Diffusion_V6_XL/resolve/main/ponyDiffusionV6XL_v6StartWithThisOne.safetensors" `
  -OutFile "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\checkpoints\ponyDiffusionV6XL_v6StartWithThisOne.safetensors"
```

**Verify before wiring it up** — 6938041050 bytes, and:

```powershell
Get-FileHash -Algorithm SHA256 C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\checkpoints\ponyDiffusionV6XL_v6StartWithThisOne.safetensors
# 67ab2fd8ec439a89b3fedb15cc65f54336af163c7eb5e4f2acc98f090a29b0b3
```

Two things about that hash are worth knowing, because getting either wrong
wastes an afternoon or worse.

**Do not read it off the `etag` header.** This repository is Xet-backed, so
`etag` carries the Xet content-address hash, and the same value appears as a
path component in the CDN redirect — which makes it look authoritative. It is
not the SHA256 and will never match `Get-FileHash`. The authoritative value is
`lfs.sha256` from `https://huggingface.co/api/models/<repo>?blobs=true`, or the
`X-Linked-ETag` header. Four checksums in
[the image generation plan](plans/uncensored-image-generation.md) were recorded
the wrong way and every one of them was wrong; they were corrected on
2026-08-20 after this download failed to match.

**Pony is distributed on CivitAI, which requires an API token, so
`LyliaEngine/Pony_Diffusion_V6_XL` is an unaffiliated third-party mirror.** Two
things make that acceptable rather than reckless: `.safetensors` is a data-only
format that cannot execute code on load, unlike the `.ckpt` pickles it
replaced, and the checksum above pins exactly which bytes were reviewed — which
is only true now that the checksum is a real SHA256.

Pony was trained with the tags `score_9, score_8_up, score_7_up` and degrades
visibly without them, but Open WebUI overwrites the mapped prompt node wholesale
on every request. So the tags do not live in the prompt node: they sit in a
fixed `CLIPTextEncode` (node 10) merged into the user's prompt by a
`ConditioningConcat` (node 11), and the mapping points at the user node only.
That is why `inventory/comfyui-workflows/pony.json` has two nodes `sdxl.json`
does not.

The stock `run_nvidia_gpu.bat` binds loopback only, and editing it would be
reverted by the next ComfyUI update, silently taking the LAN bind with it.
Write `C:\ComfyUI\start-comfyui-lan.bat` instead:

```bat
@echo off
cd /d C:\ComfyUI\ComfyUI_windows_portable
.\python_embeded\python.exe -s ComfyUI\main.py --listen 0.0.0.0 --port 8188 --windows-standalone-build
```

For it to survive reboots, put a shortcut to that file in `shell:startup`.
Then confirm `http://192.168.1.40:8188` loads from another machine — and that
`/system_stats` reports `cuda:0`, since a CPU fallback also loads fine and
merely takes twenty minutes per image.

#### MiniMax H3: video generation, driven directly from ComfyUI

Unlike Pony above, **H3 is not reachable through Open WebUI at all** — there
is no video-generation config surface in `ImagesConfig`
(`docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md`
confirmed this). It is driven straight from ComfyUI's own web UI at
`http://192.168.1.40:8188`: no catalog entry, no admin-API push, no `make`
target. Full design:
`docs/superpowers/specs/2026-08-27-video-generation-design.md`.

The three H3 templates (T2V, I2V, R2V) come from ComfyUI's Template Library
on demand rather than shipping with the portable install, so the machine
needs network access the first time that library is opened. **Be plain about
a second gap too: the measurements below were not taken through the GUI.**
They were taken by submitting API-format graphs derived from the official
templates directly to `/prompt`, because the templates ship in editor format
and the T2V/I2V ones wrap their graph in a subgraph that `/prompt` cannot
accept as-is. The GUI path described above — opening the Template Library,
pointing its dropdowns at the files below, and queuing from the web UI — is
the intended way to drive H3, but it is documented, not verified.

**Requires ComfyUI ≥ 0.30.0.** This host measured **0.31.0**, which clears
the floor. All four native H3 node classes ship with that build and needed no
custom-node install: `EmptyMiniMaxH3LatentAV`, `MiniMaxH3ImageToVideo`,
`MiniMaxH3ReferenceToVideo`, `MiniMaxH3SigmaShift`. (Distinct `Minimax*Node` /
`MinimaxHailuo*` classes also exist in the same build — those call MiniMax's
hosted cloud API, not the local weights below. Do not confuse them.)

##### Weight files

All five come from `Comfy-Org/MiniMax-H3` on Hugging Face — the
ComfyUI-packaged form, not the raw Diffusers release — at the pruned-int8
tier, one of the two smallest tiers on offer. The non-pruned int8 diffusion
model (31.7 GiB) and bf16 (61.7 GiB) are the ones that clearly cannot fit a
24 GB card; pruned-int8 was chosen over them, not because it was the only
candidate that could fit. A pruned-fp8-scaled tier
(`minimax_h3_fl2va_pruned_fp8_scaled.safetensors`, 20,958,205,608 bytes — 12
MB smaller than the pruned-int8 file used here) also exists in the same repo
and was **not** evaluated; nothing here claims pruned-int8 is the smallest or
best-fitting option, only that it is a verified-working one. **Verified on
both byte count and SHA256** against the repo's own `lfs.sha256`, the same
discipline the Pony checksum above exists to enforce: byte count alone would
not have caught a complete-but-corrupted transfer.

| File | Destination | Size (bytes) | SHA256 |
|---|---|---|---|
| `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | `models/diffusion_models/` — T2V + I2V | 20970379616 | `e889202c41dafb67b10d67b97f0d8541508036a6090af23425a5c2615d03c47a` |
| `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | `models/diffusion_models/` — R2V | 20970379616 | `9255f52b6677845ad238f20dfaafa94727053694127ab7f255c048f0f9365779` |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `models/text_encoders/` — shared by all three | 15687142551 | `35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6` |
| `minimax_h3_video_vae_fp16.safetensors` | `models/vae/` | 5207808496 | `7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522` |
| `minimax_h3_audio_vae_fp32.safetensors` | `models/vae/` | 605254808 | `8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48` |

Total on disk 63.4 GB, against a 2.0 TiB free disk — not the constraint.

##### The turbo LoRAs are downloaded but switched off, and their necessity is unproven

The official T2V/I2V and R2V templates each reference a turbo LoRA behind a
switch node that defaults to `False` —
`minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` for T2V/I2V and
`minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` for R2V, ~1.96 GB
each, hash-verified the same way as the five files above. Both were
downloaded on the reasoning that ComfyUI validates the `LoraLoaderModelOnly`
node's COMBO input against files actually present on disk even when the
branch feeding it is switched off, so a missing file looked likely to fail
prompt validation regardless of the switch state.

**That reasoning turned out to be wrong.** The T2V validation failure hit
during setup traced back to an unrelated bug in this session's own
template-to-API converter (a dotted autogrow input name, `values.a`, folded
into the wrong shape) — not a missing LoRA file. The turbo LoRA simply
happened to be present when the first successful queue went in, so **whether
it is actually required is unproven, and it is not currently in use.** All
three modes below were generated with the switch left at its template
default, `False`. Do not read this section as recommending the LoRA be
turned on, or as evidence the file needs to be present at all — that would
need testing with it absent, which was not done.

##### Measured: all three modes fit, R2V is tight

Card: RTX 4090, 24564 MiB. Idle baseline measured after freeing VRAM ComfyUI
was still holding from an earlier Pony session, with nothing resident in
Ollama, then a peak reading taken for each mode — the same "State | VRAM
used" table this doc already uses for chat models in
["Sharing the card with image generation"](#sharing-the-card-with-image-generation):

| State | VRAM used of 24564 MiB |
|---|---|
| Idle (live desktop session — VS Code, Steam, NVIDIA overlay, Explorer all resident) | 2876 MiB |
| T2V peak | 21826 MiB |
| I2V peak | ~21615 MiB |
| R2V peak | 23042 MiB |

**That idle baseline is higher than the 1464–2012 MiB baselines in
[gpu-capacity.md](gpu-capacity.md)** because this was measured during a live
interactive desktop session — VS Code, Steam, the NVIDIA overlay and Explorer
were all holding VRAM at the time. Recorded as the real number rather than an
idealised one, the same discipline that file exists for.

The design's open question — whether the ~39.6 GB on-disk pruned-int8 stack
could ever be co-resident on a 24 GB card — is answered: it does not need to
be. ComfyUI sequences the three stages (text encoder → diffusion → VAE
decode) without ever holding all of them at once, and **generation succeeded
in all three modes, with no OOM.** The Ollama chat model was not resident
during any of the three runs, consistent with the design's VRAM strategy of
stopping it before generating — `ollama ps` had nothing resident from the
start of this work, so nothing needed to be manually stopped this time, but
the requirement stands for any future run that follows a chat session.

| Mode | Checkpoint | Execution time¹ | Output |
|---|---|---|---|
| T2V | `fl2va` | 386.2 s | 387,730 bytes (0.37 MB) |
| I2V | `fl2va` | 418.7 s | 2,365,000 bytes (2.26 MB) |
| R2V | `ref2va` | 455.4 s | 2,204,062 bytes (2.10 MB) |

¹ ComfyUI's own `/history` execution timestamps (`execution_start` to
`execution_success`), excluding time spent waiting in the queue — see the
trap recorded just below the table.

All three: 1344×768, 5.17 s (124 frames @ 24 fps), H.264 video + AAC audio, 20
sampling steps, turbo LoRA off. The output-size spread is not a broken
render — T2V's near-static test scene simply compresses far harder under
H.264 than I2V/R2V's more detailed ones. Each `.mp4` was confirmed by parsing
its container atoms directly (duration, dimensions, and both a video and an
audio track present), not by file size alone.

**The first pass at this table timed submission-to-completion, and that made
R2V look twice as slow as it actually is.** R2V was queued behind I2V —
ComfyUI serialises the queue rather than running two prompts at once — so its
submission-to-completion figure (844 s) silently included ~389 s spent
waiting for I2V to finish on the same GPU. `/history`'s own timestamps expose
this: R2V's `execution_start` landed 277 ms after I2V's `execution_success`,
which is proof of queue delay, not slow execution. The execution-time figures
in the table above are the ones worth planning against; a submission-to-completion
timing is only honest when nothing else is queued ahead of the job being
timed, and that should have been checked the first time rather than assumed.

**R2V is the tight one on VRAM: only ~1.5 GiB of headroom** (23042 of 24564
MiB, from the earlier table, ~1.2 GB higher than T2V/I2V's 21826 MiB peak).
On execution time it is unremarkable — about 9% slower than I2V, not the
"roughly twice as long" the polluted wall-clock number first suggested.
Upstream's account (the node's own tooltip) attributes the extra VRAM to
reference-image conditioning riding through every sampling step; that is
upstream's explanation of the mechanism, not something measured here.
Anything else holding VRAM at generation time — a game, a second ComfyUI
model, even a heavier desktop session than the one this was measured
against — plausibly OOMs R2V where T2V/I2V would still fit.

##### System RAM is at the low end of guidance, not comfortably above it

Measured `Win32_ComputerSystem.TotalPhysicalMemory` = 33,409,974,272 bytes =
**31.1 GiB**. Third-party guidance for H3 suggests 32–64 GB. 31.1 GiB sits at
the low end of that range rather than comfortably inside it — recorded as a
live risk, not a resolved one.

##### Output and retention

Output lands in ComfyUI's default output folder with a `homelab-h3` filename
prefix. Retention is manual, by design —
`docs/superpowers/specs/2026-08-27-video-generation-design.md` explains why.

#### A missing model does not fail here — it silently loads a different one

Installing H3 broke image generation, and the way it broke is worth keeping,
because nothing in the failure points at the cause.

**Symptom, 2026-08-27:** every generation in ComfyUI's own UI died with
`IndexError: list index out of range` at a `KSampler`, apparently from "the
default workflow, changing nothing".

Neither half of that was true, and neither was the obvious reading that H3 had
broken something. The graph was the stock **Z-Image Turbo** template, whose
three model files had never been downloaded. **When a loader's saved filename
is absent, ComfyUI's frontend does not fail — it selects the first entry in the
dropdown.** Until H3 arrived, `models\diffusion_models\`, `models\text_encoders\`
and `models\vae\` held no model files at all, because this host generated
images only through `CheckpointLoaderSimple`. H3 put the first-ever file in all
three at once, and the template quietly rebound itself to a 19.5 GiB video
model:

| Loader | Template expects | What it silently loaded |
|---|---|---|
| `UNETLoader` | `z_image_turbo_bf16.safetensors` | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` |
| `CLIPLoader` | `qwen_3_4b.safetensors` | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` |
| `VAELoader` | `ae.safetensors` | `minimax_h3_audio_vae_fp32.safetensors` |

The substituted files exist, so the graph passes validation and fails far
later, inside the model's own forward pass — the bottom frame of the
traceback, in `comfy\ldm\minimax\model.py`:

```python
def forward(self, x, timestep, context, ...):
    audio_src = x[1]
```

H3 is omni-modal: `x` must be `[video_latent, audio_latent]`. The template's
`EmptySD3LatentImage` supplies a single image latent, so `x[1]` is out of
range. Note the VAE it chose was the **audio** VAE — first alphabetically —
the same fallback showing itself a third time in one graph.

**The rule this leaves behind:** on this host, a template naming a model you
have not downloaded will not say so. It will run a different model. Before
trusting an unfamiliar template, confirm every filename it names is present;
each template's own `MarkdownNote` node lists them with upstream URLs. And when
a generation fails inside a model rather than at validation, read the
traceback's **bottom** frame first — it names the model that actually loaded,
which is the one piece of evidence the error message itself withholds.

The narrower lesson for image generation specifically: `CheckpointLoaderSimple`
is not exposed to this trap in the same way, because `models\checkpoints\` has
only ever held real image checkpoints. The three split-file directories are
where it bites.

**Installing the image models below changed what a fallback lands on, and that
is a mitigation rather than a fix.** The dropdowns are ordered alphabetically,
so the first entry in each is now an image model rather than a slice of H3:
`flux1-dev.safetensors` for `UNETLoader`, `clip_l.safetensors` for
`CLIPLoader`, `ae.safetensors` for `VAELoader`. A template missing its models
will now silently load something that at least produces an image, which is a
quieter and arguably more dangerous failure than the loud `IndexError` above.
The check is still to confirm the filenames before trusting a template.

#### The image models installed alongside Pony and SDXL

Added 2026-08-27, so the stock templates have their real models rather than
leaving the dropdowns to guess. All are ungated public downloads; every one is
`.safetensors`, which cannot execute code on load.

| models\ subdirectory | File | Bytes | Used by |
|---|---|---|---|
| `diffusion_models` | `z_image_turbo_bf16.safetensors` | 12309866400 | Z-Image Turbo |
| `text_encoders` | `qwen_3_4b.safetensors` | 8044982048 | Z-Image Turbo |
| `diffusion_models` | `flux1-dev.safetensors` | 23802932552 | Flux.1 dev |
| `text_encoders` | `clip_l.safetensors` | 246144152 | Flux.1 dev |
| `text_encoders` | `t5xxl_fp16.safetensors` | 9787841024 | Flux.1 dev |
| `vae` | `ae.safetensors` | 335304388 | **both** of the above |
| `checkpoints` | `sd_xl_refiner_1.0.safetensors` | 6075981930 | SDXL base+refiner |

SHA256, each verified against the downloaded bytes:

```
2407613050b809ffdff18a4ac99af83ea6b95443ecebdf80e064a79c825574a6  z_image_turbo_bf16.safetensors
6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a  qwen_3_4b.safetensors
4610115bb0c89560703c892c59ac2742fa821e60ef5871b33493ba544683abd7  flux1-dev.safetensors
660c6f5b1abae9dc498ac2d21e1347d2abdb0cf6c0c0c8576cd796491d9a6cdd  clip_l.safetensors
6e480b09fae049a72d2a8c5fbccb8d3e92febeb233bbe9dfe7256958a9167635  t5xxl_fp16.safetensors
afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38  ae.safetensors
7440042bbdc8a24813002c09b6b69b64dc90fded4472613437b7f55f9b7d9c5f  sd_xl_refiner_1.0.safetensors
```

Those hashes were taken from `lfs.oid` in
`https://huggingface.co/api/models/<repo>/tree/main/<dir>`, which is a real
SHA256 — the same trap documented for Pony above applies here, so do not read
them off `etag`. Note that endpoint lists a **directory**; handing it a full
file path returns 404.

**Fetch these with `curl.exe`, not the `Invoke-WebRequest` used for the two
checkpoints above.** PowerShell 5.1 buffers an entire response in memory before
writing it, which is survivable for a 6.5 GB checkpoint on this 31.1 GiB
machine and is not survivable for `flux1-dev.safetensors` at 22.17 GiB. `curl`
also resumes a partial transfer, which matters over this host's Wi-Fi link:

```powershell
curl.exe -L --fail --no-progress-meter -C - -o <dest>.part <url>
```

Do not pipe `curl.exe` through `2>&1` in PowerShell: it wraps native stderr in
ErrorRecords, and under `$ErrorActionPreference='Stop'` ordinary progress
output then kills the run. Verify the hash on the `.part` file and only then
rename it into place — a truncated `.safetensors` that loads is worse than one
that fails.

**`ae.safetensors` is named by two templates from two different repositories,
and that is not a collision.** Z-Image points at `Comfy-Org/z_image_turbo` and
Flux at `Comfy-Org/Lumina_Image_2.0_Repackaged`. Both are 335304388 bytes with
SHA256 `afc8e28…529e38` — the same file. One copy serves both templates. Do
not "disambiguate" them by renaming: both templates default to the bare name
`ae.safetensors`, and renaming would re-arm the silent-fallback trap above.

##### The saved workflows

Three stock templates are copied verbatim into
`ComfyUI\user\default\workflows\`, so they open from the Workflows sidebar
instead of having to be found in Browse Templates each time:

```
Z-Image Turbo - text to image.json
Flux.1 dev - text to image.json
SDXL base+refiner - text to image.json
```

They are unmodified copies, which is deliberate — it keeps them diffable
against upstream when a template changes. The Z-Image and Flux templates are
subgraph-based: their top level holds three nodes, and the real graph lives
under `definitions.subgraphs`. A loader scan that walks only `nodes` finds
nothing in them and will wrongly conclude they reference no models.

##### Measured 2026-08-27: all three run, and Flux is the tight one

Each generated a 1024×1024 PNG from its template's own prompt. Execution time
is ComfyUI's `execution_start` → `execution_success` out of `/history`, not
wall clock. Card total is 24564 MiB; idle desktop baseline was 1542–1555 MiB.

| Workflow | Execution | Peak VRAM | Headroom |
|---|---|---|---|
| SDXL base+refiner | 9.7 s | 13860 MiB | 10704 MiB |
| Z-Image Turbo | 11.0 s | 21762 MiB | 2802 MiB |
| Flux.1 dev | 21.7 s | 23564 MiB | **1000 MiB** |

**Flux.1 dev fits, but only just.** `flux1-dev.safetensors` is 22.17 GiB and
`t5xxl_fp16.safetensors` another 9.12 GiB — more than the card holds — so
ComfyUI sequences them the way it does for H3 rather than keeping both
resident. It works, with roughly 1 GiB spare. Anything else holding VRAM at
the time — a chat model, a game, a second ComfyUI model — plausibly OOMs it.
The `t5xxl_fp8_e4m3fn_scaled.safetensors` encoder the template offers as an
alternative is the lever to pull if that becomes a problem; it was not needed
here and so was not downloaded.

**Z-Image peaks nearly as high as Flux despite being half the size**, because
its `qwen_3_4b` text encoder (7.49 GiB) sits alongside the 11.46 GiB model.
Model file size is not a proxy for VRAM cost when the encoder ships separately.

**These numbers required restarting ComfyUI between runs, and the first
attempt at them was wrong.** `POST /free` unloads the model but does not
return the memory to the card: torch's caching allocator keeps its reservation,
so `nvidia-smi` still reports the previous model's footprint. Measured back to
back after Flux, Z-Image and SDXL "peaked" at 21700 and 21060 MiB — figures
that were almost entirely Flux's leftover reservation rather than their own
cost. SDXL's true peak is 13860 MiB, some 7 GiB lower. This is the same class
of error as the R2V queue-delay above: a measurement taken in a convenient
order rather than a clean one. Restart between models, or do not publish the
number.

#### Qwen Image Edit: the editing checkpoint

Added 2026-08-31. Sourced from `Comfy-Org/Qwen-Image-Edit_ComfyUI` (diffusion
model) and `Comfy-Org/Qwen-Image_ComfyUI` (text encoder, VAE) on Hugging Face —
first-party Comfy-Org repackaging, Apache-2.0, ungated, the same provenance bar
as the models above. Every hash below was verified twice independently against
the downloaded bytes, once during implementation and once by re-computing all
three from scratch.

| File | Destination | Bytes |
|---|---|---|
| `qwen_image_edit_fp8_e4m3fn.safetensors` | `diffusion_models` | 20430635136 |
| `qwen_2.5_vl_7b_fp8_scaled.safetensors` | `text_encoders` | 9384670680 |
| `qwen_image_vae.safetensors` | `vae` | 253806246 |

```
393c6743d1de2e9031b5197027b36116f2096958ccc0223526d34e1860266021  qwen_image_edit_fp8_e4m3fn.safetensors
cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4  qwen_2.5_vl_7b_fp8_scaled.safetensors
a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f  qwen_image_vae.safetensors
```

##### Measured 2026-08-31: it fits, and it is tight the same way Flux is

Card total is 24564 MiB. Idle baseline (ComfyUI not loaded, desktop apps only)
measured ~3812–4187 MiB across two samples with slightly different desktop
state each time. Peak during a real 50-step Qwen Image Edit generation was
~23536–23545 MiB, stable across two separate runs — leaving ~1019–1028 MiB of
headroom, about 4% of the card. No OOM across three total generation runs (two
direct-to-ComfyUI, one through the full Open WebUI API), but there is little
margin. Execution time for a real generation ran ~157–196 s, depending on
input image size and what else was competing for the GPU.

**This fits, but only just — the same conclusion as Flux, for the same
reason.** The design doc predicted this by analogy to Flux's ~28 GB on-disk
stack before anything was measured, and the measurement landed almost exactly
where predicted. **It cannot coexist with a resident chat model**, unlike
Pony/SDXL (see "Sharing the card with image generation" below) — it belongs in
the same bucket as Flux, Chroma, and H3.

### 4. Open the firewall, narrowly

Windows Defender Firewall blocks both ports inbound by default. Add rules for
TCP 11434 and TCP 8188 **scoped to the LAN subnet**, not to Any:

```powershell
New-NetFirewallRule -DisplayName "Ollama (LAN)" -Direction Inbound `
  -Protocol TCP -LocalPort 11434 -RemoteAddress 192.168.1.0/24 -Action Allow
New-NetFirewallRule -DisplayName "ComfyUI (LAN)" -Direction Inbound `
  -Protocol TCP -LocalPort 8188 -RemoteAddress 192.168.1.0/24 -Action Allow
```

Worth being explicit about the exposure: neither Ollama nor ComfyUI has any
authentication. The `-RemoteAddress` scope is the only thing standing between
them and the rest of the network, so do not widen it, and do not forward
either port at the router.

#### ⚠️ That scope is not currently holding — measured 2026-08-10

**Both services answer on TERRA's tailnet address, unauthenticated.** Verified
from `brandons-macbook-pro` (100.110.75.114), a tailnet peer:

```
curl http://100.107.5.66:11434/api/tags     -> 200, full model list
curl http://100.107.5.66:8188/system_stats  -> 200, ComfyUI version + RAM
```

Ollama's installer adds its own `ollama.exe` rules with `Remote: Any` on both
the Private and Public profiles, and those override the narrow LAN rule above.
The tailnet has **8 peers**, so every one of them can run inference, pull
against the 2.4 TB disk, or delete the model library.

**This corrects an earlier claim that "ComfyUI is correctly scoped — this is
Ollama-specific."** It is not. ComfyUI answered on the same tailnet address in
the same test, so whatever is admitting the traffic is not unique to Ollama's
installer rules. Both need scoping, and the ComfyUI half has no diagnosis yet.

One honest caveat about the evidence. `tailscale status` reports the path to
TERRA as `direct 192.168.1.40:41641` — the WireGuard transport rides the LAN,
because both machines are on it. That does **not** invalidate the result:
Windows Firewall evaluates the *decapsulated* packet as it leaves the Tailscale
adapter, where the source address is `100.110.75.114`, which
`-RemoteAddress 192.168.1.0/24` cannot match. A genuinely remote peer produces
an identical inner packet. The airtight version of this test is the same two
curls from a peer on cellular; every such peer was offline at the time.

The obvious fix needs an elevated shell **on TERRA**:

```powershell
Get-NetFirewallRule -DisplayName "ollama.exe" | Disable-NetFirewallRule
```

Then re-run both curls from a tailnet peer and require that they stop
answering while `192.168.1.40` still does — a check that fails closed rather
than one that merely looks quiet. Re-check after Ollama updates; the installer
created these rules and may recreate them.

#### ⚠️ That fix was applied and it did not work — measured 2026-08-13

The `ollama.exe` rules were disabled on TERRA on 2026-08-12 and read back as
`enabled=False` from that machine. **The tailnet path still answers.** Verified
2026-08-13 from `brandons-macbook-pro`, the same peer as before:

```
curl http://100.107.5.66:11434/api/tags     -> 200, 12 models, Ollama 0.32.9
curl http://100.107.5.66:8188/system_stats  -> 200
curl http://192.168.1.40:11434/api/tags     -> 200  (LAN still works, as required)
```

`route -n get 100.107.5.66` resolves to `utun6`, the Tailscale adapter, so this
is genuinely the tailnet path and not the LAN one under a different address.

**So disabling the `ollama.exe` rules is not sufficient, and the diagnosis
above is incomplete.** That was already implied by the ComfyUI half — ComfyUI
has no `ollama.exe` rule and answered anyway — but it is now measured for both.
Something other than Ollama's installer rules is admitting decapsulated
Tailscale traffic. Candidates not yet eliminated, in the order worth checking:

1. A Tailscale-created firewall rule. The client adds its own allow rules, and
   they are scoped to the tailnet interface rather than to a remote subnet.
2. The Tailscale adapter landing in the **Private** profile, where a broad
   inbound allow would match traffic that `-RemoteAddress 192.168.1.0/24`
   was written to exclude.
3. Tailscale Serve or a subnet-router/exit-node setting forwarding the ports.

Until one of those is confirmed, treat **both services as reachable by all 8
tailnet peers, unauthenticated**. The reliable containment is at the Tailscale
layer rather than the Windows one — a tailnet ACL denying `:11434` and `:8188`
to every peer, which is enforced by the coordination server and cannot be
reverted by an Ollama installer. That is the recommended next step, and unlike
the firewall edit it does not need administrator rights on TERRA.

### 5. Tell the homelab it exists

```bash
# inventory/group_vars/all/main.yml
gpu_host_online: true
```

Then `make infra`. Open WebUI restarts with both backends enabled.

Verify by *using* it, not by checking that the container is up: send a chat
message and get a real reply, then generate an image from the same
conversation. A green container proves nothing about whether inference works.

## What actually fits on the card

**The table that used to live here has moved to
[gpu-capacity.md](gpu-capacity.md), which is generated rather than
hand-written.** There were two copies of it — one here, one in
`chat-models.md` — and they had already drifted apart. Regenerate it with
`scripts/vram_survey.py` and `scripts/vram_report.py`; do not re-add a copy.

The shape of the problem has not changed: exceeding the card does not fail, it
silently spills layers to system RAM and slows generation by roughly an order
of magnitude, and `ollama ps` is the only place that shows. The practical
ceiling is around **21 GB resident**.

### The KV cache is quantized on this host

`OLLAMA_KV_CACHE_TYPE=q8_0` with `OLLAMA_FLASH_ATTENTION=1`, adopted
2026-08-12 on the evidence in `gpu-capacity.md`. It frees 0.4–1.2 GB on every
large model and — the reason it was adopted — makes
`huihui_ai/gemma-4-abliterated:31b` fit entirely on the GPU at the full 32768
context, which it could not do under `f16`. Its `num_ctx` cap is gone.

`q4_0` was measured too and saves a further ~400 MB, but it changed no
verdict that `q8_0` had not already changed, so the extra memory bought
nothing while carrying more quality risk. Upstream calls `q8_0` no noticeable
loss and `q4_0` small-to-medium.

**The setting is server-global.** There is no per-model override, so this
applies to the coding model as much as to chat, and changing it means
restarting Ollama.

#### Setting it is not the same as it taking effect

This is the trap, and it is a sharper version of the `OLLAMA_HOST` warning
further up this page. Writing the variable to the User or Machine environment
and restarting Ollama **is not sufficient.** Windows builds a process's
environment from the registry at *launch*, so anything started from a shell
that predates the write — including `Start-Process` from such a shell — gets
the old values. Observed 2026-08-12: the variables were set, Ollama was
restarted, and the server still reported `OLLAMA_FLASH_ATTENTION:false` with
an empty cache type.

Set the variables in the launching process, then start the tray app:

```powershell
[Environment]::SetEnvironmentVariable('OLLAMA_FLASH_ATTENTION','1','User')
[Environment]::SetEnvironmentVariable('OLLAMA_KV_CACHE_TYPE','q8_0','User')
Get-Process ollama,'ollama app' -EA SilentlyContinue | Stop-Process -Force
$env:OLLAMA_FLASH_ATTENTION='1'; $env:OLLAMA_KV_CACHE_TYPE='q8_0'
Start-Process 'C:\Users\tv\AppData\Local\Programs\Ollama\ollama app.exe'
```

The User-scope write is what makes it survive a reboot; the `$env:` pair is
what makes it apply *now*. **Verify, every time** — do not infer that it
worked from having set it:

**An Ollama upgrade silently reverts this.** Measured on 2026-08-12 upgrading
0.32.6 → 0.32.9: the installer relaunches Ollama from its own environment,
which does not carry the User-scope variables, and the server came back
reporting `OLLAMA_FLASH_ATTENTION:false` with an empty cache type. Nothing
errored. The visible consequence would have been `gemma-4-abliterated:31b`
quietly spilling again at 32768 — an order of magnitude slower — while
`models.yml` and `chat-models.md` both assert it fits. So the rule is broader
than "verify after changing it": **verify after anything that restarts Ollama**,
upgrades included.

```powershell
Select-String 'server config' "$env:LOCALAPPDATA\Ollama\server.log" | Select-Object -Last 1
```

Require `OLLAMA_FLASH_ATTENTION:true` and `OLLAMA_KV_CACHE_TYPE:q8_0` in that
line. Why this matters more than it looks: upstream documents that an
unsupported architecture falls back to `f16` **without saying so**. A survey
run against a server that never received the variable produces exactly the
same output as a card that cannot do quantized cache — every model unchanged,
`FALLBACK` across the board. The wrong conclusion, "this card does not support
it", is the one you would reach and record.

**Two lessons from producing this table**, both of which cost time:

- **Record the idle baseline before each measurement.** The first run of this
  table was taken with a game holding VRAM, which invalidated it on its face.
  Re-measured on an idle card (1920 MiB baseline) the numbers moved by ~250 MiB
  and no verdict changed — but that could only be established by redoing it.
- **A model that spills is not necessarily too big.** The 31b was written off
  as not fitting; it fits entirely on the GPU once `num_ctx` drops from 32768
  to 16384, because the KV cache — not the weights — was over the line. Test
  the context before deleting a model.

`aratan/qwen3.6-claude-coder-35b-A3b-mlx-Q4KM-abliterated` was installed and
removed: 23 GB resident even at `num_ctx=2048`, never reaching 100% GPU. There
the weights genuinely do not fit and no context setting helps.

**Every estimate this roster was planned against was low**, which is why the
table exists at all: 26b was projected at ~15 GB and is 17 GB resident; the
abliterated coder was projected at ~20 GB and was 29 GB at default context.
Plan from measurements.

### Sharing the card with image generation

Measured on 2026-08-08 against the previous chat model, because the arithmetic
suggests a worse answer than reality delivers:

| State | VRAM used of 24564 MiB |
|---|---|
| Idle | ~2.5 GB |
| `qwen3:30b` resident | ~22.8 GB |
| Chat model resident **and** an SDXL image generating | ~22.8 GB, ~1.3 GB free |

On paper a 21 GB chat model plus a 6.5 GB checkpoint cannot fit, and the
expectation was that image generation would either fail or evict the chat
model. **It does neither.** With `qwen3:30b` loaded at 100% GPU and 1.3 GB
free, an SDXL generation completed in 10 seconds and the chat model was still
resident afterwards — ComfyUI pages its weights against system RAM rather than
demanding the whole card. Open WebUI's usual path, generating an image from
inside a chat conversation, is therefore fine.

What is genuinely tight is headroom. Both were verified together at 1024×1024;
larger batches or a heavier checkpoint like Flux have not been tested, and
`ollama stop <model>` frees the card instantly if something does hit an OOM.

**That measurement predates the current roster and has not been repeated
against it.** The default chat model is now `huihui_ai/gemma-4-abliterated:26b`
at 20339 MiB, which leaves a little more room than the 22.8 GB above rather
than less. The conclusion — that ComfyUI pages rather than demanding the whole
card — is expected to hold, but it has not been re-verified, and
`docs/plans/uncensored-image-generation.md` records why a heavier checkpoint
would not fit even so.

`qwen3:30b` is still installed although it is no longer offered for chat. It is
the only aligned model left on the host, which makes it the only thing that can
calibrate the uncensored-model control in
[chat-models.md](chat-models.md#verifying-the-models-are-uncensored). Keeping it
costs 18.6 GB of a 2.2 TB disk; deleting it would mean sourcing a baseline from
outside the roster.

**Do not verify a generation by re-running an identical workflow.** ComfyUI
caches by workflow hash and returns the previous image in ~2 seconds without
running the sampler — indistinguishable from success unless you notice the
identical filename and byte size. Change the seed every time.

## Continue for VSCode

Continue talks **directly to the PC**, not through the homelab. Nothing in
this repo is in that path — no Caddy, no DNS name, no Open WebUI — so coding
assistance keeps working even if the service VMs are down, and there is no
extra hop on every keystroke of autocomplete.

`~/.continue/config.yaml` on the workstation:

```yaml
name: homelab-gpu
version: 0.0.1
schema: v1
models:
  - name: qwen3-coder-30b
    provider: ollama
    model: qwen3-coder:30b
    apiBase: http://192.168.1.40:11434
    roles: [chat, edit, apply]
  - name: qwen2.5-coder-1.5b
    provider: ollama
    model: qwen2.5-coder:1.5b-base
    apiBase: http://192.168.1.40:11434
    roles: [autocomplete]
  - name: nomic-embed-text
    provider: ollama
    model: nomic-embed-text
    apiBase: http://192.168.1.40:11434
    roles: [embed]
```

Test autocomplete in a real file rather than trusting the model list to load —
the list populates from `/api/tags` even when generation is broken.

If you are running Continue **on the GPU host itself**, point `apiBase` at
`http://localhost:11434` instead. Same models, one less network hop, and it
keeps working regardless of what the Wi-Fi is doing.

## When the PC is off

Open WebUI stays up and its own login still works; chat requests fail because
the backend is unreachable, and Continue falls back to nothing. If the PC is
going to be off for a while, set `gpu_host_online: false` and run `make infra`
to get the clean "no models" state back instead of a wall of timeouts.

Web search does **not** depend on this machine — SearXNG runs in svc-download's
VPN jail and is unaffected.
