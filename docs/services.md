# Services and application wiring

Normal access is `https://<name>.<service_domain>`. The default split-horizon
domain is `fortwow.dev`; derive the current service VM addresses from
`inventory/hosts.yml` rather than copying IPs into documentation or role data.

## Service map

| Name | Host | Purpose |
|---|---|---|
| `jellyfin` | `svc-media` | Movies and television playback; also LAN-bound for local players |
| `abs` | `svc-media` | Audiobookshelf playback and library management |
| `romm` | `svc-media` | ROM presentation and metadata, backed by MariaDB |
| `seerr` | `svc-media` | Requests for Jellyfin media |
| `home` | `svc-media` | Homepage service launcher |
| `ntfy` | `svc-media` | Deployment, verification, leak, disk, and backup alerts |
| `cockpit-media` | `svc-media` | Media VM host administration |
| `sabnzbd` | `svc-download` | Usenet downloader inside the VPN jail |
| `prowlarr` | `svc-download` | Indexer management inside the VPN jail |
| `sonarr` | `svc-download` | Television automation inside the VPN jail |
| `radarr` | `svc-download` | Movie automation inside the VPN jail |
| `bazarr` | `svc-download` | Subtitle automation for Sonarr/Radarr inside the VPN jail |
| `lazylibrarian` | `svc-download` | Book/audiobook automation inside the VPN jail |
| `jdownloader` | `svc-download` | General-purpose downloader inside the VPN jail |
| `shelfmark` | `svc-download` | Interactive book/audiobook search and requests inside the VPN jail |
| `searxng` | `svc-download` | Metasearch inside the VPN jail; Open WebUI's search provider |
| `cockpit-dl` | `svc-download` | Download VM host administration |
| `auth` | `svc-infra` | Authelia SSO portal; fronts thirteen services (see below) |
| `chat` | `svc-infra` | Open WebUI; chat and image generation against the GPU host |
| `scan` | `svc-infra` | Nightly security scan report (errata, image CVEs, benchmark, exposure) |

Download UIs reach their jailed containers through generated systemd socket
proxies. Do not publish a download container directly on the host network.

### Single sign-on

`auth` on `svc-infra` is the Authelia login portal. Thirteen services sit
behind it via Caddy `forward_auth`: the eight download apps above (including
`searxng`, whose browser-facing vhost this gates — Open WebUI's own use of it
dials svc-download's `IP:port` directly and is unaffected), plus
`code-server`, `webtop`, `syncthing`, `prometheus` and `scan`.

The protected set is `sso_protected_services` in
`inventory/group_vars/all/main.yml` — one list, consumed by both vhost loops in
`Caddyfile.j2`. Adding a name protects a service; emptying the list and running
`make access` is the rollback. `tests/validate_sso.py` rejects a name with no
Caddy vhost, because that would leave the service unprotected while looking
configured.

Who may reach them is a separate question from which are protected. The
accounts are `authelia_users` in `roles/svc_infra/defaults/main.yml` — names
and groups in the clear, hashes in the vault — and `access_control` in
`authelia-configuration.yml.j2` denies by default, granting only
`group:admins`. An account in any other group authenticates and then gets a
403. Opening a service to a group is a rule with a `domain:` list; the same
test fails the build if a rule names a group nobody is in, or if no rule
grants `admins` (which would lock everyone out of everything at once).

Two things worth knowing before debugging anything here:

- **Auth is applied at the Caddy vhost only.** Every protected service is still
  reachable unauthenticated at its direct `IP:port`. That is the intended
  escape hatch, and it is also why Syncthing's own GUI password still matters
  on the LAN.
- **`make verify` now depends on Authelia.** If the portal is down, Caddy's
  `forward_auth` returns 502 and the smoke test fails for all twelve at once.
  That is correct — they really are broken — but the cause is one container.

Services deliberately left open are listed with their reasons alongside the
`sso_protected_services` definition; the short version is that native and
mobile clients (Jellyfin, Immich, Nextcloud, Audiobookshelf, ntfy, Vaultwarden)
cannot follow a browser redirect to a login form.

## The AI stack

Three pieces on three different machines:

```text
chat.fortwow.dev (Open WebUI, svc-infra)
  |-- inference + images --> Ollama / ComfyUI on the Win11 4090 box (LAN, direct)
  |-- search QUERIES -----> SearXNG in svc-download's VPN jail --> Mullvad
  `-- page FETCHES -------> forward proxy in the same jail ------> Mullvad
```

**Open WebUI keeps its own login and is deliberately not behind Authelia.** It
has its own account system with a first-visit admin setup, so fronting it with
the portal would mean signing in twice for one service. The first person to
visit `https://chat.fortwow.dev` creates the administrator account — do that
promptly.

**SearXNG is jailed for its egress, not for its own protection.** Putting it in
the VPN netns means every upstream engine query leaves via Mullvad by
construction, because the only route out of that namespace is wg0. Open WebUI
reaches it at `<svc-download-ip>:8888` through the generated socket proxy, the
same mechanism every other jailed UI uses. Two consequences worth knowing:

- Its listener is configured by `SEARXNG_PORT`/`GRANIAN_HOST` env in the
  catalog, **not** by `settings.yml`. This image serves through granian, and
  `server.port` / `server.bind_address` in the settings file are inert.
- Port 8888 rather than the upstream default 8080, because the jail is one
  shared network namespace and SABnzbd already holds 8080 in it.

**A web search is two round trips, and only the first was ever jailed.**
SearXNG queries the upstream engines and returns a list of URLs; Open WebUI
then fetches those pages itself. Until 2026-08-13 that second request left
svc-infra directly, so the engines saw Mullvad and every site in the results
saw the house. It was found by asking a model to fetch an echo service and
reading back a home address.

Chat's outbound HTTP now goes to a forward proxy inside the same jail, and
svc-infra drops any non-LAN packet from Open WebUI's cgroup — so the proxy is
not a setting that can be ignored. `chat_proxy_log_requests` (default `true`)
controls whether the proxy records destinations; it governs the drop rule's
log statement too, and never the drop counter the hourly probe depends on.

Two consequences worth knowing:

- **Chat's web features fail closed.** If the tunnel or the jail is down,
  fetching breaks rather than falling back. Local inference, image generation
  and history are unaffected. `homelab-chat-egress.timer` alerts on it.
- **This covers chat and nothing else.** Every other service on svc-infra and
  svc-media still egresses directly; `service_guarded_egress` is `true` on
  svc-download alone. The full audit is in the design doc's appendix.

**Settings are seeded from the catalog, then owned by the database.**
`ENABLE_PERSISTENT_CONFIG` is `true`, so a value in
`inventory/group_vars/all/infra-apps.yml` applies only until that key is
touched in the admin UI — after which a row exists, the row wins, and editing
the catalog silently does nothing while `make infra` still reports success.
Admin-UI changes DO survive restarts. The sharp edge is `ENABLE_SIGNUP`, where
the drift is a security change rather than a preference: confirm
`ui.enable_signup` is false in `GET /api/v1/configs/export` after any
admin-settings session.

The GPU host itself is a hand-managed Windows workstation, not infrastructure.
Its setup, the firewall scoping, the Continue for VSCode config, and the
go-live step are all in [The GPU host](gpu-host.md). Until
`gpu_host_online: true`, Open WebUI deploys with its Ollama and
image-generation backends switched off on purpose — web search still works,
because it does not depend on the PC.

## Private DNS and HTTPS

Caddy and dnsmasq run on `svc-media`. dnsmasq answers the service domain only;
Caddy terminates HTTPS with a publicly trusted Let's Encrypt wildcard and
routes media/infra traffic locally or to a download proxy. Jellyfin's backend
uses its explicit LAN-bound listener, not an unintended wildcard/loopback
publish. The public Cloudflare zone contains no service records; Certbot uses
its DNS API only for temporary ACME challenge TXT records.

Complete these external steps once:

1. In pfSense/Unbound, create a domain override for `service_domain` pointing
   to `svc-media`'s `ansible_host`.
2. In Tailscale DNS, add that same address as a nameserver restricted to the
   service domain. The dedicated subnet router must advertise the LAN subnet.
3. Keep Cloudflare DNSSEC/registrar DS records disabled unless pfSense has a
   narrowly scoped `domain-insecure: "fortwow.dev"` exception.

No client CA installation is required. Certificate issuance, renewal, DNSSEC,
and router steps are documented in [DNS and HTTPS](dns-pfsense-caddy.md).

Verify from a client on both LAN and tailnet:

```bash
dig +short jellyfin.fortwow.dev
curl --fail --head https://jellyfin.fortwow.dev
```

The address must resolve to `svc-media`, and every configured Caddy backend
must return its expected HTTP response during rollout.

## Sources of truth

### Download applications

`download_apps` in `inventory/group_vars/all/apps.yml` is keyed by application
and records its immutable image digest, UI port, volumes, media-mount
requirement, backup paths, and dashboard metadata. The role derives all
repeated behavior from it: Quadlets, image pulls, socket proxies, firewall
ports, start/restart handling, backup archives, canary membership, UI probes,
and disruptive recovery.

To add or change a download app:

1. Add or update exactly one catalog entry, including a reviewed digest; never
   use `:latest`.
2. Add only genuinely app-specific files or validation that cannot be derived
   from the catalog.
3. Run `make validate`; catalog assertions must show one generated artifact for
   every eligible behavior.
4. During a maintenance window run `make check`, `make dl`, `make verify`, and
   `make verify-disruptive`.
5. Confirm the proxy, NFS visibility when requested, Mullvad identity, backup
   artifact, Homepage link, and leak-canary recovery.

Removing an entry is a migration: stop and retire its legacy unit only when the
unit or file actually exists. Preserve appdata until backup and rollback
requirements expire.

### Media and infrastructure endpoints

`caddy_services` in `inventory/group_vars/all/main.yml` remains the source for
media and infrastructure endpoints, including backend address, scheme, TLS
handling, group, and icon. The access layer combines these entries with
download catalog metadata to render DNS, Caddy, and Homepage configuration.
Add an endpoint there; do not hand-edit the rendered Caddyfile, dnsmasq zone,
or dashboard.

## One-time UI wiring

Ansible intentionally does not automate application wizards or store provider
credentials in task output.

1. In SABnzbd, configure the Usenet provider and categories.
2. In Prowlarr, add indexers and connect Sonarr, Radarr, and LazyLibrarian.
3. In Sonarr and Radarr, configure SABnzbd and root folders under the container's
   `/data` media mount.
4. In LazyLibrarian, configure SABnzbd and destinations
   `/data/books`, `/data/ebooks`, and `/data/audiobooks`.
5. In Shelfmark, leave authentication disabled as intended, use Universal
   search, configure Prowlarr at `http://127.0.0.1:9696` and SABnzbd at
   `http://127.0.0.1:8080`, and add their API keys. Keep SABnzbd's `books` and
   `audiobooks` completed paths beneath `/data`; Shelfmark sees that same path.
   Ebooks deliver to `/books` (CWA ingest), audiobooks to
   `/data/audiobooks`, and the library link is
   `https://calibre-web.{{ service_domain }}`. Configure only direct sources
   you are authorized to use; their credentials stay in Shelfmark appdata.
6. In Jellyfin, create the administrator and add the NFS movie/TV libraries.
7. In Seerr, sign in with Jellyfin and connect the Sonarr/Radarr instances.
8. In Audiobookshelf, add `/audiobooks`, `/books`, and `/ebooks`, then schedule
   its built-in backup to `/config/backups`.
9. In RomM, verify metadata-provider credentials and ingest owned ROMs under
   `/srv/media/romm/roms/<platform>/`; there is no arr-style ROM acquisition
   pipeline.

The media request flow is:

```text
Seerr -> Sonarr/Radarr -> Prowlarr -> SABnzbd -> NFS media -> Jellyfin
```

The book flow is:

```text
LazyLibrarian -> Prowlarr/SABnzbd -> NFS books/audiobooks -> Audiobookshelf

Prowlarr/SABnzbd -> Shelfmark -> CWA ingest -> ebook library
                           `-> NFS audiobooks -> Audiobookshelf

Direct source -------> Shelfmark -> the same destinations
```

Shelfmark complements LazyLibrarian rather than replacing its background
author, series, and new-release monitoring.

## Beszel monitoring setup

Beszel's hub (svc-infra, `beszel.{{ service_domain }}`) deploys fully
automated — but the agent on each of the three service VMs needs one manual
step first, because the credentials it needs only exist after the hub itself
has been visited once (there's no API/CLI to generate them ahead of time):

1. Deploy normally (`make deploy` or `make infra`). The hub comes up; every
   agent skips itself with a loud WARNING in the play output — this is
   expected on a fresh install, not a failure.
2. Visit `https://beszel.{{ service_domain }}` and create the admin account.
3. Under **Settings > Tokens**, create (or copy) the **universal token** —
   this single token authenticates every agent, no per-host token needed.
4. The **key** is the hub's public key, also shown on that same tokens page
   (or when manually adding a system) — copy it in full, including the
   `ssh-ed25519 ...` prefix.
5. `make vault-edit`, set `vault_beszel_token` and `vault_beszel_key` to
   those two values, save.
6. Re-run `make deploy` (or `make dl`/`make media`/`make infra`
   individually). Every agent now renders, starts, and reports in — check
   the hub UI for three connected systems.

`vault_beszel_token`/`vault_beszel_key` are shared by all three agents; there
is nothing host-specific to configure per VM.

## Grafana dashboards

Two provisioned dashboards, both owned by this repo (`allowUiUpdates: false`,
so UI edits revert on restart — copy a dashboard to a new name to experiment):

- **Homelab nodes** — host CPU, memory, disk and network from node_exporter.
- **Homelab estate** — CVE counts, images behind upstream, container drift, and
  a freshness row that reports how old each of those numbers is. Fed by the
  textfile collector rather than a scrape; see "Trending is separate from
  alerting, and both are needed" in `CLAUDE.md`. Its default range is 30 days
  because the series update nightly and weekly — a 6h window shows nothing and
  looks broken.

Freshness is judged per emitter, not against one global threshold: scan and
drift read as stale past 26h, release past 8 days, because a single threshold
would show the weekly release series as permanently red. The "Critical CVEs"
tile alarms at 1; "High CVEs" deliberately does not, because high-severity
findings are routine across the pinned image catalog and a tile that is always
red stops getting read.

## Seerr and RomM migration notes

Seerr replaces Jellyseerr on the same application port. The role retires legacy
units only when they exist. Before first Seerr start, preserve the old
Jellyseerr appdata and follow Seerr's compatible-config migration procedure;
after acceptance, confirm the nightly artifact is named for `seerr`, not
`jellyseerr`.

RomM database bootstrap variables affect only an empty MariaDB data directory.
For an existing database, rotate or reconcile the database accounts inside
MariaDB before changing their vault values. Never reset the database directory
as part of a normal converge. Restore instructions are in
[Operations and restore](operations.md#romm).
