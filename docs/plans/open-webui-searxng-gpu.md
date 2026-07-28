# Open WebUI + SearXNG (VPN-jailed) + Continue/Win11 GPU host

## Context

Add a self-hosted AI stack: **Open WebUI** (chat + image generation) on svc-infra, backed by a **Windows 11 PC with an RTX 4090** that does not exist yet, and **SearXNG** inside the Mullvad VPN jail on svc-download as Open WebUI's *exclusive* web-search provider — so every search upstream query exits via the VPN. **Continue for VSCode** on the workstation will talk directly to the PC's Ollama (no homelab dependency in the coding loop).

User decisions: Ollama + ComfyUI on the PC; Continue → direct to PC IP; Open WebUI keeps its own login (NOT behind Authelia SSO); hostname **chat.fortwow.dev**; **reserve the PC's IP now** and bake it into configs (pick `192.168.1.40`; DHCP reservation to be made when the PC arrives).

There is zero AI/GPU precedent in this repo — everything lands via the two existing catalogs, no new role code.

## Verified facts the plan builds on

- **Ports:** svc-infra free → **3007** for Open WebUI (taken: 8000/8080/8081/8082/8090/8123/8222/8384/8443/2283/3000/3001/3003/3005/3007✗→free/8001/61208/9925/9091/9092/9100). VPN jail is ONE shared netns; taken listen ports 8080(sabnzbd)/9696/8989/7878/5299/8084/6767/5800 → **8888** for SearXNG (its default 8080 collides with SABnzbd; moved via settings.yml).
- **Jail plumbing is fully derived from `download_apps`** (`proxy: true` → quadlet, systemd socket proxy on the LAN IP, nftables allow from lan_cidr, Caddy vhost + dnsmasq name, Homepage tile, verify probe, leak-canary membership). Socket proxy dials `10.77.0.2:<ui_port>`, so the container must bind **0.0.0.0:8888**. All jail egress goes via Mullvad by construction (only route is wg0). Jail has no LAN access / no `*.fortwow.dev` DNS — reached inbound only via the proxy at `<svc-download-ip>:8888`, which svc-infra can hit (nft allows lan_cidr → catalog ports).
- **Secret handling:** the download-catalog quadlet render is NOT no_log → SearXNG's `secret_key` cannot go in catalog `env`. Precedent: Recyclarr's bespoke config render in `roles/svc_download/tasks/files.yml` (no_log + diff:false + registered var driving a conditional restart in `apps.yml`). `tests/validate_secret_tasks.py` auto-enforces no_log on any template containing `vault_`.
- **Open WebUI PersistentConfig trap:** `OLLAMA_BASE_URL`, `COMFYUI_BASE_URL`, `ENABLE_IMAGE_GENERATION`, `WEB_SEARCH_ENGINE`, `SEARXNG_QUERY_URL` etc. are frozen into its DB on first boot unless `ENABLE_PERSISTENT_CONFIG=false`. Set it false so the catalog env stays the source of truth (repo ethos); tradeoff (admin-UI edits to those settings don't stick) documented.
- Real-catalog validators (`validate_generated_catalog.py`, `validate_shell_templates.py`) auto-cover new entries — no fixture edits.

## Branch 1 — `feat/searxng` (svc-download)

1. **`inventory/group_vars/all/apps.yml`** — new `download_apps.searxng` entry: digest-pinned `docker.io/searxng/searxng`, `ui_port: 8888`, volume `/srv/appdata/searxng:/etc/searxng:Z`, `media_mount: false`, `backup_paths: [searxng]`, `dashboard: { group: Downloads, icon: searxng }`, `proxy: true`. Comment explains: jailed for Mullvad egress, port 8888 because 8080 is SABnzbd in the shared netns, secret lives in settings.yml not env (catalog render isn't no_log), consumed by Open WebUI at `http://<svc-download-ip>:8888/search?q=…&format=json`.
2. **`roles/svc_download/templates/searxng-settings.yml.j2`** (new): `use_default_settings: true`; `server:` bind 0.0.0.0, port 8888, `secret_key: "{{ vault_searxng_secret }}"`, `limiter: false` (every request arrives from the single veth IP 10.77.0.2 — the limiter would treat it as one abusive client), `public_instance: false`; `search.formats: [html, json]` (Open WebUI queries `?format=json`, 403'd by default).
3. **`roles/svc_download/tasks/files.yml`** — mirror the Recyclarr pattern: create `/srv/appdata/searxng` (check the image's runtime uid for ownership; pre-chown like recyclarr's dir if needed), render settings.yml `0600`, `no_log: true`, `diff: false`, `register: download_searxng_settings`.
4. **`roles/svc_download/tasks/apps.yml`** — restart `dl-searxng.service` when `download_searxng_settings.changed` (next to "Ensure Recyclarr is started").
5. **Vault:** `vault_searxng_secret` (random hex) + placeholder in `all_vault.yml.example`.

**Verify (functional, per CLAUDE.md):** from svc-download AND from svc-infra: `curl 'http://<svc-download-ip>:8888/search?q=test&format=json'` returns JSON results; `https://searxng.fortwow.dev` loads; egress via Mullvad confirmed (`ip netns exec vpn curl https://am.i.mullvad.net/connected`); leak-canary stays green. Full workflow: `make validate` → iterate `make dl` (+`make media` for the Caddy vhost) → commit → clean tree → deploy `changed=0` → `make verify` → merge/push/delete branch.

**Rollback:** delete the catalog entry + template/tasks, redeploy — the stale-unit reaper removes the quadlet and proxy units.

## Branch 2 — `feat/open-webui` (svc-infra + all docs)

1. **`inventory/group_vars/all/main.yml`:**
   - `gpu_host_ip: "192.168.1.40"` + `gpu_host_online: false` near the host vars, with a comment: the Win11/4090 PC's reserved address (make the DHCP reservation when it arrives), and the online flag gates the Ollama/ComfyUI env so Open WebUI doesn't surface dead-backend errors until then. Flipping `gpu_host_online: true` + `make infra` is the entire go-live step.
   - `caddy_services`: `chat: { backend: "{{ hostvars[infra_host].ansible_host }}:3007", group: Infra, icon: open-webui }`.
   - Do NOT touch `sso_protected_services`.
2. **`inventory/group_vars/all/infra-apps.yml`** — `infra_secret_apps.open-webui` entry: digest-pinned `ghcr.io/open-webui/open-webui`, `ui_port: 3007`, `container_port: 8080`, volume `/opt/homelab/appdata/open-webui:/app/backend/data:Z`, `backup_paths: [open-webui]`, `require_vault: [vault_openwebui_secret_key]`, env:
   - `WEBUI_URL: https://chat.{{ service_domain }}`, `WEBUI_SECRET_KEY: {{ vault_openwebui_secret_key }}` (signs JWTs — stable, rotating logs everyone out), `ENABLE_PERSISTENT_CONFIG: "false"`
   - `ENABLE_OLLAMA_API`/`ENABLE_IMAGE_GENERATION`: templated off `gpu_host_online`; `OLLAMA_BASE_URL: http://{{ gpu_host_ip }}:11434`; `IMAGE_GENERATION_ENGINE: comfyui`; `COMFYUI_BASE_URL: http://{{ gpu_host_ip }}:8188`
   - `ENABLE_WEB_SEARCH: "true"`, `WEB_SEARCH_ENGINE: searxng`, `SEARXNG_QUERY_URL: http://{{ hostvars[download_host].ansible_host }}:8888/search?q=<query>`, `ENABLE_OPENAI_API: "false"`
   - No role-code changes — quadlet/firewalld/backup/restart-cascade all derive from the catalog.
3. **Vault:** `vault_openwebui_secret_key` + `all_vault.yml.example` line.
4. **Docs:** new `docs/gpu-host.md` (Win11 side, manual: Ollama with `OLLAMA_HOST=0.0.0.0`, ComfyUI `--listen 0.0.0.0`, Windows firewall inbound TCP 11434+8188 scoped to 192.168.1.0/24, DHCP reservation for .40, then flip `gpu_host_online`; Continue for VSCode sample `~/.continue/config.yaml` with ollama provider, `apiBase: http://192.168.1.40:11434`, chat/autocomplete/embed model roles). `docs/services.md`: chat + searxng in the service map; note Open WebUI's own login (deliberately not SSO'd), the PersistentConfig tradeoff, and the when-the-PC-arrives checklist. `docs/first-login-walkthrough.md`: Open WebUI first-visit-creates-admin entry.

**Verify now (PC absent):** create the admin account at `https://chat.fortwow.dev`; run a chat with web search toggled on and confirm SearXNG-sourced citations (proves svc-infra → socket proxy → jail → Mullvad end-to-end); confirm no Ollama/image-gen errors surface with `gpu_host_online: false`. Same deploy workflow as Branch 1 (`make infra`, `changed=0` from a clean tree, `make verify`).

**When the PC arrives:** DHCP-reserve 192.168.1.40 → install/configure Ollama + ComfyUI per docs → `gpu_host_online: true` → `make infra` → verify a model reply and a ComfyUI image in chat → set up Continue and test autocomplete.

## Implementation-time checks (flagged, not guessed)

- Pin both image digests from the registries (repo rejects tag refs).
- Confirm the open-webui image runs as container-root (rootless-podman rule; fallback `user: "0"`).
- Confirm SearXNG's runtime uid vs `/etc/searxng` ownership (it writes uwsgi.ini there on first boot on some versions — pre-chown if needed).
- Confirm exact web-search env var names against the pinned Open WebUI release (`RAG_WEB_SEARCH_*` in older releases vs `WEB_SEARCH_*`/`ENABLE_WEB_SEARCH` in current).
