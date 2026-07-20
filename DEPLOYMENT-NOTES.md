# First deployment — session notes & reference

Reference for the first live deployment of this repo (2026-07-17 → 2026-07-19),
run entirely from the MBP against the PVE host `thurgadin`. The deploy is
**complete and verified green** three ways: full `site.yml` (`failed=0` on all
hosts), `make verify` including the fail-closed kill test, and a clean
idempotence re-run. Commits `14fced3 → 59943d1` on `fortwowlabs/homelab-ironwood`.

---

## 1. Using the services

Two access paths by design: media/request apps over Tailscale from anywhere;
admin/download tools on the LAN (kept behind the Mullvad jail).

### Media & request apps — over Tailscale (`svc-media.kitty-daggertooth.ts.net`, real HTTPS)

| App | URL | For |
|-----|-----|-----|
| Jellyseerr | `https://svc-media.kitty-daggertooth.ts.net:8447` | **Hand this to other people** — browse & request |
| Jellyfin | `https://svc-media.kitty-daggertooth.ts.net:8444` | Watch the library |
| Audiobookshelf | `https://svc-media.kitty-daggertooth.ts.net:8443` | Audiobooks & podcasts |
| RomM | `https://svc-media.kitty-daggertooth.ts.net:8445` | Retro games |

Jellyfin is also on the LAN directly: `http://192.168.1.30:8096` (no tunnel — best for a local TV).

### Admin / download tools — LAN only (`svc-download`, `192.168.1.31`)

Tunneled out of the VPN jail via systemd socket proxies so the apps stay behind Mullvad.

| App | URL | Role |
|-----|-----|-----|
| SABnzbd | `http://192.168.1.31:8080` | Usenet downloader |
| Prowlarr | `http://192.168.1.31:9696` | Indexer manager |
| Sonarr | `http://192.168.1.31:8989` | TV automation |
| Radarr | `http://192.168.1.31:7878` | Movie automation |

Remote access to these works through the Tailscale subnet router on thurgadin's network.

### Intended end-to-end flow (after wiring — see §2)

Request in Jellyseerr → Radarr/Sonarr search indexers via Prowlarr → SABnzbd
downloads inside the Mullvad jail → Radarr/Sonarr import to the NFS share →
appears in Jellyfin automatically.

---

## 2. Remaining manual steps (NOT done yet)

The containers all run, but these one-time steps are still needed. Per the
README these are deliberately manual (Phase 7 app wiring, Phase 9 ops).

- **App wiring** (each app currently opens to a first-run wizard):
  - SABnzbd: add Usenet provider (server + credentials).
  - Prowlarr: add indexers; add Sonarr/Radarr as "apps" so it syncs them.
  - Sonarr/Radarr: set download client to SABnzbd (via the in-jail proxy),
    set root folders on the media share, link indexers through Prowlarr.
  - Jellyfin: create admin account; add libraries on the NFS media folders.
  - Jellyseerr: sign in with the Jellyfin account; connect to Sonarr/Radarr.
  - RomM: metadata-provider API creds (IGDB/ScreenScraper/etc.) — the
    `romm.env` fields were left blank at deploy time.
- **ntfy server** on svc-media (Phase 9). Until it exists, every play prints
  a harmless WARNING (the play result is still authoritative). The backstop
  already permits the svc-media ntfy port when `ntfy_url` points at svc-media.
- **Nightly verify timer** from `contrib/` — install on an always-on box that
  has the repo + `.vault_pass` (NOT the PVE host).
- **Pin container images by digest** after burn-in (README "Updates & image policy").

---

## 3. Operating the stack

Run from the repo root on the MBP. Ansible lives in `.venv` (the Makefile
auto-detects it); the vault password is in `.vault_pass` (gitignored).

```bash
make deploy   USE_VAULT_FILE=1    # full provision + configure + verify
make verify   USE_VAULT_FILE=1    # gate assertions incl. fail-closed kill test
make check    USE_VAULT_FILE=1    # dry-run / drift detection
make dl       USE_VAULT_FILE=1    # svc-download only
make media    USE_VAULT_FILE=1    # svc-media only
make vault-edit                   # edit encrypted secrets
```

A clean re-run shows `changed=2` per service VM — all four are deliberate
`changed_when: true` command tasks (nft endpoint reload, the kill test,
a `--user daemon-reload`, and the container `start`s). No template/copy/
package re-applies, so the stack is genuinely idempotent.

### Leak-canary recovery (svc-download)

A tripped canary halts the download stack and stays loud until cleared:

```bash
rm /var/lib/leak-canary/tripped
systemctl start dl-{sabnzbd,prowlarr,sonarr,radarr}
```

Planned maintenance on the stack: `systemctl stop leak-canary.timer` first,
`systemctl start leak-canary.timer` after (or it pings you about the stop).

### Backstop panic recovery

If the nftables backstop ever severs SSH: `qm terminal 131` on the PVE host,
then `nft flush ruleset`.

---

## 4. Environment facts

- **thurgadin** (PVE host): `192.168.1.10`, API token `automation@pve!ansible`
  (scoped: pool `homelab-svc` + storages `nvme0pool`/`local`, plus custom roles
  `homelab-sysmodify` (Sys.Modify at /) and `homelab-sdnuse` (SDN.Use on vmbr0)).
- **svc-download**: VM 131, `192.168.1.31`. **svc-media**: VM 130, `192.168.1.30`.
- **TrueNAS**: `192.168.1.20`, NFS exports `tank` + `rust`; media/backups live
  under `tank/` (`nfs_media_export` = `/mnt/sata_wd_14tb/tank/media`).
- Service uid/gid: `10001` (`homelab`).
- Remote access via a Tailscale subnet router on thurgadin's network — same
  `192.168.1.x` IPs resolve over the tunnel.

---

## 5. Fixes made this session (hard-won root causes)

Kept here so nobody re-debugs them. Full detail in the commit messages
(`14fced3`, `50469af`, `8546af3`, `1a5af45`, `59943d1`).

**Control node / Ansible plumbing**
- `community.general.yaml` stdout callback was removed in v12 → builtin
  default callback + `callback_result_format=yaml`.
- macOS Homebrew python blocks global pip (PEP 668) → project `.venv`;
  Makefile auto-detects `.venv/bin`.
- `group_vars/all_vault.yml` maps to a nonexistent group `all_vault` and was
  silently never loaded → moved to `group_vars/all/{main,vault}.yml`.
- API-driven local tasks must pin `ansible_python_interpreter` to the venv
  python (proxmoxer lives there, not in Homebrew python).
- SSH keepalives (`ServerAliveInterval`) so nft ruleset loads that sever the
  session fail fast instead of hanging the worker forever.

**PVE / provisioning**
- `community.proxmox` renamed the `serial0=` param to a `serial:` dict.
- Scoped token 403s were peeled off one class at a time: `Sys.Modify` at `/`
  (serial config) and `SDN.Use` on the bridge (NIC attach) — granted via
  single-privilege custom roles, documented in the README + vault example.
- A bundle-era VM outside the pool is invisible to the pool-scoped token's
  existence check → adopt it first: `pveum pool modify homelab-svc -vms 131`.
- Fresh VM host keys must be trusted before first SSH (verified from two
  vantage points because host_key_checking is on, deliberately).

**svc-download (jail)**
- Create `/etc/netns/vpn` before writing `resolv.conf` into it.
- NFS exports are under the `tank` dataset (`tank/media`, `tank/backups`).
- Don't manage owner/group on NFS mountpoints — root-squash rejects `chgrp`
  once the mount is live; server-side ownership is authoritative.
- `runuser -u` rejects the `#uid` form → use `setpriv` for NFS write-probes,
  probe cleanup, and backup-subtree creation (root gets squashed, can't unlink).
- `patch-window.nft` used `add rule`, which appends *below* the chain's final
  `log … drop` where it never matches → `insert rule` at the top. Image pulls
  and the package-install patch window both depend on this.
- `systemd_socket_proxyd_bind_any`/`connect_any` booleans needed for the proxy
  socket *binds* (denial is EACCES with no audit record — systemd's internal
  labeling path).
- **The big one:** the `*-proxy.service` units set `NoNewPrivileges=yes`. Under
  NNP, SELinux *silently skips* the `init_t → systemd_socket_proxyd_t` domain
  transition unless an explicit `nnp_transition` allow exists. proxyd then ran
  as `init_t`, its connect() to the jail veth was denied, and the booleans were
  inert (wrong domain). The denial surfaces ONLY as `security_bounded_transition`
  in the raw `audit.log`, never a plain AVC. Fixed with `proxyd-nnp.te`.
- Jail-membership checks must compare the container PID's `/proc/<pid>/ns/net`
  inode against `/run/netns/vpn`. podman's `SandboxKey` is EMPTY for containers
  joined to a pre-made netns via `--network=ns:`, so the old check false-
  positived a LEAK and the canary stopped the stack mid-deploy.
- Backstop output chain needs `oifname "veth-host" accept` so the LAN proxies
  can dial the jailed apps; and the template needs a reload handler (the
  endpoint refresh only `nft -f`s when the table is absent).
- Package installs gate behind `rpm -q` presence + patch window — the loaded
  backstop blocks the DNS a `dnf` metadata refresh needs, breaking re-runs.

**svc-media**
- GenericCloud ships neither `firewalld` nor `python3-firewall`; install both
  and enable/start firewalld before the rich rules (else `INVALID_ZONE`).
- Create `tailscaled.service.d` / `caddy.service.d` before writing drop-ins.
- Confined caddy (`httpd_t`) needs vhost ports `8444-8449` labeled
  `http_port_t` (only 8443 is by default) and HTTP/3 disabled (SELinux labels
  the ports tcp-only; QUIC binds EACCES).
- Caddy → tailscaled localapi socket needs a small SELinux module
  (`caddy-tailscale.te`); no boolean covers it. Requires the tailnet admin to
  enable HTTPS Certificates first.
- Container NFS exec-probes run with `chdir: /tmp` — podman dies when its CWD
  (`~straderb`, 0700) is unreadable after the uid drop.

**Both**
- SELinux module compiles use a fresh `mktemp -d`, not `cd /tmp` with fixed
  filenames (which collide with pre-existing sticky-bit `/tmp` artifacts).
