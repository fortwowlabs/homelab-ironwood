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

Pull at least a chat model and, if you want Continue's autocomplete and
retrieval to work, a small completion model and an embedding model:

```powershell
ollama pull qwen2.5-coder:14b
ollama pull qwen2.5-coder:1.5b-base
ollama pull nomic-embed-text
```

Confirm from another machine, not from the PC itself — loopback would pass
even with the default bind:

```bash
curl http://192.168.1.40:11434/api/tags
```

### 3. Install ComfyUI

Install [ComfyUI](https://github.com/comfyanonymous/ComfyUI) and start it with
an explicit listen address:

```powershell
python main.py --listen 0.0.0.0 --port 8188
```

Confirm `http://192.168.1.40:8188` loads from another machine.

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

### 5. Tell the homelab it exists

```bash
# inventory/group_vars/all/main.yml
gpu_host_online: true
```

Then `make infra`. Open WebUI restarts with both backends enabled.

Verify by *using* it, not by checking that the container is up: send a chat
message and get a real reply, then generate an image from the same
conversation. A green container proves nothing about whether inference works.

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
  - name: qwen2.5-coder-14b
    provider: ollama
    model: qwen2.5-coder:14b
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

## When the PC is off

Open WebUI stays up and its own login still works; chat requests fail because
the backend is unreachable, and Continue falls back to nothing. If the PC is
going to be off for a while, set `gpu_host_online: false` and run `make infra`
to get the clean "no models" state back instead of a wall of timeouts.

Web search does **not** depend on this machine — SearXNG runs in svc-download's
VPN jail and is unaffected.
