# GPU host: Ollama + ComfyUI setup (Windows side)

**Date:** 2026-08-08
**Machine:** the Windows 11 / RTX 4090 workstation described in
[gpu-host.md](../../gpu-host.md)

## Context

[gpu-host.md](../../gpu-host.md) has described the GPU host's setup since
before the hardware existed. The machine now exists, so this spec is the
execution design for actually doing it, written *on* that machine against its
measured specs rather than against the placeholder assumptions the original
doc was written under.

Three things the original doc could not know, which this spec corrects:

1. **The model set was chosen for Continue only.** Open WebUI is a general
   chat UI on the same Ollama and needs a general-purpose model, which was
   never specified.
2. **A fresh ComfyUI install ships no image models.** "Install ComfyUI and
   start it" produces a UI that loads perfectly and fails every generation —
   precisely the green-process-broken-service trap [CLAUDE.md](../../../CLAUDE.md)
   warns about.
3. **24 GB of VRAM cannot hold everything at once.** The doc implies Ollama
   and ComfyUI coexist freely. They contend, and that contention is
   structural, not a misconfiguration.

The intended outcome is both services running, LAN-reachable, and verified by
*use* — leaving only `gpu_host_online: true` + `make infra` for the Ansible
session to complete the go-live.

## Measured hardware

Gathered on this machine on 2026-08-08, not assumed:

| | |
|---|---|
| GPU | NVIDIA RTX 4090, **24564 MiB** VRAM, driver 610.62, compute cap 8.9 |
| CPU | AMD Ryzen 7 7800X3D, 8 cores / 16 threads |
| RAM | 31.1 GB |
| Disk | `C:` ~2.4 TB free |
| OS | Windows 11 Pro, 10.0.26200 |

An integrated AMD Radeon adapter is also present; it is irrelevant here beyond
being the reason a second video controller shows up in inventory.

**Starting state (all verified, not assumed):** Ollama is already installed at
`C:\Users\tv\AppData\Local\Programs\Ollama\ollama.exe` but is not running and
has **zero models pulled**. `OLLAMA_HOST` is unset in both User and Machine
scopes. No firewall rules exist for either port. ComfyUI is absent. No Python
or conda is on the Windows `PATH`.

### Addressing

`192.168.1.40` — the address [main.yml](../../../inventory/group_vars/all/main.yml)
already pins as `gpu_host_ip` — is **live and held by the Wi-Fi adapter**
(`14-AC-60-D5-F4-DB`, linked at 1.2 Gbps). No repo edit is needed.

Ethernet (`74-56-3C-B3-C6-C5`) is disconnected. Wi-Fi is adequate here: LLM
responses are text, and ComfyUI images are a few MB. **If wired is ever
wanted, move the existing DHCP reservation to the Ethernet MAC rather than
adding a second one** — two reservations would split routing, with Windows
preferring the wired route outbound while `.40` stayed on Wi-Fi inbound.

## Scope

**In scope:** everything on this workstation — Ollama configuration, model
pulls, ComfyUI install and auto-start, both firewall rules, and local
verification of both services.

**Out of scope:** anything in Ansible or on `thurgadin`. The DHCP reservation
is already done. Flipping `gpu_host_online` and running `make infra` happen in
a separate session and are listed under [Handoff](#handoff).

## Approach

Scripted where the work is idempotent and unattended; manual where it needs a
browser download or a judgment call. Two phases with a hard checkpoint between
them, so that stopping after Phase 1 still leaves a working chat backend.

## Phase 1 — Ollama

### 1.1 Bind to the LAN

Set `OLLAMA_HOST = 0.0.0.0:11434` as a **System** environment variable
(*System Properties → Environment Variables → System variables*), then restart
Ollama fully — quit it from the system tray so the background process actually
exits, rather than just opening a new terminal.

This is the single most likely mistake in the whole plan. Ollama runs as a
background service started at login; a User-scoped variable, or one exported
in a shell, will not be seen. The failure is quiet and asymmetric — everything
works locally and every LAN connection is refused.

### 1.2 Firewall

One inbound rule, TCP 11434, scoped to `192.168.1.0/24` — never `Any`. The
command is already in [gpu-host.md](../../gpu-host.md) and is reused verbatim.

Ollama has **no authentication**. The `-RemoteAddress` scope is the only
control between it and anything else that can route to this machine, so it
must not be widened and the port must not be forwarded at the router.

### 1.3 Models

Two-model design, accepting eviction between them (see
[VRAM contention](#vram-contention)):

| Model | Role | Approx VRAM (Q4) |
|---|---|---|
| ~30B-class general instruct | Open WebUI chat | ~18–20 GB |
| `qwen2.5-coder:14b` | Continue chat / edit / apply | ~9 GB |
| `qwen2.5-coder:1.5b-base` | Continue autocomplete | ~1 GB |
| `nomic-embed-text` | Continue embeddings | ~0.3 GB |

Total disk is roughly 30 GB against 2.4 TB free — a non-issue.

**The general chat model's exact tag is an execution-time decision, not a
value fixed here.** The criteria: general-purpose instruct model, ~30B
parameters, Q4_K_M quantization, resident in ~19–20 GB. Candidates seen in
Ollama's library at design time include Mistral Small 3 (24B), Command R
(35B), and Aya (35B); the library moves faster than this document will, so
check it at execution rather than trusting this list. The three coding models
carry over from [gpu-host.md](../../gpu-host.md) unchanged.

### 1.4 Phase 1 gates

All three run **from another machine**. Loopback would pass even with a wrong
bind, which is the whole failure this phase risks.

1. `curl http://192.168.1.40:11434/api/tags` returns the model list — proves
   the LAN bind and the firewall rule.
2. `curl http://192.168.1.40:11434/api/generate` with a real prompt returns
   real generated text.
3. The firewall rule's `RemoteAddress` reads `192.168.1.0/24`, not `Any`.

**Gate 2 is the positive control and gate 1 does not substitute for it.**
`/api/tags` populates from the model list even when generation is broken —
[gpu-host.md](../../gpu-host.md) already documents this trap for Continue.
Treating a passing gate 1 as success is how a broken backend ships looking
healthy.

## Phase 2 — ComfyUI

### 2.1 Install

The official **portable/standalone** Windows build, extracted to
`C:\ComfyUI\` — a short, space-free path chosen because the embedded Python
and its custom-node tooling have a long history of breaking on paths with
spaces, and because the auto-start shortcut in 2.3 hard-codes it.

It bundles an embedded Python, which matters because no Python is on this
machine's `PATH` and a gaming workstation is a poor place to start managing
system Python versions. The archive (~1.5 GB) is downloaded by hand from
ComfyUI's GitHub releases; scripting that download is more brittle than doing
it once. Note it ships as a **`.7z`, not a `.zip`** — Windows 11 extracts that
natively, but it is a surprise worth knowing before the download finishes.

Extraction yields `C:\ComfyUI\ComfyUI_windows_portable\`, so checkpoints go in
`C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\checkpoints\`.

### 2.2 A checkpoint is mandatory

A fresh ComfyUI has no image models. **Without a checkpoint the UI loads
normally and every generation fails**, which is indistinguishable from a
working install until something is actually generated.

Use an SDXL-class checkpoint (~7 GB) rather than Flux (~12 GB at fp8), for the
contention reason below.

### 2.3 Launch and auto-start

Launched with `--listen 0.0.0.0 --port 8188`, via a shortcut in the Startup
folder (`shell:startup`), so it survives reboots unattended. Open WebUI's image
generation assumes the backend is simply there whenever `gpu_host_online` is
true; a service that needs a manual launch after every reboot would not hold
up that assumption.

The shortcut points at a **separate launcher script**, not at the portable
build's stock `run_nvidia_gpu.bat` — that one binds loopback only, and editing
it in place would be silently reverted by the next ComfyUI update, taking the
LAN bind with it.

Second inbound firewall rule, TCP 8188, scoped to `192.168.1.0/24`. ComfyUI
has no authentication either — same constraint as Ollama.

### 2.4 Phase 2 gates

1. `http://192.168.1.40:8188` loads from another machine.
2. A queued generation returns an actual PNG.

Gate 2 is the real one, for the reason in 2.2.

## VRAM contention

24564 MiB total, minus ~1–2 GB held by the Windows desktop and browser, leaves
roughly **22 GB usable**. Against that:

- 30B chat model (~19 GB) + `qwen2.5-coder:14b` (~9 GB) = ~28 GB. They cannot
  both stay resident. Alternating between Open WebUI and Continue evicts and
  reloads, and a ~19 GB reload off NVMe stalls for tens of seconds.
- 30B chat model (~19 GB) + SDXL (~7 GB) = ~26 GB. Also over budget — and this
  one is not hypothetical, because Open WebUI triggers image generation *from
  inside a chat conversation*, so the chat model is typically already resident
  when ComfyUI asks.

`OLLAMA_KEEP_ALIVE` (default 5 minutes) is the release valve; shortening it
frees VRAM faster between turns at the cost of more reloads. Choosing SDXL
over Flux keeps the image side's demand as small as practical.

**Stated plainly: on a single 24 GB card that is also a gaming PC, concurrent
LLM and image generation will sometimes be slow or fail.** That is inherent to
the hardware budget, not a defect to debug, and it should not be treated as a
regression when it happens.

## Continue (optional, non-blocking)

Continue runs on this same workstation, so it should point at
`http://localhost:11434`, not at `192.168.1.40` — lower latency, and
unaffected by Wi-Fi. This differs from the sample in
[gpu-host.md](../../gpu-host.md), which assumes Continue runs on a separate
laptop; both remain valid for their respective machines.

Test autocomplete in a real file. The model list populates from `/api/tags`
even when generation is broken.

## Failure modes

| Symptom | Cause |
|---|---|
| Works locally, refused from LAN | `OLLAMA_HOST` set User-scoped instead of System, or Ollama not fully restarted |
| Model list loads, chat produces nothing | Generation broken; `/api/tags` is not evidence of inference |
| ComfyUI UI loads, generations all fail | No checkpoint installed |
| CUDA OOM mid-generation | Chat model and image model both resident — see contention |
| Reachable at `.40` but routes oddly | Ethernet plugged in while the reservation is on the Wi-Fi MAC |

## Handoff

For the Ansible session, after both phases pass their gates:

1. Set `gpu_host_online: true` in
   [main.yml](../../../inventory/group_vars/all/main.yml).
2. `make infra`.
3. Verify by **using** Open WebUI: a real chat reply, and an image generated
   in-conversation. A green container proves nothing about whether inference
   works.

No other repo change is required — `gpu_host_ip` already matches reality.
