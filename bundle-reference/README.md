# Homelab VPN-Jail Bundle

Deployable artifacts for the netns-jail design: download workloads live in a
network namespace whose only egress is `wg0` — leaks are unroutable by
construction, not blocked by policy. pfSense VLAN 40 is the logging backstop.

```
svc-download/            Rocky 10 VM, LAN (192.168.1.31)
  vpn-netns-up.sh        -> /usr/local/sbin/         (jail construction)
  vpn-netns-down.sh      -> /usr/local/sbin/         (one-step rollback)
  vpn-netns.service      -> /etc/systemd/system/
  vpn-netns.env.example  -> /etc/vpn-netns.env       (WG_ADDR; chmod 600)
  peer.conf.example      -> /etc/wireguard/peer.conf (wg(8) syntax; chmod 600)
  quadlets/*.container   -> /etc/containers/systemd/ (rootful)
  proxies/*.{socket,service} -> /etc/systemd/system/ (UI access via veth)
  leak-canary.{sh,service,timer}                     (continuous verification)
pfsense/lan-egress-rules.md  aliases + 5-rule set + patch-window options
svc-media/               Rocky 10 VM, LAN (192.168.1.30)
  quadlets/*             -> ~homelab/.config/containers/systemd/ (rootless)
  tailscale-serve.sh     access layer; replaces Caddy entirely
```

**DEPLOY.md is the authoritative runbook (v4, grounded to the audited
thurgadin/convoker environment). The section below is a condensed reference
only — `svc-download/deploy-files.sh` now does the file placement.**

## Deploy order — svc-download

```bash
# 0. Prereqs
dnf install -y epel-release && dnf install -y wireguard-tools podman curl
# gotcha: wireguard-tools has historically lived in EPEL on EL; verify
# `dnf repoquery wireguard-tools` on Rocky 10 — kernel module is in-tree.
setsebool -P virt_use_nfs 1        # containers reading NFS-backed volumes
test -x /usr/lib/systemd/systemd-socket-proxyd   # ships in the systemd RPM on EL

# 1. Files
install -m 750 vpn-netns-{up,down}.sh leak-canary.sh /usr/local/sbin/
install -m 644 vpn-netns.service leak-canary.{service,timer} proxies/* /etc/systemd/system/
install -m 644 quadlets/* /etc/containers/systemd/
install -m 600 peer.conf /etc/wireguard/peer.conf        # from your provider
install -m 600 vpn-netns.env /etc/vpn-netns.env          # WG_ADDR=...
mkdir -p /etc/netns/vpn && echo 'nameserver 10.64.0.1' > /etc/netns/vpn/resolv.conf

# 2. Sanity: Quadlet generation before anything runs
/usr/libexec/podman/quadlet -dryrun          # must emit dl-sabnzbd/dl-nzbhydra2 without errors
systemctl daemon-reload

# 3. Jail first, then leak-test EMPTY, then workloads
systemctl enable --now vpn-netns.service
ip netns exec vpn curl -4 https://ifconfig.me         # -> provider IP
curl -4 --max-time 5 https://ifconfig.me              # -> blocked by pfSense rule 5 (or your WAN IP until VLAN rules land — do pfsense/ first)
systemctl start dl-sabnzbd dl-nzbhydra2               # generated units: start, don't enable
systemctl enable --now sab-proxy.socket hydra-proxy.socket
systemctl enable --now leak-canary.timer
```

## Acceptance tests (all must pass before real traffic)

```bash
ip netns exec vpn curl -4 https://ifconfig.me                     # provider IP
podman exec sabnzbd curl -4 https://ifconfig.me                   # provider IP (same)
podman inspect -f '{{.NetworkSettings.SandboxKey}}' sabnzbd       # /run/netns/vpn
systemctl stop vpn-netns                                          # Requires= propagation:
systemctl is-active dl-sabnzbd                                    # -> inactive (came down with the jail)
systemctl start vpn-netns dl-sabnzbd dl-nzbhydra2
/usr/local/sbin/leak-canary.sh && echo PASS
# Reboot the whole Proxmox host: everything converges unattended, and there is
# no leak window during boot — containers can't start before the jail exists,
# and a half-built jail has no route anywhere.
# pfSense: DOWNLOAD firewall log shows zero rule-5 hits at steady state.
```

## Rollback

* Whole jail: `systemctl stop vpn-netns` — ExecStop deletes the netns, which
  destroys wg0 and the veth pair; dependent containers stop via Requires=.
  `systemctl disable vpn-netns` + remove Quadlet files + `daemon-reload`
  returns a stock VM.
* Per-service: remove its `.container` file, `daemon-reload`, `systemctl stop`.
* pfSense: disable rules 3/5 (see pfsense/lan-egress-rules.md) for
  visibility-without-enforcement.

## Gotchas (the ones that will actually bite)

1. **peer.conf is wg(8) syntax, not wg-quick.** `Address=`/`DNS=`/`MTU=` in it
   will make `wg setconf` fail. Address lives in `/etc/vpn-netns.env`, DNS in
   `/etc/netns/vpn/resolv.conf`, MTU in the up script.
2. **PIA does not hand out static WireGuard configs** — keys come from their
   API (pia-foss/manual-connections) and rotate. That's friction for this
   pattern. Mullvad/IVPN/Proton give static confs with numeric endpoints and
   fit cleanly; if the US-court-tested criterion keeps you on PIA, script the
   token dance into an ExecStartPre or accept periodic manual refresh.
3. **Use a numeric Endpoint IP.** A hostname needs init-ns DNS at setconf time
   and reintroduces a dependency the design just removed.
4. **No `--userns=auto` on the download containers.** Auto-mapped subuids
   (100000+) written to NFS volumes become unmappable ownership on TrueNAS.
   LSIO PUID/PGID=10001 (matching the dataset owner) is the boundary instead.
5. **No PublishPort with `--network=ns:`** — publishing is unsupported when
   joining a foreign netns. UI access is veth + systemd-socket-proxyd only.
6. **Podman writes its own container resolv.conf** regardless of
   /etc/netns/vpn/resolv.conf; that's why each Quadlet pins `DNS=`. Verify:
   `podman exec sabnzbd cat /etc/resolv.conf`.
7. **Generated Quadlet units can't be `systemctl enable`d** — `[Install]`
   inside the .container file is what wires boot start.
8. **SABnzbd host_whitelist**: first login via 192.168.1.31 works (IP), but
   any hostname you later front it with must be added to `host_whitelist` in
   sabnzbd.ini or you get "Access denied — hostname verification failed".
9. **firewalld can stay enabled on this VM.** The kill switch is not
   firewall-based anymore, so there's no coexistence problem; use firewalld
   normally for SSH/proxy-port admission on the VLAN interface.
10. **NFS fstab**: `hard,nofail,x-systemd.automount,x-systemd.mount-timeout=30`
    + Proxmox start order TrueNAS(1, delay) -> service VMs(2), and
    `qemu-guest-agent` in both VMs for fs-freeze-consistent backups.
11. **RomM churns** — its reference compose (MariaDB + app-internal Valkey,
    `/redis-data` volume, library as `romm/roms/<platform>/`) was current at
    docs.romm.app 4.9.x; re-verify at deploy time and pin the image tag.
12. **Canary notification**: the `fail()` hook in leak-canary.sh is a stub —
    wire it to ntfy/mail/webhook, and note anything it calls must itself be
    reachable (host init ns has no general egress under pfSense rule 5; a
    LAN-internal ntfy on svc-media is the clean answer).

## svc-media quick notes

Rootless under `homelab` (uid/gid 10001): `loginctl enable-linger homelab`,
Quadlets in `~homelab/.config/containers/systemd/`, manage with
`systemctl --user -M homelab@ start audiobookshelf`. Access layer is
**Caddy over Tailscale** (all four apps; see caddy/README-caddy.md) — or
`tailscale-serve.sh` as the no-Caddy alternative covering ABS + RomM only.
Pick one; they collide on :8443. DEPLOY.md assumes Caddy.
Girlfriend gets the node **shared** into her own tailnet — quarantined by
default, sees nothing else, zero grants engineering.

## v2 expansion

Video stack (Jellyfin/Jellyseerr + Sonarr/Radarr/Prowlarr in the jail),
Caddy access layer, Cockpit container management, and the storage-layout
change that makes arr imports hardlinks instead of copies:
see **docs/v2-video.md** and **svc-media/caddy/**. Deploy v1 acceptance
tests first; v2 assumes they pass.

## No-VLAN variant (current bundle state)

svc-download sits on the flat LAN at 192.168.1.31. Leak enforcement is
unchanged — the netns jail never depended on VLANs. The pfSense egress
backstop moves to per-source-IP rules on the LAN interface
(pfsense/lan-egress-rules.md; the old VLAN doc is retained as
*.retired for the retrofit path). Lost: L2 segmentation of svc-download
from LAN neighbors — compensated (partially, honestly) by the optional
svc-download/host-backstop.nft scoped output policy. The leak canary now
asserts against https://am.i.mullvad.net/connected instead of IP-matching,
so /etc/leak-canary.env is no longer needed.
