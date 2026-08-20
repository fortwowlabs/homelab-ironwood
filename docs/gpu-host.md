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
