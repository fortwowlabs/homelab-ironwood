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

`gpu_host_online: false` is the default and is the state to leave it in until
the PC actually exists and answers. While it is false, Open WebUI deploys with
`ENABLE_OLLAMA_API` and `ENABLE_IMAGE_GENERATION` switched off, so the chat UI
offers no models rather than throwing connection errors at a machine that
isn't there.

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

Pull the four chat models Open WebUI offers, the coding model, and the two
small models Continue needs for autocomplete and embeddings:

```powershell
# Chat — all four are abliterated (see docs/chat-models.md)
ollama pull huihui_ai/gemma-4-abliterated:26b     # default
ollama pull huihui_ai/Qwen3.6-abliterated:27b     # technical work
ollama pull huihui_ai/gemma-4-abliterated:31b     # see the CPU-spill warning
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

The fix needs an elevated shell **on TERRA**:

```powershell
Get-NetFirewallRule -DisplayName "ollama.exe" | Disable-NetFirewallRule
```

Then re-run both curls from a tailnet peer and require that they stop
answering while `192.168.1.40` still does — a check that fails closed rather
than one that merely looks quiet. Re-check after Ollama updates; the installer
created these rules and may recreate them.

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

Measured 2026-08-09, one model at a time, each stopped before the next was
loaded. **Two of the six do not fit and silently spill to CPU** — Ollama does
not warn, it just runs slowly, so `ollama ps` is the only place this is
visible.

| Model | Resident | Processor | GPU used of 24564 MiB |
|---|---|---|---|
| `huihui_ai/gemma-4-abliterated:26b` | 17 GB | **100% GPU** | 20339 MiB |
| `huihui_ai/Qwen3.6-abliterated:27b` | 18 GB | **100% GPU** | 20411 MiB |
| `davidau-fable-fusion:27b-q4km` | 19 GB | **100% GPU** | 20800 MiB |
| `qwen3-coder:30b` | 21 GB | **100% GPU** | 22634 MiB |
| `huihui_ai/gemma-4-abliterated:31b` @ `num_ctx` 16384 | 20 GB | **100% GPU** | 23465 MiB |
| `huihui_ai/gemma-4-abliterated:31b` @ default 32768 | 21 GB | ⚠️ 10%/90% CPU/GPU | 23626 MiB |

The practical ceiling is around **21 GB resident**. Above that Ollama offloads
layers to system RAM and generation slows by roughly an order of magnitude.

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
