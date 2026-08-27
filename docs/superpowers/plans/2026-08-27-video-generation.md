# MiniMax H3 Video Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get MiniMax H3 producing real video-with-audio clips (T2V, I2V, R2V) directly from ComfyUI on the GPU host, with every claim about it — that it downloaded intact, that it fits, that it actually ran — verified rather than assumed.

**Architecture:** No Open WebUI integration and no Ansible involvement (both confirmed impossible/inapplicable by the design). This is entirely GPU-host setup — five weight files, a ComfyUI version check, three official templates — plus one documentation update recording what was actually measured.

**Tech Stack:** ComfyUI (portable, Windows), PowerShell, curl/Python for verification reads. No repo code.

**Spec:** [docs/superpowers/specs/2026-08-27-video-generation-design.md](../specs/2026-08-27-video-generation-design.md)

## Global Constraints

- **Pruned-int8 tier only, for every weight file.** Non-pruned int8 (31.7 GB) and bf16 (61.7 GB) diffusion models exceed the 24 GB card before anything else loads. Do not substitute a different tier "to try it."
- **Source repo is `Comfy-Org/MiniMax-H3`, not `MiniMaxAI/MiniMax-H3`.** The latter is the raw Diffusers release and is not loadable by ComfyUI's native nodes.
- **Never trust a hash you did not just fetch.** Query `https://huggingface.co/api/models/Comfy-Org/MiniMax-H3?blobs=true` at download time and read `lfs.sha256` for the exact file, then compare with `Get-FileHash -Algorithm SHA256`. Do not hardcode a hash from a prior read of that page — `docs/plans/uncensored-image-generation.md` records four checksums that were wrong for exactly this kind of reason (an indirect, unverified read stood in for the live authoritative one).
- **ComfyUI must report `>= 0.30.0`** before any H3 workflow is trusted to work at all.
- **Stop the resident Ollama chat model (`ollama stop <model>`) before every generation attempt.** Not optional, not "try coexisting first" — the design already establishes the combined footprint has no realistic chance of coexisting.
- **Filename prefix `homelab-h3`** on every H3 workflow's output node, mirroring `homelab-owui`.
- **No backup step, no retention/pruning step, no nightly check.** The design deliberately scopes these out — do not add them.
- **License/territory question is closed.** The operator confirmed this estate is outside the excluded territories (EU/UK/US/South Korea) on 2026-08-27. Do not re-litigate it mid-implementation.

---

### Task 1: Confirm ComfyUI version, system RAM, and disk headroom

**Files:** None — read-only checks against the GPU host, nothing is created or modified.

**Interfaces:**
- Produces: the measured ComfyUI version, system RAM, and free disk space, recorded in this plan's Task 7 doc update. No later task depends on a specific value here except as a pass/fail gate.

- [ ] **Step 1: Read ComfyUI's version and RAM in one call**

From any machine on the LAN — this is `/system_stats`, already used in `docs/gpu-host.md` to confirm `cuda:0` after install:

```bash
curl -s http://192.168.1.40:8188/system_stats | python -c "
import json, sys
d = json.load(sys.stdin)
print('ComfyUI version:', d['system']['comfyui_version'])
print('RAM total (GB):', round(d['system']['ram_total'] / 1e9, 1))
print('RAM free (GB):', round(d['system']['ram_free'] / 1e9, 1))
"
```

Expected: a version string and two RAM figures. If the key names above don't match what this ComfyUI build actually returns, run `curl -s http://192.168.1.40:8188/system_stats | python -m json.tool` first and read the real structure — the field names above are ComfyUI's documented shape as of this writing, not a guarantee for whatever build is currently installed.

- [ ] **Step 2: Judge against the constraints**

- ComfyUI version `< 0.30.0` → Task 2 is required, not optional.
- RAM total `< 32` GB → record this as a real risk before proceeding; third-party guidance for H3 suggests 32–64 GB, and this repo does not accept an unmeasured assumption in either direction. Continuing anyway is a judgment call for whoever runs this plan, not a silent proceed.

- [ ] **Step 3: Check disk headroom on TERRA**

On the GPU host itself (PowerShell):

```powershell
Get-PSDrive C | Select-Object Used,Free
```

Expected: at least 60 GB free (the five weight files total ~42 GB; leave real headroom, not a razor margin — `docs/gpu-host.md` already treats this as the first check before any large pull for exactly this reason).

---

### Task 2: Upgrade ComfyUI to >= 0.30.0, if Task 1 found it below that

**Files:**
- Modify (host, not tracked in git): `C:\ComfyUI\ComfyUI_windows_portable\`

**Interfaces:**
- Consumes: the version read in Task 1, Step 1.
- Produces: a ComfyUI install that reports `>= 0.30.0` from the same `/system_stats` read.

- [ ] **Step 1: Skip this task entirely if Task 1 already reported >= 0.30.0**

Confirm by re-running the Task 1 Step 1 curl and reading `comfyui_version` again before doing anything below — do not upgrade an already-current install on the assumption Task 1's reading was stale.

- [ ] **Step 2: Stop the running ComfyUI process on TERRA**

```powershell
Get-Process | Where-Object { $_.Path -like '*python_embeded*' } | Stop-Process -Force
```

- [ ] **Step 3: Download the current portable release**

`docs/gpu-host.md` already documents this exact install procedure for the original install; this repeats it for an upgrade. From [ComfyUI's releases](https://github.com/comfyanonymous/ComfyUI/releases), take `ComfyUI_windows_portable_nvidia.7z` — confirm the release notes on that page state a version `>= 0.30.0` before downloading, since GitHub's "latest" can lag a same-day feature landing.

- [ ] **Step 4: Extract over the existing install**

```powershell
tar.exe -xf ComfyUI_windows_portable_nvidia.7z -C C:\ComfyUI --strip-components=0
```

This preserves `C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\` (where existing checkpoints like Pony live) because the archive's own top-level layout matches what's already on disk — confirm this by checking `Get-ChildItem C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\checkpoints\` still lists the existing SDXL/Pony files immediately after extraction, before starting ComfyUI. If they're gone, stop and restore from a backup rather than re-downloading multi-gigabyte checkpoints.

- [ ] **Step 5: Restart via the existing LAN-bound launcher**

```powershell
C:\ComfyUI\start-comfyui-lan.bat
```

(This file already exists per `docs/gpu-host.md`'s original setup — it is not recreated here.)

- [ ] **Step 6: Confirm the version and CUDA both**

```bash
curl -s http://192.168.1.40:8188/system_stats | python -c "
import json, sys
d = json.load(sys.stdin)
print('version:', d['system']['comfyui_version'])
print('device:', d['devices'][0]['type'] if d.get('devices') else 'unknown')
"
```

Expected: version `>= 0.30.0` and a CUDA device, not CPU fallback — the same `cuda:0` check `docs/gpu-host.md` already applies after every ComfyUI (re)install, because a CPU fallback also loads without error and only shows up as twenty-minute generations.

---

### Task 3: Download and verify all five H3 weight files

**Files:**
- Modify (host, not tracked in git):
  - `C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\diffusion_models\minimax_h3_fl2va_pruned_int8_convrot.safetensors`
  - `C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\diffusion_models\minimax_h3_ref2va_pruned_int8_convrot.safetensors`
  - `C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\text_encoders\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`
  - `C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\vae\minimax_h3_video_vae_fp16.safetensors`
  - `C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\vae\minimax_h3_audio_vae_fp32.safetensors`

**Interfaces:**
- Produces: five verified files on disk at the paths above. Task 5 (templates) and Task 6 (generation) both depend on all five being present and correctly named — a ComfyUI workflow referencing a missing or misnamed file fails at validation the same way the original SDXL defect did (`docs/plans/uncensored-image-generation.md`).

- [ ] **Step 1: Confirm the source repo answers, and is not gated**

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  https://huggingface.co/api/models/Comfy-Org/MiniMax-H3?blobs=true
```

Expected: `200`. A `401`/`403` means the repo is gated and needs an HF access token before any download below will work — stop and get one rather than proceeding to a download that will silently land as an HTML login page wearing a `.safetensors` name (the exact failure `docs/gpu-host.md` already warns about for the FLUX VAE).

- [ ] **Step 2: Fetch the authoritative hash for each file, then download and verify — file 1 of 5**

On TERRA:

```powershell
Invoke-WebRequest -Uri "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors" `
  -OutFile "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\diffusion_models\minimax_h3_fl2va_pruned_int8_convrot.safetensors"
```

Then, from any machine, get the authoritative hash for this exact path (do not reuse a hash read from documentation or a prior conversation — fetch it fresh):

```bash
curl -s "https://huggingface.co/api/models/Comfy-Org/MiniMax-H3?blobs=true" | python -c "
import json, sys
d = json.load(sys.stdin)
for f in d.get('siblings', []):
    if f.get('rfilename') == 'diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors':
        print('expected sha256:', f.get('lfs', {}).get('sha256'))
        print('expected bytes:', f.get('size'))
"
```

Then on TERRA, compute the actual hash and byte count and compare both against what the curl above printed:

```powershell
Get-FileHash -Algorithm SHA256 "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\diffusion_models\minimax_h3_fl2va_pruned_int8_convrot.safetensors"
(Get-Item "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\diffusion_models\minimax_h3_fl2va_pruned_int8_convrot.safetensors").Length
```

Both must match exactly. If either disagrees, delete the file and redownload — do not wire a suspect file into a workflow "to see if it works," matching the discipline `docs/gpu-host.md` already applies to Pony (verify before wiring it up).

- [ ] **Step 3: Download and verify file 2 of 5 — `ref2va` diffusion model**

```powershell
Invoke-WebRequest -Uri "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors" `
  -OutFile "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\diffusion_models\minimax_h3_ref2va_pruned_int8_convrot.safetensors"
```

```bash
curl -s "https://huggingface.co/api/models/Comfy-Org/MiniMax-H3?blobs=true" | python -c "
import json, sys
d = json.load(sys.stdin)
for f in d.get('siblings', []):
    if f.get('rfilename') == 'diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors':
        print('expected sha256:', f.get('lfs', {}).get('sha256'))
        print('expected bytes:', f.get('size'))
"
```

```powershell
Get-FileHash -Algorithm SHA256 "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\diffusion_models\minimax_h3_ref2va_pruned_int8_convrot.safetensors"
(Get-Item "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\diffusion_models\minimax_h3_ref2va_pruned_int8_convrot.safetensors").Length
```

Both must match Step 3's curl output exactly, same rule as Step 2.

- [ ] **Step 4: Download and verify file 3 of 5 — text encoder**

```powershell
Invoke-WebRequest -Uri "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors" `
  -OutFile "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\text_encoders\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
```

```bash
curl -s "https://huggingface.co/api/models/Comfy-Org/MiniMax-H3?blobs=true" | python -c "
import json, sys
d = json.load(sys.stdin)
for f in d.get('siblings', []):
    if f.get('rfilename') == 'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors':
        print('expected sha256:', f.get('lfs', {}).get('sha256'))
        print('expected bytes:', f.get('size'))
"
```

```powershell
Get-FileHash -Algorithm SHA256 "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\text_encoders\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
(Get-Item "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\text_encoders\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors").Length
```

- [ ] **Step 5: Download and verify file 4 of 5 — video VAE**

```powershell
Invoke-WebRequest -Uri "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_fp16.safetensors" `
  -OutFile "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\vae\minimax_h3_video_vae_fp16.safetensors"
```

```bash
curl -s "https://huggingface.co/api/models/Comfy-Org/MiniMax-H3?blobs=true" | python -c "
import json, sys
d = json.load(sys.stdin)
for f in d.get('siblings', []):
    if f.get('rfilename') == 'vae/minimax_h3_video_vae_fp16.safetensors':
        print('expected sha256:', f.get('lfs', {}).get('sha256'))
        print('expected bytes:', f.get('size'))
"
```

```powershell
Get-FileHash -Algorithm SHA256 "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\vae\minimax_h3_video_vae_fp16.safetensors"
(Get-Item "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\vae\minimax_h3_video_vae_fp16.safetensors").Length
```

- [ ] **Step 6: Download and verify file 5 of 5 — audio VAE**

```powershell
Invoke-WebRequest -Uri "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors" `
  -OutFile "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\vae\minimax_h3_audio_vae_fp32.safetensors"
```

```bash
curl -s "https://huggingface.co/api/models/Comfy-Org/MiniMax-H3?blobs=true" | python -c "
import json, sys
d = json.load(sys.stdin)
for f in d.get('siblings', []):
    if f.get('rfilename') == 'vae/minimax_h3_audio_vae_fp32.safetensors':
        print('expected sha256:', f.get('lfs', {}).get('sha256'))
        print('expected bytes:', f.get('size'))
"
```

```powershell
Get-FileHash -Algorithm SHA256 "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\vae\minimax_h3_audio_vae_fp32.safetensors"
(Get-Item "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\vae\minimax_h3_audio_vae_fp32.safetensors").Length
```

- [ ] **Step 7: Confirm ComfyUI itself sees all five files**

Restart ComfyUI (it scans model directories at startup) and check the diffusion-model list contains both new checkpoints:

```bash
curl -s http://192.168.1.40:8188/object_info | python -c "
import json, sys
d = json.load(sys.stdin)
for cls in d:
    for input_name, spec in d[cls].get('input', {}).get('required', {}).items():
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            names = [n for n in spec[0] if isinstance(n, str) and 'minimax_h3' in n.lower()]
            if names:
                print(cls, input_name, '->', names)
"
```

Expected: at least one line naming each of the five filenames from Steps 2–6, across whichever loader node classes ComfyUI registered for them — this also answers, without guessing in advance, exactly what those loader node/input names actually are, which the next task needs.

---

### Task 4: Install the three official H3 workflow templates

**Files:** None tracked in git — these are loaded into ComfyUI's own template library / saved workflow storage on the GPU host, not committed to this repo (unlike `inventory/comfyui-workflows/*.json`, which exist only because Open WebUI needs them pushed through its admin API — H3 has no such consumer).

**Interfaces:**
- Consumes: the loader node/input names discovered in Task 3, Step 7.
- Produces: three loadable ComfyUI workflows (T2V, I2V, R2V) with every model dropdown pointed at a real file — verified, not merely "loads without a red node."

- [ ] **Step 1: Open ComfyUI's Template Library**

In a browser, go to `http://192.168.1.40:8188`, open the Template Library, and select the **Video** category.

- [ ] **Step 2: Load the T2V template**

Select the MiniMax H3 text-to-video template. It loads a default graph with its own checkpoint/text-encoder/VAE dropdowns pre-populated (likely to a filename ComfyUI's packager expects, not necessarily the exact filenames from Task 3 — check this, don't assume it lines up).

- [ ] **Step 3: Point every dropdown at the Task 3 files explicitly**

For the diffusion model, text encoder, video VAE, and audio VAE nodes in the loaded graph, set each dropdown to the exact filename downloaded in Task 3 (`minimax_h3_fl2va_pruned_int8_convrot.safetensors`, `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`, `minimax_h3_video_vae_fp16.safetensors`, `minimax_h3_audio_vae_fp32.safetensors`). Do not trust a default selection to already be correct — an empty or wrong default is exactly the class of silent failure `docs/plans/uncensored-image-generation.md` documents for Open WebUI's own default workflow.

- [ ] **Step 4: Set the output filename prefix**

Find the SaveVideo/SaveImage-equivalent output node and set its filename prefix to `homelab-h3`, per the Global Constraints. Save the workflow (ComfyUI's own "Save" — this persists to the GPU host's workflow storage, not to this repo).

- [ ] **Step 5: Repeat Steps 2–4 for the I2V template**

Same dropdowns, same `minimax_h3_fl2va_pruned_int8_convrot.safetensors` diffusion model (I2V shares it with T2V per the design), same `homelab-h3` prefix.

- [ ] **Step 6: Repeat Steps 2–4 for the R2V template**

Same dropdowns except the diffusion model, which is `minimax_h3_ref2va_pruned_int8_convrot.safetensors` — the R2V-specific checkpoint from Task 3.

---

### Task 5: Stop the chat model and generate one real T2V clip

**Files:** None.

**Interfaces:**
- Consumes: the saved T2V workflow from Task 4.
- Produces: the first real measurement of whether this fits the card at all, and the first proof the pipeline produces real output rather than an error swallowed into silence.

- [ ] **Step 1: Record the idle VRAM baseline**

On TERRA:

```powershell
nvidia-smi --query-gpu=memory.used --format=csv
```

Record this number — `docs/gpu-host.md` and `docs/gpu-capacity.md` both treat a measurement taken without this baseline as invalid on its face.

- [ ] **Step 2: Stop the resident chat model**

```powershell
ollama ps
ollama stop <whatever model 'ollama ps' shows resident>
```

Confirm with a second `ollama ps` that nothing is resident before proceeding.

- [ ] **Step 3: Queue the T2V workflow with a real prompt**

In the ComfyUI UI, open the saved T2V workflow, set the prompt to `a plain grey ceramic mug on a wooden table, soft daylight` — matching `scripts/image_generation_check.py`'s own probe prompt, chosen there for being unambiguous to eyeball — and queue it.

- [ ] **Step 4: Watch VRAM while it runs, and record what actually happened**

```powershell
nvidia-smi --query-gpu=memory.used --format=csv
```

Run this a few times during generation. Record the peak. This is the measurement the spec's "VRAM strategy" section explicitly deferred to implementation — write down what you see, including if it fails.

**If it fails with an out-of-memory error:** that is a real, useful result — record it in Task 7's documentation update as a finding, not a blocker to silently work around. Do not change quantization tiers or add offload flags without documenting why in that same doc update; a silent workaround here would repeat the exact mistake `docs/gpu-host.md` warns against for `OLLAMA_KV_CACHE_TYPE` — a fix applied without recording whether it actually took effect.

- [ ] **Step 5: Confirm real output landed, not merely that the queue completed**

```bash
curl -sS http://192.168.1.40:8188/history | python -c "
import json, sys
h = json.load(sys.stdin)
print(len(h), 'entries')
last = list(h.values())[-1]
outputs = last.get('outputs', {})
print(json.dumps(outputs, indent=2)[:2000])
"
```

Expected: at least one entry, with an output listing a filename starting `homelab-h3`. A queue that completes with an empty `outputs` dict is the video-generation equivalent of the image-generation defect that produced a green container and no image for eleven days — treat it exactly that seriously, not as a near-success.

- [ ] **Step 6: Confirm the file itself is real**

On TERRA, find the file in ComfyUI's output directory and confirm it is a plausible size for a several-second video-with-audio clip (tens of megabytes, not a few kilobytes — a truncated or error-page download wearing a video extension is the same failure mode `docs/gpu-host.md` documents for image checkpoints) and that it actually plays.

---

### Task 6: Generate one real I2V clip and one real R2V clip

**Files:** None.

**Interfaces:**
- Consumes: the saved I2V and R2V workflows from Task 4. Chat model is already stopped from Task 5 — confirm it's still stopped rather than re-stopping blindly.

- [ ] **Step 1: Confirm the chat model is still stopped**

```powershell
ollama ps
```

Expected: empty. If something else got loaded since Task 5, stop it.

- [ ] **Step 2: Queue the I2V workflow with a real source image**

Use any real photo (not a synthetic test pattern — I2V's whole point is animating an actual image) as the source, queue it, and wait for completion.

- [ ] **Step 3: Verify I2V output the same way as Task 5, Steps 5–6**

Same `/history` check for a non-empty `outputs` dict with a `homelab-h3`-prefixed filename, same file-size sanity check, same "does it actually play" confirmation.

- [ ] **Step 4: Queue the R2V workflow with real reference inputs**

Use at least one reference image (R2V accepts up to 9 images / 3 clips / 3 audio per the design; one image is sufficient to prove the pipeline works — testing the full 9/3/3 combination is out of scope for this proof), queue it, and wait for completion.

- [ ] **Step 5: Verify R2V output the same way**

Same `/history` and file checks as Step 3.

- [ ] **Step 6: Restart the chat model**

```powershell
ollama run <the model stopped in Task 5, Step 2>
```

Confirm with `ollama ps` that it's resident again before considering this task done — leaving the estate in a state where chat silently has no model loaded is not an acceptable place to stop.

---

### Task 7: Record what was actually measured

**Files:**
- Modify: `docs/gpu-host.md`

**Interfaces:** None — this is the terminal documentation task; nothing later depends on it.

- [ ] **Step 1: Add H3 to the checkpoint list**

In `docs/gpu-host.md`, in the same section that documents the Pony checkpoint (search for "The second checkpoint: Pony Diffusion V6 XL"), add a parallel subsection for MiniMax H3 covering:

- The five filenames, their `Comfy-Org/MiniMax-H3` source paths, and the SHA256 values actually confirmed in Task 3 (copy them from the terminal output of that task, not from this plan — this plan does not hardcode them, per the Global Constraints).
- The measured VRAM peak from Task 5, Step 4, and the idle baseline it was measured against, in the same "State | VRAM used" table format `docs/gpu-host.md` already uses for chat models.
- Whether generation succeeded or OOM'd, plainly — if it failed, say so and say at what point, rather than omitting the section until it works.
- The `homelab-h3` filename prefix and the fact that retention is manual, matching the design's explicit choice — one sentence, referencing `docs/superpowers/specs/2026-08-27-video-generation-design.md` rather than re-explaining the reasoning.
- The measured system RAM from Task 1 and whether it met the 32–64 GB third-party guidance.

- [ ] **Step 2: Commit**

```bash
git add docs/gpu-host.md
git commit -m "docs: record what MiniMax H3 setup actually measured"
```

---

## Finishing

Follow `CLAUDE.md`'s change workflow to close out:

1. `git status --porcelain` prints nothing.
2. There is no `make infra`/`make verify` step for this branch specifically — nothing here touches Ansible-managed state. Run `make validate` anyway to confirm the doc edits didn't break anything it checks (unlikely, but cheap to confirm rather than assume).
3. Merge to `main`, push, delete the branch locally and on the remote.
