# Services and application wiring

Normal access is `https://<name>.<service_domain>`. The default private domain
is `fort.wow`; derive the current service VM addresses from
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
| `lazylibrarian` | `svc-download` | Book/audiobook automation inside the VPN jail |
| `cockpit-dl` | `svc-download` | Download VM host administration |

Download UIs reach their jailed containers through generated systemd socket
proxies. Do not publish a download container directly on the host network.

## Private DNS and HTTPS

Caddy and dnsmasq run on `svc-media`. dnsmasq answers the service domain only;
Caddy terminates HTTPS with its internal CA and routes media/infra traffic
locally or to a download proxy. Jellyfin's backend uses its explicit LAN-bound
listener, not an unintended wildcard/loopback publish.

Complete these external steps once:

1. In pfSense/Unbound, create a domain override for `service_domain` pointing
   to `svc-media`'s `ansible_host`.
2. In Tailscale DNS, add that same address as a nameserver restricted to the
   service domain. The dedicated subnet router must advertise the LAN subnet.
3. Run `make access` and install the fetched
   `<service_domain>-root-ca.crt` into each client's trust store.

On macOS use Keychain Access or `security add-trusted-cert`; on iOS install the
profile and explicitly enable full trust; on Linux add it to the distribution
CA anchors; Firefox may require a separate import. Treat this private Caddy CA
as distinct from the Proxmox API CA in [Deployment](deployment.md#trust-the-proxmox-api-certificate).

Verify from a client on both LAN and tailnet:

```bash
dig +short jellyfin.fort.wow
curl --fail --head https://jellyfin.fort.wow
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
5. In Jellyfin, create the administrator and add the NFS movie/TV libraries.
6. In Seerr, sign in with Jellyfin and connect the Sonarr/Radarr instances.
7. In Audiobookshelf, add `/audiobooks`, `/books`, and `/ebooks`, then schedule
   its built-in backup to `/config/backups`.
8. In RomM, verify metadata-provider credentials and ingest owned ROMs under
   `/srv/media/romm/roms/<platform>/`; there is no arr-style ROM acquisition
   pipeline.

The media request flow is:

```text
Seerr -> Sonarr/Radarr -> Prowlarr -> SABnzbd -> NFS media -> Jellyfin
```

The book flow is:

```text
LazyLibrarian -> Prowlarr/SABnzbd -> NFS books/audiobooks -> Audiobookshelf
```

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
