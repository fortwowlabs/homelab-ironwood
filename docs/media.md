# Media acquisition & presentation

Requests go through **Seerr** (`seerr.fort.wow`) — the unified successor to
Overseerr/Jellyseerr — which drives Sonarr/Radarr. It replaced Jellyseerr in v6
(same port 5055, so nothing else changed). Its config is Overseerr/Jellyseerr-
compatible: to carry an existing setup over, follow the migration guide at
<https://docs.seerr.dev/migration-guide/> (copy the old
`/opt/homelab/appdata/jellyseerr` config into `.../seerr` before first start);
otherwise just re-run the short setup wizard.

| Type | Search/download | Presentation | Status |
|------|-----------------|--------------|--------|
| Movies / TV | Radarr / Sonarr + Prowlarr + SABnzbd | Jellyfin | working |
| Books / ebooks / audiobooks | **LazyLibrarian** (v6) + Prowlarr + SABnzbd | Audiobookshelf | **new — wire in UI** |
| Game ROMs | *manual ingest* (no arr pipeline exists) | RomM + metadata | **new — add keys + drop ROMs** |
| Requests | **Seerr** (v6, replaces Jellyseerr) | drives Sonarr/Radarr | **new** |

## LazyLibrarian (books / ebooks / audiobooks)

Runs in the VPN jail exactly like the *arrs (rootful quadlet
`dl-lazylibrarian.container`, `--network=ns:/run/netns/vpn`), exposed on the
LAN via `ll-proxy` and reverse-proxied at `https://lazylibrarian.fort.wow`. It's
in the leak-canary's expected set, the backstop input chain (:5299), and the
nightly appdata backup. Library dirs are created on NFS: `/srv/media/books`,
`/srv/media/ebooks`, `/srv/media/audiobooks`.

**One-time UI wiring** (like the existing Phase 7):
1. **Prowlarr** → Settings ▸ Apps ▸ add **LazyLibrarian** (URL
   `http://10.77.0.2:5299` from inside the jail, or the LAN proxy
   `http://192.168.1.31:5299`; API key from LazyLibrarian ▸ Config ▸ Interface).
   Add book/ebook/audiobook indexers.
2. **LazyLibrarian** → Config ▸ Downloaders ▸ add **SABnzbd**
   (`http://10.77.0.2:8080`, its API key). Config ▸ Processing ▸ set the
   destination dirs to `/data/books`, `/data/ebooks`, `/data/audiobooks`.
3. **Audiobookshelf** → add libraries pointing at `/audiobooks` and
   `/books`/`/ebooks` (ABS's media mounts).

## RomM (game ROMs)

There is **no arr-style indexer/download pipeline** for ROMs. RomM organizes,
enriches, and presents ROM files **you add yourself**; acquisition stays manual.

- **Metadata keys** are now IaC — set the optional `vault_romm_*` /
  `vault_screenscraper_*` etc. keys in the vault (`make vault-edit`); the role
  renders `romm.env`. IGDB (Twitch app) gives the richest data.
- **Ingest ROMs** into `/srv/media/romm/roms/<platform>/…` (e.g.
  `.../roms/snes/`, `.../roms/genesis/`); RomM scans and matches them.

### Adopting the existing RomM DB

`romm.env` + `romm-db.env` are now vault-templated. The DB was previously
initialised with a hand-generated password, so pick one:

- **Match it:** set `vault_romm_db_password` to the value currently in
  `/opt/homelab/appdata/romm/romm-db.env` on svc-media.
- **Reset it (no ROM data yet, so this is clean):** stop RomM, remove
  `/opt/homelab/appdata/romm/mysql/*`, set fresh vault passwords, redeploy —
  MariaDB re-initialises with the vault values.

`MARIADB_*` only take effect on first init of an empty data dir; changing them
later does not alter an already-initialised DB's passwords.
