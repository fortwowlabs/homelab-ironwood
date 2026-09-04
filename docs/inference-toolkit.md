# Inference toolkit: what to reach for, and when

A practical index across everything the GPU host ([gpu-host.md](gpu-host.md))
can do — chat, vision, image, video — for someone who doesn't yet know the
roster. Model-by-model detail and setup history live in
[chat-models.md](chat-models.md) and [gpu-host.md](gpu-host.md); this page is
the map that says which one to open.

**Status legend**, used throughout: ✅ verified working (generated something
real and checked it) · 📦 installed, not yet generation-tested · ⛔ not
installed yet.

## "I want to..."

| Need | Reach for | Where |
|---|---|---|
| General chat, warmest prose | `huihui_ai/gemma-4-abliterated:26b` (default) | Open WebUI |
| Technical/agentic chat | `huihui_ai/Qwen3.6-abliterated:27b` or `Qwen3.8-abliterated:27b` | Open WebUI |
| A chat model with real VRAM headroom left over | `gurubot/gpt-oss-derestricted:20b` 📦 | Open WebUI, once added to the model dropdown — see below |
| Creative writing / roleplay | `davidau-fable-fusion:27b-q4km` | Open WebUI |
| Read an image, describe/OCR it, aligned | `muse-glimmer:30b` | Open WebUI |
| Read an image, uncensored | `huihui_ai/qwen3-vl-abliterated:8b` | Open WebUI |
| Coding help, agentic | `qwen3-coder:30b` (Continue default) or `huihui_ai/qwen3-coder-abliterated:30b-a3b-instruct-q4_K_M` | Continue / Open WebUI |
| Generate an image from a chat | Pony V6 XL (default workflow) or SDXL fallback | Open WebUI, in-conversation |
| Generate an image, best available quality | Qwen-Image-2512 📦 | ComfyUI directly (no Open WebUI surface yet) |
| Edit an existing image | Qwen Image Edit | ComfyUI directly |
| Generate a short video, general purpose | Wan 2.2 14B (T2V) 📦 | ComfyUI directly |
| Generate a video with reference-image conditioning | MiniMax H3 (R2V mode) ✅ | ComfyUI directly |
| Generate a video with synced audio | MiniMax H3 ✅ (Wan 2.2 T2V has no audio) | ComfyUI directly |

Nothing here needs Ansible or `make infra` — the GPU host is intentionally
outside that world (see "This machine is not managed by Ansible" in
[gpu-host.md](gpu-host.md)). Everything below is installed and driven by hand
on TERRA.

## Chat models

Full roster, personas, and the uncensored-verification discipline:
[chat-models.md](chat-models.md). One model is resident at a time; switching
in the Open WebUI dropdown costs 20-30 seconds.

### New: `gpt-oss-derestricted:20b` 📦

Added 2026-09-04. OpenAI's open-weight gpt-oss-20b (21B total, 3.6B active
MoE), abliterated by a third party. **Confirmed uncensored** —
`scripts/abliteration_control.py gurubot/gpt-oss-derestricted:20b` returned
`ANSWERED`, same bar every other roster model is held to.

Ollama's own `/api/ps` reports it 100% GPU-resident at **~14.9 GiB**
(`size_vram` == `size`) at the default 32768 context — meaningfully lighter
than every other chat model in the roster, which sit at 17-23 GiB. That is
the one hard number available right now.

**What's still outstanding, and why it isn't in `models.yml` yet:**
`models.yml` requires `measured_mib` to come from an actual
`scripts/vram_survey.py` pass on an idle card — not hand-estimated. That
script refused to run this session (`baseline: 7709 MiB already in use,
above the 2560 MiB idle threshold`) because ComfyUI and desktop apps were
already holding VRAM. Run it properly once the card is idle:

```bash
scripts/vram_survey.py --host http://192.168.1.40:11434 \
  --out pass-gptoss.json gurubot/gpt-oss-derestricted:20b
```

Then add the `model_roster` entry (`role: chat`, `abliterated: true`, the
resulting `measured_mib`) and the model to `ROSTER` in
`scripts/abliteration_control.py`, so `tests/validate_model_roster.py` and
`scripts/roster_reconcile.py` stop seeing it as undeclared drift. Until that
entry exists, the model is real and usable (`ollama pull` already done,
control-verified) but outside the catalog's bookkeeping.

**A second finding worth knowing about, not just this model**: ComfyUI's own
`/system_stats` endpoint (`http://192.168.1.40:8188/system_stats`) returned
byte-identical `vram_free` across three polls spanning a full Ollama model
unload — it is not a live reading, at least not on every call. `nvidia-smi`
run directly (works fine from the WSL side of TERRA — `/usr/lib/wsl/lib/`)
gave the real, moving number. Don't trust ComfyUI's stats endpoint as a VRAM
probe without corroborating it against `nvidia-smi` first.

## Image generation

Two separate paths, and they don't share a UI:

- **In-chat, through Open WebUI**: submits to ComfyUI's API automatically
  when you ask for an image in a conversation. Runs whichever checkpoint
  `image_workflow` in `inventory/group_vars/all/images.yml` points at
  (Pony V6 XL by default, SDXL as the fallback the gate is tested against).
- **Direct, through ComfyUI's own UI** (`http://192.168.1.40:8188` or
  `comfyui.fortwow.dev`): everything else — Z-Image Turbo, Flux.1 dev,
  Qwen Image Edit, and the new Qwen-Image-2512. Open WebUI has no config
  surface for picking among these; you open the workflow by name from the
  Workflows sidebar.

### New: Qwen-Image-2512 📦

Added 2026-09-04. Currently rated the strongest open text-to-image model
(Apache-2.0). Downloaded and checksum-verified:

| File | Bytes | SHA256 |
|---|---|---|
| `qwen_image_2512_fp8_e4m3fn.safetensors` (`diffusion_models`) | 20430679144 | `5dc80554d5d83390046a2f4a94ece06afb7700bf7b0aaf8bde9769793875876b` |

Its text encoder and VAE were **not** downloaded separately — Qwen-Image and
Qwen Image Edit share the same `qwen_2.5_vl_7b_fp8_scaled.safetensors` and
`qwen_image_vae.safetensors`, already on disk from the Edit install (same
byte counts and hashes recorded in [gpu-host.md](gpu-host.md#qwen-image-edit-the-editing-checkpoint)).
One copy serves both, the same pattern already noted there for
`ae.safetensors` between Z-Image and Flux.

**Not yet generation-tested.** A stock blueprint exists on the host at
`ComfyUI/blueprints/Text to Image (Qwen-Image 2512).json`, and the packaged
template ships at
`python_embeded/Lib/site-packages/comfyui_workflow_templates_json/templates/image_qwen_Image_2512.json`.
Both are subgraph-based editor format — the same shape that made H3's
templates unable to go straight to `/prompt` (see
[gpu-host.md](gpu-host.md#minimax-h3-video-generation-driven-directly-from-comfyui)).
Open it from ComfyUI's own UI (Workflows sidebar or Browse Templates) and
queue a generation there rather than fighting a hand-converted API graph —
that's the same "intended way" call already made for H3. Before trusting the
result: confirm every model filename the graph references is actually
present (the H3/Z-Image silent-fallback trap in gpu-host.md), and change the
seed if re-running, since ComfyUI caches by workflow hash and will hand back
a stale image in ~2 seconds otherwise.

## Video generation

Both paths are direct-to-ComfyUI only — neither has an Open WebUI surface.

### MiniMax H3 ✅ — verified, all three modes measured

T2V, I2V, R2V. Full detail, VRAM table, and the turbo-LoRA caveat:
[gpu-host.md](gpu-host.md#minimax-h3-video-generation-driven-directly-from-comfyui).
R2V is the only mode here with reference-image conditioning, and the only
one with synced audio.

### New: Wan 2.2 14B (T2V) 📦

Added 2026-09-04. Native ComfyUI support with official templates (unlike
H3's editor-format-only templates), which was the actual reason to add it —
less hand-conversion risk, more community troubleshooting surface. Downloaded
and checksum-verified, all four exact matches against Hugging Face's
`X-Linked-ETag`:

| File | Destination | Bytes | SHA256 |
|---|---|---|---|
| `wan2.2_t2v_high_noise_14B_fp8_scaled.safetensors` | `diffusion_models` | 14293923632 | `cad711ae211c8b23455ec68cd6a190a33a3d874234a77eb57266d73f8f0e6c9f` |
| `wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors` | `diffusion_models` | 14293923632 | `e71b96d7c82e638694c5e7fb98fac4bfb0e4ddc5fbbb4b1df40da8f0f1278a97` |
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | `text_encoders` | 6735906897 | `c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68` |
| `wan2.2_vae.safetensors` | `vae` | 1409400960 | `e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156` |

Wan 2.2's 14B is a two-stage (high-noise/low-noise) MoE-style design, so
**both** diffusion files are needed for one generation — ComfyUI sequences
them rather than holding both resident, the same pattern already relied on
for H3's text-encoder/diffusion/VAE stages.

**Not yet generation-tested**, and no VRAM measurement taken — same
"open it from ComfyUI's own Template Library and queue there" guidance as
Qwen-Image-2512 above. Search "Wan2.2 14B T2V" in the Template Library.
Only the T2V stack was downloaded (I2V would need two more ~14GB diffusion
files following the same pattern — reuses the same text encoder and VAE).

**No Wan/LTX equivalent to H3's R2V mode was found or evaluated.** If
reference-image-conditioned video is ever needed, H3 is still the only
verified option for it.

**Three concurrent large downloads produced HTTP/2 stream resets** on this
host (`curl: (92) HTTP/2 stream 1 was not closed cleanly`) — all three of
this session's big transfers (Qwen-Image-2512, both Wan diffusion files, the
gpt-oss Ollama pull) died at the same point in time, resumed cleanly with
`-C -` and a retry loop. Looks like a shared network hiccup rather than three
independent bad pulls; worth remembering if a future multi-file download
stalls the same way.

## ComfyUI custom nodes ⛔

Not installed yet — blocked by the auto-mode classifier from an unattended
session (cloning and running third-party code on TERRA, including a
face-swap tool, isn't something that gets waved through without a human
actually present). Needs either a permission rule
(`.claude/settings.local.json`, `Bash(git clone *)`) or running the clones
by hand:

```powershell
cd C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\custom_nodes
git clone --depth 1 https://github.com/Comfy-Org/ComfyUI-Manager.git
git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Impact-Pack.git
git clone --depth 1 https://github.com/cubiq/ComfyUI_IPAdapter_plus.git
git clone --depth 1 https://github.com/rgthree/rgthree-comfy.git
git clone --depth 1 https://github.com/Gourieff/ComfyUI-ReActor.git
```

Then install each node's own `requirements.txt` with the embedded Python and
restart ComfyUI:

```powershell
cd C:\ComfyUI\ComfyUI_windows_portable
.\python_embeded\python.exe -s -m pip install -r ComfyUI\custom_nodes\ComfyUI-Manager\requirements.txt
REM repeat per node that ships a requirements.txt
```

What each is for, and when to reach for it:

| Node pack | Use it for |
|---|---|
| **ComfyUI-Manager** | Install/update/remove custom nodes and models through a GUI instead of hand-run PowerShell downloads — install this one first, it makes the rest easier. |
| **ComfyUI-Impact-Pack** | Face/detail fixing on generated portraits (Detector/Detailer nodes) — useful against the Pony/SDXL checkpoints, which don't get faces right at a distance. |
| **ComfyUI_IPAdapter_plus** | Style transfer / image-prompted generation — feed a reference image's style into a new generation. Last updated April 2025; still the standard for SDXL/SD1.5-era work, but worth a compatibility check if it ever stops loading cleanly against a newer ComfyUI. |
| **rgthree-comfy** | Quality-of-life graph nodes (context switches, fast muting, reroutes) — no new capability, just makes complex graphs easier to read and edit. |
| **ComfyUI-ReActor** | Face swap. The maintained build (`Gourieff/ComfyUI-ReActor`) ships with built-in nudity detection that refuses unsafe inputs — it is not the older unrestricted version some tutorials still reference. Use deliberately; this is identity-manipulation tooling. |

## Outstanding follow-ups

- [ ] Run `scripts/vram_survey.py` for `gpt-oss-derestricted:20b` on an idle
      card, then add it to `models.yml` and
      `scripts/abliteration_control.py`'s `ROSTER`.
- [ ] Generation-test Qwen-Image-2512 and Wan 2.2 T2V through ComfyUI's own
      UI; record VRAM peaks the same way Flux/Z-Image/H3 are documented in
      [gpu-host.md](gpu-host.md).
- [ ] Install the five custom node packs once the permission rule or manual
      clone happens.
- [ ] Corroborate the ComfyUI `/system_stats` staleness finding — is it
      cached at startup, or does it only refresh after a workflow runs? If
      the latter, it's usable mid-generation, just not for an idle probe.
