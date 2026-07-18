# v2 expansion — video stack, Caddy, container management

Adds to the base bundle: Jellyfin + Jellyseerr (svc-media), Sonarr + Radarr +
Prowlarr (inside the jail on svc-download), Caddy as the tailnet access
layer, and Cockpit as the container/web management UI.

## Design deltas from v1 — read these first

1. **Prowlarr replaces NZBHydra2.** v1 dropped Prowlarr because nothing
   app-synced from it; Sonarr/Radarr reverse that — Prowlarr now pushes
   indexer config to both automatically. Keep dl-nzbhydra2 only if you later
   add LazyLibrarian (which syncs from Hydra, not Prowlarr). Don't run both
   as active indexer layers; that doubles API hits against your indexer
   quotas for zero coverage gain.
2. **The arrs live INSIDE the jail.** Indexer queries and metadata lookups
   describe your library; they ride wg0 with the downloads. Everything in
   the netns shares one network stack, so wiring is localhost: Prowlarr →
   SAB at 127.0.0.1:8080, arrs at 127.0.0.1:8989/7878. Imports are
   filesystem ops on the NFS mount — no network needed, jailing costs
   nothing.
3. **Storage layout changes for hardlinks.** v1 had completed downloads on
   svc-download's local disk; the arrs make that a copy-across-filesystems
   penalty on every import. New layout — ONE TrueNAS dataset exported once,
   mounted at /srv/media on svc-download, containers see it as /data:
   ```
   sata_wd_14tb/media                    -> /srv/media  (single dataset = hardlinks work)
     downloads/complete/{tv,movies}
     tv/   movies/   audiobooks/   ebooks/   romm/
   ```
   SAB: incomplete stays LOCAL (fast ext4 churn), completed goes to
   /data/downloads/complete/<category>. Sonarr/Radarr import = hardlink +
   atomic move within the dataset. Gotchas: (a) crossing ZFS datasets breaks
   hardlinks silently (EXDEV → copy) — keep it ONE dataset; (b) SAB now
   unpacks over NFS/1GbE — acceptable for usenet speeds, but if it annoys
   you, move unpack back local and eat the copy; (c) update dl-sabnzbd's
   Volume lines to match (`/srv/media:/data` replaces the complete/watch
   mounts).
4. **Jellyfin has two front doors.** LAN clients (Chromecast with the
   Jellyfin Android TV app — the device finally has a purpose) hit
   http://192.168.1.30:8096 direct; tailnet clients use Caddy :8444.
   Firewalld: open 8096 to the LAN zone only.
5. **pfSense delta**: LAN interface rule — pass svc-media (192.168.1.30) →
   192.168.1.31 TCP {8989, 7878} so Jellyseerr can drive the arrs. Add
   svc-media to ADMIN_HOSTS or make it its own alias.

## Deploy order

```bash
# svc-download (root):
install -m 644 quadlets/dl-{prowlarr,sonarr,radarr}.container /etc/containers/systemd/
install -m 644 proxies/{prowlarr,sonarr,radarr}-proxy.* /etc/systemd/system/
install -m 750 leak-canary.sh /usr/local/sbin/        # updated container list
/usr/libexec/podman/quadlet -dryrun
systemctl daemon-reload
systemctl start dl-prowlarr dl-sonarr dl-radarr
systemctl enable --now {prowlarr,sonarr,radarr}-proxy.socket
/usr/local/sbin/leak-canary.sh && echo PASS           # MUST pass with new containers

# svc-media (as homelab user units + root for caddy/cockpit):
install -m 644 quadlets/{jellyfin,jellyseerr}.container ~homelab/.config/containers/systemd/
systemctl --user -M homelab@ daemon-reload
systemctl --user -M homelab@ start jellyfin jellyseerr
# Caddy: see caddy/README-caddy.md
firewall-cmd --zone=public --add-port=8096/tcp --permanent && firewall-cmd --reload
```

Wiring after first boot: Prowlarr → Settings → Apps → add Sonarr
(http://127.0.0.1:8989) and Radarr (127.0.0.1:7878); both get SAB as
download client at 127.0.0.1:8080, category tv/movies; Jellyseerr → point at
Jellyfin (127.0.0.1:8096 is wrong from its netns — use 192.168.1.30:8096 or
the container DNS name if you later put them on a shared podman network) and
at the arrs via 192.168.1.31:{8989,7878}.

## Container management UI: Cockpit, not Portainer

The "Portainer sort of" answer on an EL/Podman/Quadlet system is
**Cockpit + cockpit-podman**:

```bash
dnf install -y cockpit cockpit-podman   # both VMs
systemctl enable --now cockpit.socket   # https://<host>:9090
# svc-media: reach it over the tailnet; add a Caddy vhost or open 9090 in the
#            tailscale0 firewalld zone.
# svc-download: reachable from ADMIN_HOSTS via the LAN->VLAN40 rule.
```

Why: it's in AppStream (no third-party infra), understands rootless AND
rootful Podman side by side, shows logs/exec/stats per container, and rides
along with the rest of Cockpit (journal, metrics, storage, terminal) that
you already know from the RHEL day job. Critically, it *observes* the
containers Quadlet manages without fighting systemd for ownership.

Portainer CE does run against podman.socket, but it's Docker-first: it
neither understands Quadlet units nor systemd lifecycle, so anything you
create through it becomes unmanaged drift outside your unit files — the
worst of both worlds. If you want Portainer's stack-deploy UX, that's a sign
to move that workload's definition into a Quadlet instead. Verdict: Cockpit
for eyes-on-glass, Quadlet files as the single source of truth, Portainer
not installed.

## Recommended additions (curated, in priority order)

1. **ntfy (svc-media, tailnet-only via Caddy)** — self-hosted push
   notifications. This also closes the v1 loose end: point leak-canary's
   `fail()` at `curl -d "leak" http://192.168.1.30:2586/homelab` (LAN-internal,
   works even though svc-download has no WAN egress). Wire SAB, the arrs, and
   Uptime Kuma into it too. Highest value-per-watt addition on this list.
2. **Uptime Kuma (svc-media)** — checks: each UI endpoint, the NFS mount
   (push monitor from a cron), TrueNAS, and an HTTP check against ifconfig.me
   *from the jail* via a push monitor fed by leak-canary — turning the canary
   log into a dashboard with history.
3. **Recyclarr (svc-download, jail)** — syncs TRaSH-guide quality
   profiles/custom formats into Sonarr/Radarr. The difference between the
   arrs grabbing sane releases and grabbing 80GB remuxes of cam rips. Run as
   a Quadlet with a systemd timer, not a daemon.
4. **Bazarr (svc-download, jail)** — subtitle automation against the same
   /data layout. Add only if subtitles actually matter in your household.
5. **Backup hardening** — TrueNAS: zfs snapshot schedule on sata_wd_14tb/media +
   sata_wd_14tb/backups (hourly/daily/weekly tiers), and an offsite leg:
   restic or TrueNAS cloud-sync of *appdata backups + ebooks/audiobooks*
   (irreplaceable) to B2 — media video is re-acquirable, don't pay to
   offsite it. This is the biggest actual-risk reducer on the list.
6. **Scrutiny or plain smartd alerts on the Proxmox host** — TrueNAS covers
   its passed-through disks; the hypervisor's boot/VM NVMe is currently
   unmonitored. Given you already wrote drive-survey.sh for work, smartd →
   ntfy may be all you want.
7. **Dashboard (Homepage/Homarr)** — one tailnet page with all services +
   API widgets (SAB queue, arr calendars, Jellyfin sessions). Cosmetic but
   the household-facing win is real: one bookmark for her instead of five
   ports.
8. **GPU passthrough for Jellyfin, later** — the 3700X has no iGPU, so
   transcodes are CPU-bound. If remote streaming to phones (which always
   transcodes) becomes routine: Intel Arc A310 (~$100) passed through to
   svc-media gives QSV AV1/HEVC encode at ~15W. Until then, direct-play
   clients and pre-optimized bitrates cover a two-user household.
9. **Skip for now**: Prometheus/Grafana (Cockpit + Kuma covers two VMs),
   Authelia/authentik SSO (tailnet identity already gates everything),
   fail2ban (nothing is WAN-exposed — keep it that way: zero port forwards
   remains the design's best security property).

## Acceptance additions

```bash
# arrs are jailed:
for c in prowlarr sonarr radarr; do podman inspect -f '{{.NetworkSettings.SandboxKey}}' $c; done
                                             # all -> /run/netns/vpn
podman exec sonarr curl -4 -s https://ifconfig.me   # provider IP
# hardlinks actually happening (inode match after an import):
stat -c '%i %n' /srv/media/downloads/complete/tv/<file> /srv/media/tv/<show>/<file>
# Jellyfin LAN direct-play: playback on the Chromecast shows "Direct playing"
# leak canary still green with 5 containers:
/usr/local/sbin/leak-canary.sh && echo PASS
```
