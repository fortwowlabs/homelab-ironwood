# First-login walkthrough

Every service, in the order to do them. The order is not arbitrary — several
services depend on another being set up first, and doing them out of order means
redoing work.

Before starting:

- DNS: all 37 service names resolve to `192.168.1.30` via dnsmasq, the pfSense
  override, and the normal client path.
- TLS: a request without `-k` succeeds with the Let's Encrypt wildcard.
  Standard clients need no custom CA installation.

Setup guide for the router side, if you ever rebuild it:
[dns-pfsense-caddy.md](dns-pfsense-caddy.md).

**Legend**

- 🔑 **Vault** — the password already exists. Retrieve it, log in, done.
- 🆕 **Create** — you choose the credentials on first visit.
- ⚙️ **Configure** — needs more than a login.
- ✅ **Nothing to do** — no auth, or already configured (verified).

---

## Phase 0 — Prerequisites

Complete DNS and confirm the public certificate before touching any service.
See [DNS and HTTPS](dns-pfsense-caddy.md).

### 0.1 Open the vault

```bash
make vault-edit
```

Keep it open in another window — Phases 2 and 4 read from it. Values are never
printed anywhere else, and the file is gitignored.

---

## Phase 1 — Vaultwarden first 🆕

**Do this before anything else.** It is your password manager, so setting it up
first means every credential from here on gets saved as you go, instead of being
collected and entered later.

1. Go to **https://vaultwarden.fortwow.dev/admin**
2. Log in with the plaintext admin token — vault key
   `vault_vaultwarden_admin_token_plaintext`.
   (The vault also holds `vault_vaultwarden_admin_token`, which is the Argon2
   *hash* of it. That one is what the container reads; you cannot log in with it.)
3. In the admin panel, **invite your own email address**.
4. No SMTP is configured, so no mail is sent and no link appears. Instead go to
   **https://vaultwarden.fortwow.dev/#/signup** and register using that exact
   invited address.

Self-registration is switched off permanently and does not need toggling —
admin invites bypass it by design. If `/admin` gives 404 instead of a login
page, the admin token did not reach the container.

> From here on: **save each credential into Vaultwarden as you create it.**

---

## Phase 2 — Log in with existing vault passwords 🔑

These already have accounts. Retrieve, log in, save to Vaultwarden. Any order.

| Service | URL | Username | Vault key |
|---|---|---|---|
| Nextcloud | nextcloud.fortwow.dev | `admin` | `vault_nextcloud_admin_password` |
| Grafana | grafana.fortwow.dev | `admin` | `vault_grafana_admin_password` |
| Semaphore | semaphore.fortwow.dev | `admin` | `vault_semaphore_admin_password` |
| NetBox | netbox.fortwow.dev | `admin` | `vault_netbox_superuser_password` |
| Webtop | webtop.fortwow.dev | `admin` | `vault_webtop_password` (HTTP basic auth) |
| code-server | code-server.fortwow.dev | — | `vault_codeserver_password` (password only) |
| **Paperless-ngx** | paperless.fortwow.dev | `admin` | `vault_paperless_admin_password` |
| **Mealie** | mealie.fortwow.dev | `admin@fort.wow` | `vault_mealie_admin_password` |
| Beszel | beszel.fortwow.dev | `admin@fort.wow` | already created — see `LLM-TODO-LIST.md`; change the password on first login |

Change the Beszel password once you are in; it was set during an earlier session
and is recorded in plaintext in `LLM-TODO-LIST.md`.

---

## Phase 3 — Media stack 🆕 (order matters here)

**Jellyfin must come first.** Seerr authenticates *against* Jellyfin, so setting
up Seerr first means redoing it.

Verified 2026-07-25: Jellyfin's startup wizard has **not** been completed yet.

### 3.1 Jellyfin — https://jellyfin.fortwow.dev
Runs the first-run wizard: create your admin user, then add libraries. The NFS
media paths inside the container are:

- Movies → `/srv/media/movies`
- TV → `/srv/media/tv`
- Music → `/srv/media/music`

(Confirm against what actually exists under `/srv/media` — the wizard will show
you.) Set the metadata language, then finish. Save the credentials.

### 3.2 Seerr — https://seerr.fortwow.dev
Choose **Sign in with Jellyfin**, point it at `http://192.168.1.30:8096`, and
use the Jellyfin admin account you just made. Then connect Sonarr and Radarr
when prompted — see Phase 4 for their addresses.

### 3.3 Audiobookshelf — https://abs.fortwow.dev
Creates the root user on first visit. Library path `/srv/media/audiobooks`.

### 3.4 RomM — https://romm.fortwow.dev
First-run admin account. Its database credentials are already in the vault
(`vault_romm_*`) and are not the same as the web login.

### 3.5 Calibre-Web — https://calibre-web.fortwow.dev ⚙️
Ships with a **default account `admin` / `admin123`**. Log in and change it
immediately — this is the only service with a publicly-known default.

---

## Phase 4 — Download stack ✅ / ⚙️

**Verified 2026-07-25:** Sonarr, Radarr and Prowlarr are configured with
`AuthenticationMethod=Forms` and `AuthenticationRequired=DisabledForLocalAddresses`.
Because Caddy proxies from a LAN address, **you will not be prompted to log in**
from inside the network. Nothing to do for those three.

| Service | URL | State |
|---|---|---|
| Sonarr | sonarr.fortwow.dev | ✅ no login needed on LAN |
| Radarr | radarr.fortwow.dev | ✅ no login needed on LAN |
| Prowlarr | prowlarr.fortwow.dev | ✅ no login needed on LAN |
| SABnzbd | sabnzbd.fortwow.dev | ✅ reachable; Usenet provider already configured (`news.eweka.nl`) |
| Bazarr | bazarr.fortwow.dev | 🆕 no config yet — first-run wizard |
| LazyLibrarian | lazylibrarian.fortwow.dev | 🆕 no config yet — first-run wizard |
| Shelfmark | shelfmark.fortwow.dev | 🆕 intentionally open on LAN/tailnet; configure sources and clients |
| jDownloader | jdownloader.fortwow.dev | ⚙️ noVNC desktop; sign in to MyJDownloader if you use it |

Internal addresses for wiring these together — they all share the VPN jail's
network namespace, so they reach each other on **localhost**:

- Sonarr `localhost:8989`, Radarr `localhost:7878`, Prowlarr `localhost:9696`,
  SABnzbd `localhost:8080`, Shelfmark `localhost:8084`

**Order within this phase:**

1. **SABnzbd** — nothing to do. It used to return 403 "Hostname verification
   failed" through Caddy; the deploy now keeps its host allowlist correct, and
   self-heals if the container is recreated (which changes the hostname it
   would otherwise trust).
2. **Prowlarr** — add indexers, then use *Settings > Apps* to push them to
   Sonarr/Radarr automatically.
3. **Bazarr** — connect to Sonarr and Radarr (localhost addresses above).
   Its API keys are read automatically by Recyclarr, but Bazarr's own setup is
   manual.
4. **Shelfmark** — leave authentication disabled as intended. Keep Universal
   search, connect Prowlarr at `http://127.0.0.1:9696` and SABnzbd at
   `http://127.0.0.1:8080`, and enter their API keys. Use SABnzbd categories
   `books` and `audiobooks` with completed paths below `/data`; ebooks deliver
   to `/books`, audiobooks to `/data/audiobooks`, and the library link is
   `https://calibre-web.fortwow.dev`. Configure only direct sources you are
   authorized to use.

Recyclarr already syncs TRaSH-Guides quality profiles into Sonarr/Radarr
automatically — no UI, nothing to configure.

---

## Phase 5 — Infra apps 🆕

Any order; none depend on each other.

| Service | URL | What to do |
|---|---|---|
| Immich | immich.fortwow.dev | Create admin on first visit. Library is on NFS at `/srv/media/photos` |
| Uptime Kuma | uptime-kuma.fortwow.dev | Create admin, then add monitors for the services you care about |
| Bambuddy | bambuddy.fortwow.dev | Create admin; add your Bambu printer |
| Syncthing | syncthing.fortwow.dev | **Set a GUI password immediately** (Actions > Settings > GUI) — it is unauthenticated until you do |

### Home Assistant 🆕

Previously this returned a bare HTTP 400 through Caddy until `trusted_proxies`
was hand-edited in. The deploy now writes that block itself (as a marked,
idempotent section it will not clobber your automations), so
**https://home-assistant.fortwow.dev works directly** — just create your account.

If you ever add your own top-level `http:` key to `configuration.yaml` by hand,
the role stands down rather than creating a duplicate key that would stop HA
booting, and prints a note telling you to add `trusted_proxies` yourself.

---

## Phase 6 — No login required ✅

Nothing to do; listed so you know they exist.

| Service | URL | Notes |
|---|---|---|
| Homepage | home.fortwow.dev | Dashboard tiles for everything |
| IT Tools | it-tools.fortwow.dev | Offline dev utilities |
| Glances | glances.fortwow.dev | Host metrics; REST API at `/api/4/...`, MCP at `/mcp/sse` |
| Prometheus | prometheus.fortwow.dev | Scrape targets and raw queries |
| ntfy | ntfy.fortwow.dev | Push notifications; deploy alerts already publish here |
| Cockpit ×3 | cockpit-media / cockpit-dl / cockpit-infra | Log in with your **system** account (`straderb`), not a vault password |

---

## Phase 7 — Minecraft 🎮

Not web services — connect from the Minecraft client:

- `minecraft.fortwow.dev:25565`
- `minecraft2.fortwow.dev:25566`

Both are Paper, latest, 2 GB heap each, LAN-only. Admin commands run on the host
rather than in a UI:

```bash
ssh straderb@192.168.1.30
sudo -u homelab XDG_RUNTIME_DIR=/run/user/10001 \
    podman exec minecraft rcon-cli whitelist add <player>
```

Worlds are flushed with `save-off`/`save-all` before each nightly backup tar, so
backups are consistent.

---

## Phase 8 — Worth doing once everything is up

- **Grafana dashboards** — a *Homelab nodes* dashboard is now provisioned
  automatically (CPU, memory, filesystem, network, load and uptime for all three
  VMs, with a Host selector). Nothing to import. Add more by dropping JSON into
  `roles/svc_infra/files/grafana-dashboards/`.
- **Uptime Kuma monitors** — add HTTP checks for the services you care about.
- **Semaphore** — deployed empty. To run *this* repo's playbooks from its UI, add
  a project, this git repo, an SSH key, and an inventory.
- **Immich machine learning** — currently off. svc-infra now has 16 GB, so there
  is headroom to enable smart search / face detection.

---

## Quick reference: what needs what

```
Trust CA  ──▶  everything HTTPS
Vault     ──▶  Phase 2 logins
Vaultwarden ─▶  (store everything you create from here on)
Jellyfin  ──▶  Seerr
SABnzbd   ──▶  Sonarr/Radarr downloads
Prowlarr  ──▶  Sonarr/Radarr indexers
Sonarr/Radarr ▶ Bazarr subtitles
HA direct :8123 ▶ trusted_proxies ▶ HA via Caddy
```

---

## Could any of this be automated?

Yes — several of these manual steps are avoidable. See
[automation-opportunities.md](automation-opportunities.md).
