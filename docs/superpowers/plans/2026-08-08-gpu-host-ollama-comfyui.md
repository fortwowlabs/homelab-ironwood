# GPU Host: Ollama + ComfyUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring Ollama and ComfyUI up on the Windows RTX 4090 workstation, LAN-reachable and verified by use, so the Ansible session only has to flip `gpu_host_online: true`.

**Architecture:** Two phases with a hard checkpoint between them. Phase 1 (Tasks 1–3) makes Ollama serve the LAN and proves inference works; stopping there still leaves a working chat backend. Phase 2 (Tasks 4–8) adds ComfyUI for image generation. Task 9 is optional, Task 10 records what was learned back into the repo.

**Tech Stack:** Ollama 0.32.6 (already installed), ComfyUI portable (embedded Python), Windows Defender Firewall, PowerShell.

**Design spec:** [2026-08-08-gpu-host-ollama-comfyui-design.md](../specs/2026-08-08-gpu-host-ollama-comfyui-design.md)

## Global Constraints

- **Firewall rules are scoped to `192.168.1.0/24` and never `Any`.** Neither Ollama nor ComfyUI has any authentication; the address scope is the only control. Never forward either port at the router.
- **Verification runs from another machine, never loopback.** Loopback passes even when the bind address is wrong — that is the exact failure this plan is guarding against.
- **`edgar` (`192.168.1.159`, Windows, on Tailscale as `100.93.219.13`) is the verification host.** It is on the same `/24`, so it is inside the firewall scope. Confirm it is awake before starting.
- **This machine is `192.168.1.40`, held by the *Wi-Fi* adapter** (`14-AC-60-D5-F4-DB`). `gpu_host_ip` in the repo already matches; no inventory edit is needed. Do not plug in Ethernet mid-plan — it would pull a different address and split routing.
- **Tasks 1 and 6 need an elevated PowerShell.** Machine-scope environment variables and `New-NetFirewallRule` both require Administrator. The agent session is **not** elevated; those blocks must be run by the user in an admin prompt.
- **`make validate` is not run in this plan.** It cannot execute on this machine — no `make`, no `.venv`, no ansible/shellcheck/gitleaks, so `validate-tools` exits 127. Task 10 runs `tests/validate_links.py` directly instead, which is the only gate a docs-only change can break. CI on push to `main` runs the full suite.
- **VRAM budget is ~22 GB usable of 24564 MiB.** Models will evict each other; that is designed, not a bug. See the spec's contention section.

## File Structure

Almost all of this plan's product is machine state, not files. Only two files change:

| File | Responsibility |
|---|---|
| `docs/superpowers/plans/2026-08-08-gpu-host-ollama-comfyui.md` | This plan (already created) |
| `docs/gpu-host.md` | The durable operator doc — updated in Task 10 with what execution proved |

Machine state established, in order: `OLLAMA_HOST` (Machine scope) → two firewall rules → four Ollama models → `C:\ComfyUI\` → an SDXL checkpoint → a Startup shortcut.

---

### Task 1: Make Ollama listen on the LAN

Ollama is currently running and bound to `127.0.0.1:11434` — loopback only. This task changes the bind and opens the port. Both halves need elevation, so they are one task to keep it to a single admin prompt.

**Files:**
- Create: none
- Modify: none (machine state only)
- Verify: `Get-NetTCPConnection`, `Get-NetFirewallRule`

**Interfaces:**
- Consumes: nothing
- Produces: Ollama answering on `192.168.1.40:11434` for the `192.168.1.0/24` subnet. Tasks 2, 3, and 9 all depend on this.

- [ ] **Step 1: Confirm the current (broken) state**

```powershell
Get-NetTCPConnection -State Listen -LocalPort 11434 | Select-Object LocalAddress, LocalPort
```

Expected: `LocalAddress` is `127.0.0.1`. This is the state being fixed — record it so the change is provable rather than assumed.

- [ ] **Step 2: Confirm `OLLAMA_HOST` is unset in both scopes**

```powershell
"User:    " + [System.Environment]::GetEnvironmentVariable("OLLAMA_HOST","User")
"Machine: " + [System.Environment]::GetEnvironmentVariable("OLLAMA_HOST","Machine")
```

Expected: both empty.

- [ ] **Step 3: Set the variable and open the port — RUN AS ADMINISTRATOR**

Open a PowerShell window with *Run as administrator*, then:

```powershell
[System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0:11434", "Machine")

New-NetFirewallRule -DisplayName "Ollama (LAN)" -Direction Inbound `
  -Protocol TCP -LocalPort 11434 -RemoteAddress 192.168.1.0/24 -Action Allow
```

It must be **Machine** scope, not User. Ollama runs as a background process started at login and will not see a User-scoped or shell-exported variable. This is the single most likely mistake in the plan.

- [ ] **Step 4: Restart Ollama so it picks up the variable**

A new terminal is not enough — the running server process must exit.

```powershell
Get-Process -Name "ollama", "ollama app" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Start-Process "C:\Users\tv\AppData\Local\Programs\Ollama\ollama app.exe"
Start-Sleep -Seconds 5
```

- [ ] **Step 5: Verify the bind actually changed**

```powershell
Get-NetTCPConnection -State Listen -LocalPort 11434 | Select-Object LocalAddress, LocalPort
```

Expected: `LocalAddress` is now `0.0.0.0` (it was `127.0.0.1` in Step 1). If it still reads `127.0.0.1`, the variable was set in the wrong scope or the process did not actually restart — fix that before continuing; nothing downstream will work.

- [ ] **Step 6: Verify the firewall rule is scoped, not open**

```powershell
Get-NetFirewallRule -DisplayName "Ollama (LAN)" |
  Get-NetFirewallAddressFilter | Select-Object RemoteAddress
```

Expected: `192.168.1.0/255.255.255.0`.

**Windows normalises `/24` into full-netmask form**, so the value you typed in Step 3 is not the value that reads back. That is the same scope, not a drift — do not "fix" it. An automated equality check against the literal string `192.168.1.0/24` will fail here for no reason.

If it reads `Any`, delete the rule and recreate it — an unauthenticated inference server open to every interface is the one outcome this plan must not produce.

---

### Task 2: Pull the model set

**Files:**
- Create: none (models land in `C:\Users\tv\.ollama\models`)
- Modify: none

**Interfaces:**
- Consumes: Ollama running (Task 1)
- Produces: four models — `qwen3:30b` (Open WebUI chat), `qwen2.5-coder:14b`, `qwen2.5-coder:1.5b-base`, `nomic-embed-text` (Continue). Tasks 3 and 9 reference these names exactly.

- [ ] **Step 1: Confirm no models are present yet**

```powershell
ollama list
```

Expected: header row only, no models.

- [ ] **Step 2: Pull the general chat model**

```powershell
ollama pull qwen3:30b
```

19 GB — this takes a while. `qwen3:30b` is chosen over `qwen3:32b` (20 GB) deliberately: against ~22 GB usable, the 32B leaves almost nothing for KV cache at longer contexts, which forces partial CPU offload and slows generation badly. If you want the denser model anyway, `qwen3:32b` is the substitute and everything else in this plan is unchanged.

- [ ] **Step 3: Pull the three Continue models**

```powershell
ollama pull qwen2.5-coder:14b
ollama pull qwen2.5-coder:1.5b-base
ollama pull nomic-embed-text
```

- [ ] **Step 4: Verify all four are present**

```powershell
ollama list
```

Expected: four rows — `qwen3:30b`, `qwen2.5-coder:14b`, `qwen2.5-coder:1.5b-base`, `nomic-embed-text`. Total roughly 30 GB against 2.4 TB free.

---

### Task 3: Verify Ollama end-to-end — PHASE 1 CHECKPOINT

This is the gate that matters. Do not start Phase 2 until it passes.

**Files:**
- Create: none
- Modify: none

**Interfaces:**
- Consumes: Tasks 1 and 2
- Produces: proof that inference works over the LAN. The handoff to the Ansible session depends on this.

- [ ] **Step 1: Confirm generation works locally**

```powershell
Invoke-RestMethod -Uri http://localhost:11434/api/generate -Method Post `
  -Body '{"model":"qwen3:30b","prompt":"Reply with exactly: OK","stream":false}' |
  Select-Object -ExpandProperty response
```

Expected: real generated text. First run loads 19 GB into VRAM and may take 30+ seconds.

`Invoke-RestMethod` rather than `curl.exe` throughout this task: PowerShell 5.1 mangles quotes when passing a JSON body to a native executable, and debugging that is a distraction from the thing actually being tested.

- [ ] **Step 2: Confirm the model list is reachable from another machine**

On **edgar** (`192.168.1.159`), not on this machine:

```powershell
Invoke-RestMethod -Uri http://192.168.1.40:11434/api/tags |
  Select-Object -ExpandProperty models | Select-Object name
```

Expected: the four model names. This proves the bind *and* the firewall rule. A connection error here means Task 1 Step 5 or Step 6 did not really pass.

- [ ] **Step 3: Confirm generation works from another machine — THE POSITIVE CONTROL**

On **edgar**:

```powershell
Invoke-RestMethod -Uri http://192.168.1.40:11434/api/generate -Method Post `
  -Body '{"model":"qwen3:30b","prompt":"Reply with exactly: OK","stream":false}' |
  Select-Object -ExpandProperty response
```

Expected: real generated text.

**Step 2 passing is not a substitute for this.** `/api/tags` populates from the model list even when generation is broken — [gpu-host.md](../../gpu-host.md) already documents that trap for Continue. Treating a green Step 2 as success is exactly how a broken backend ships looking healthy.

- [ ] **Step 4: Record the checkpoint**

Phase 1 is complete: Open WebUI now has a working chat backend the moment `gpu_host_online` flips. If you stop here, that is a coherent stopping point.

---

### Task 4: Install ComfyUI

**Files:**
- Create: `C:\ComfyUI\` (extracted portable build)
- Modify: none

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: the extracted tree at `C:\ComfyUI\ComfyUI_windows_portable\`, whose `python_embeded\python.exe` and `ComfyUI\models\checkpoints\` paths Tasks 5 and 7 both hard-code.

- [ ] **Step 1: Download the portable build**

From <https://github.com/comfyanonymous/ComfyUI/releases> — the latest release's `ComfyUI_windows_portable_nvidia.7z` asset (~1.5 GB). Download by hand in a browser; scripting a GitHub release download is more brittle than doing it once.

Note it is a **`.7z`, not a `.zip`**. Windows 11 (this build, 26200) extracts 7z natively via Explorer. If extraction fails, install 7-Zip rather than fighting it.

- [ ] **Step 2: Extract to `C:\ComfyUI\`**

Extract so the result is `C:\ComfyUI\ComfyUI_windows_portable\`.

Short and space-free is deliberate: ComfyUI's embedded Python and its custom-node tooling have a long history of breaking on paths containing spaces, and the Startup shortcut in Task 8 hard-codes this path.

- [ ] **Step 3: Verify the expected files exist**

```powershell
Test-Path "C:\ComfyUI\ComfyUI_windows_portable\run_nvidia_gpu.bat"
Test-Path "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\checkpoints"
```

Expected: `True` for both. If the second is `False`, the archive nested differently than expected — locate the real `ComfyUI\models\checkpoints` path and use it consistently in Task 5.

---

### Task 5: Install an image checkpoint

A fresh ComfyUI ships **zero** image models. Without this task the UI loads perfectly and every generation fails — indistinguishable from a working install until something is actually generated.

**Files:**
- Create: `C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\checkpoints\sd_xl_base_1.0.safetensors`
- Modify: none

**Interfaces:**
- Consumes: Task 4's directory layout
- Produces: a loadable checkpoint. Task 9's generation gate depends on it.

- [ ] **Step 1: Confirm the checkpoints directory is empty**

```powershell
Get-ChildItem "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\checkpoints"
```

Expected: empty (or only a `put_checkpoints_here` placeholder). This is the state Task 9 would otherwise fail on.

- [ ] **Step 2: Download SDXL base**

```powershell
Invoke-WebRequest `
  -Uri "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors" `
  -OutFile "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\checkpoints\sd_xl_base_1.0.safetensors"
```

~6.9 GB. SDXL rather than Flux (~12 GB at fp8) is deliberate: with a 19 GB chat model often already resident, the smaller image model is what makes concurrent use merely slow instead of impossible.

If this returns 401/403, the repository now requires acceptance of its licence — open the URL in a browser, accept, and download manually to the same path.

- [ ] **Step 3: Verify the file landed and is plausibly complete**

```powershell
Get-Item "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\checkpoints\sd_xl_base_1.0.safetensors" |
  Select-Object Name, @{n="GB";e={[math]::Round($_.Length/1GB,2)}}
```

Expected: roughly 6.9 GB. A file of a few KB is an HTML error page saved under a `.safetensors` name — delete it and redo Step 2.

---

### Task 6: Open ComfyUI's port

**Files:**
- Create: none
- Modify: none (machine state only)

**Interfaces:**
- Consumes: nothing
- Produces: TCP 8188 reachable from `192.168.1.0/24`. Task 9 depends on it.

- [ ] **Step 1: Add the rule — RUN AS ADMINISTRATOR**

```powershell
New-NetFirewallRule -DisplayName "ComfyUI (LAN)" -Direction Inbound `
  -Protocol TCP -LocalPort 8188 -RemoteAddress 192.168.1.0/24 -Action Allow
```

- [ ] **Step 2: Verify the scope**

```powershell
Get-NetFirewallRule -DisplayName "ComfyUI (LAN)" |
  Get-NetFirewallAddressFilter | Select-Object RemoteAddress
```

Expected: `192.168.1.0/255.255.255.0`, for the normalisation reason in Task 1 Step 6. Same scope requirement — ComfyUI has no authentication either.

**Practical note:** this rule needs the same elevation as Task 1 Step 3, so create both rules in that one admin prompt rather than opening a second one here. The port simply sits closed-but-ruled until Task 7 starts something listening on it.

---

### Task 7: Launch ComfyUI bound to the LAN

**Files:**
- Create: `C:\ComfyUI\start-comfyui-lan.bat`
- Modify: none

**Interfaces:**
- Consumes: Tasks 4, 5, 6
- Produces: `C:\ComfyUI\start-comfyui-lan.bat`, the exact file Task 8's shortcut points at.

- [ ] **Step 1: Create the launcher**

The stock `run_nvidia_gpu.bat` binds to loopback only. Write `C:\ComfyUI\start-comfyui-lan.bat` containing:

```bat
@echo off
cd /d C:\ComfyUI\ComfyUI_windows_portable
.\python_embeded\python.exe -s ComfyUI\main.py --listen 0.0.0.0 --port 8188 --windows-standalone-build
```

A dedicated launcher rather than editing the stock one, so a ComfyUI update that overwrites `run_nvidia_gpu.bat` does not silently revert the LAN bind.

- [ ] **Step 2: Run it**

```powershell
Start-Process "C:\ComfyUI\start-comfyui-lan.bat"
```

First launch is slow — the embedded Python initialises and Torch loads CUDA.

- [ ] **Step 3: Verify it bound to all interfaces, not loopback**

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8188 | Select-Object LocalAddress, LocalPort
```

Expected: `0.0.0.0`. If it shows `127.0.0.1`, the `--listen` flag did not take — check the batch file for a typo before continuing.

---

### Task 8: Auto-start ComfyUI at login

Open WebUI's image generation assumes the backend is simply there whenever `gpu_host_online` is true. A service needing a manual launch after every reboot would not hold up that assumption.

**Files:**
- Create: `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ComfyUI-LAN.lnk`
- Modify: none

**Interfaces:**
- Consumes: Task 7's launcher path
- Produces: ComfyUI surviving reboots unattended

- [ ] **Step 1: Create the Startup shortcut**

```powershell
$startup = [System.Environment]::GetFolderPath('Startup')
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("$startup\ComfyUI-LAN.lnk")
$sc.TargetPath = "C:\ComfyUI\start-comfyui-lan.bat"
$sc.WorkingDirectory = "C:\ComfyUI"
$sc.WindowStyle = 7
$sc.Save()
```

`WindowStyle = 7` starts it minimised so it does not steal focus at every login.

- [ ] **Step 2: Verify the shortcut exists and points at the right file**

```powershell
$startup = [System.Environment]::GetFolderPath('Startup')
$ws = New-Object -ComObject WScript.Shell
$ws.CreateShortcut("$startup\ComfyUI-LAN.lnk").TargetPath
```

Expected: `C:\ComfyUI\start-comfyui-lan.bat`

- [ ] **Step 3: Prove it actually survives a reboot**

Reboot, log in, wait ~60 seconds, then:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8188 | Select-Object LocalAddress
```

Expected: `0.0.0.0`, with nothing launched by hand. Skipping this reduces Task 8 to an untested assumption — and an auto-start that silently does not fire is precisely the kind of thing nobody notices until image generation fails weeks later.

Ollama's own auto-start is verified by the same reboot: re-run Task 3 Step 1 and confirm it still answers.

---

### Task 9: Verify ComfyUI end-to-end — PHASE 2 CHECKPOINT

**Files:**
- Create: none
- Modify: none

**Interfaces:**
- Consumes: Tasks 4–8
- Produces: proof image generation works. The Ansible session's image-in-chat verification depends on it.

- [ ] **Step 1: Confirm the UI loads from another machine**

On **edgar**, open `http://192.168.1.40:8188` in a browser.

Expected: the ComfyUI graph editor loads. This proves bind + firewall — and nothing about whether generation works.

- [ ] **Step 2: Confirm the checkpoint is actually visible to ComfyUI**

In that UI, on the `Load Checkpoint` node, open the `ckpt_name` dropdown.

Expected: `sd_xl_base_1.0.safetensors` is listed. If the dropdown is empty, ComfyUI is not reading the directory Task 5 wrote to — recheck the path from Task 4 Step 3.

- [ ] **Step 3: Generate an actual image — THE REAL GATE**

Click **Queue Prompt** on the default workflow.

Expected: a PNG appears in the output node within a minute or two.

A loaded UI and a working UI look identical until this step. If it errors with CUDA out-of-memory, the chat model is resident — either wait out `OLLAMA_KEEP_ALIVE` (5 min default) or run `ollama stop qwen3:30b`, then retry. That contention is expected on a 24 GB card, not a defect.

- [ ] **Step 4: Confirm both services coexist**

With the image generated, from **edgar** re-run the Task 3 Step 3 generate call.

Expected: a real response, though possibly after a reload pause while the chat model comes back into VRAM. This documents the swap behaviour as observed rather than predicted.

---

### Task 10: Record what execution proved

**Files:**
- Modify: `docs/gpu-host.md`
- Verify: `tests/validate_links.py`

**Interfaces:**
- Consumes: observations from Tasks 1–9
- Produces: the durable operator doc, corrected

- [ ] **Step 1: Update `docs/gpu-host.md`**

Fold in what this plan established, replacing the pre-hardware guesses:

1. The model set — add `qwen3:30b` for Open WebUI chat alongside the three existing Continue models, with the 19 GB / VRAM-headroom reasoning.
2. A checkpoint is **required** for ComfyUI; note SDXL base and the `models\checkpoints` path.
3. The VRAM contention section — 24 GB will not hold the chat model plus either the 14b coder or a checkpoint, and Open WebUI triggers image generation from inside chat, so it is the common path.
4. `192.168.1.40` is held by the **Wi-Fi** adapter; moving to Ethernet means moving the reservation, not adding one.
5. ComfyUI's LAN launcher and Startup shortcut (`C:\ComfyUI\start-comfyui-lan.bat`), since the stock `run_nvidia_gpu.bat` binds loopback only.
6. Continue on *this* machine should target `localhost:11434`; the existing `192.168.1.40` sample stays correct for a separate laptop.

- [ ] **Step 2: Run the one gate that applies**

```bash
python3 tests/validate_links.py
```

Expected: `Local Markdown links: OK`

`make validate` is deliberately not run — it cannot execute on this machine (see Global Constraints).

- [ ] **Step 3: Commit**

```bash
git add docs/gpu-host.md
git commit -m "docs: correct gpu-host.md from the real setup run"
```

Stage the explicit path. Never `git add -A` — [CLAUDE.md](../../../CLAUDE.md) notes the repo root holds working notes quoting live credentials.

---

### Task 11 (optional): Continue on this workstation

Non-blocking. Skip entirely if you only code on another machine.

**Files:**
- Create: `C:\Users\tv\.continue\config.yaml`
- Modify: none

**Interfaces:**
- Consumes: Task 2's models
- Produces: nothing other tasks depend on

- [ ] **Step 1: Write the config**

Create `C:\Users\tv\.continue\config.yaml`:

```yaml
name: homelab-gpu-local
version: 0.0.1
schema: v1
models:
  - name: qwen2.5-coder-14b
    provider: ollama
    model: qwen2.5-coder:14b
    apiBase: http://localhost:11434
    roles: [chat, edit, apply]
  - name: qwen2.5-coder-1.5b
    provider: ollama
    model: qwen2.5-coder:1.5b-base
    apiBase: http://localhost:11434
    roles: [autocomplete]
  - name: nomic-embed-text
    provider: ollama
    model: nomic-embed-text
    apiBase: http://localhost:11434
    roles: [embed]
```

`localhost`, not `192.168.1.40` — Continue runs on this same box, so this is lower latency and unaffected by Wi-Fi.

- [ ] **Step 2: Test autocomplete in a real file**

Open a code file in VSCode and type until autocomplete fires.

Do not trust the model list as evidence — it populates from `/api/tags` even when generation is broken, the same trap as Task 3 Step 3.

---

## Handoff to the Ansible session

Not part of this plan; runs on the machine that manages `thurgadin`.

1. Set `gpu_host_online: true` in [main.yml](../../../inventory/group_vars/all/main.yml).
2. `make infra`.
3. Verify by **using** Open WebUI: a real chat reply, and an image generated in-conversation.

`gpu_host_ip` needs no edit — `192.168.1.40` is already live.
