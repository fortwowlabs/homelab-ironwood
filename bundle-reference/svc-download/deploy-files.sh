#!/usr/bin/env bash
# deploy-files.sh — Phase 4a in one shot. Run as root on svc-download, from
# inside the copied svc-download/ directory.
#
# Idempotent: safe to re-run. Never clobbers the two secret files
# (/etc/wireguard/peer.conf, /etc/vpn-netns.env) once they exist.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "FATAL: run as root (sudo -i)" >&2; exit 1; }
[[ -f vpn-netns-up.sh && -d quadlets ]] \
    || { echo "FATAL: run from inside the svc-download/ directory" >&2; exit 1; }

echo "== packages (needs egress — do this BEFORE enabling the backstop)"
dnf install -y epel-release
dnf install -y wireguard-tools podman curl nfs-utils
test -x /usr/lib/systemd/systemd-socket-proxyd \
    || { echo "FATAL: systemd-socket-proxyd missing (ships in systemd rpm?)" >&2; exit 1; }

echo "== SELinux boolean for containers on NFS volumes"
setsebool -P virt_use_nfs 1

echo "== scripts -> /usr/local/sbin"
install -m 750 vpn-netns-up.sh vpn-netns-down.sh leak-canary.sh \
               refresh-mullvad-endpoints.sh /usr/local/sbin/

echo "== systemd units + socket proxies"
install -m 644 vpn-netns.service leak-canary.service leak-canary.timer \
               proxies/*.socket proxies/*.service /etc/systemd/system/

echo "== quadlets (rootful)"
install -m 644 quadlets/*.container /etc/containers/systemd/

echo "== nftables backstop files (NOT enabled here — Phase 4d does that)"
mkdir -p /etc/nftables
install -m 644 host-backstop.nft patch-window.nft /etc/nftables/
[[ -f /etc/nftables/mullvad-endpoints.nft ]] \
    || install -m 644 mullvad-endpoints.nft.seed /etc/nftables/mullvad-endpoints.nft

echo "== secrets (seeded only if absent)"
mkdir -p /etc/wireguard
if [[ ! -f /etc/wireguard/peer.conf ]]; then
    install -m 600 peer.conf.example /etc/wireguard/peer.conf
    NEED_PEER=1
fi
if [[ ! -f /etc/vpn-netns.env ]]; then
    install -m 600 vpn-netns.env.example /etc/vpn-netns.env
    NEED_ENV=1
fi

echo "== netns resolver (Mullvad in-tunnel DNS)"
mkdir -p /etc/netns/vpn
echo 'nameserver 10.64.0.1' > /etc/netns/vpn/resolv.conf

echo "== appdata dirs (owned by 10001 = container PUID/PGID) + mountpoints"
install -d -o 10001 -g 10001 \
    /srv/appdata/sabnzbd /srv/appdata/nzbhydra2 /srv/appdata/prowlarr \
    /srv/appdata/sonarr /srv/appdata/radarr /var/lib/sabnzbd-incomplete
install -d /srv/media

echo "== quadlet generation dry-run (must list all 5 dl-* units, no errors)"
systemctl daemon-reload
/usr/libexec/podman/quadlet -dryrun 2>&1 | grep -E 'dl-|error' || true

cat << 'EOF'

DONE. Remaining manual steps, in order (DEPLOY.md Phase 4b-4f):
  1. Edit /etc/wireguard/peer.conf   (PrivateKey / PublicKey / numeric Endpoint;
                                      NO Address= or DNS= lines — wg(8) syntax)
  2. Edit /etc/vpn-netns.env         (WG_ADDR=<Mullvad Address, e.g. 10.65.x.x/32>)
  3. Add the /srv/media NFS fstab entry and mount it        (Phase 4c)
  4. Enable the nftables backstop — READ THE LOCKOUT WARNING (Phase 4d)
  5. systemctl enable --now vpn-netns.service, then leak tests (Phase 4e-4f)
EOF
[[ "${NEED_PEER:-}" ]] && echo ">> /etc/wireguard/peer.conf is still the TEMPLATE — edit it."
[[ "${NEED_ENV:-}"  ]] && echo ">> /etc/vpn-netns.env is still the TEMPLATE — edit it."
exit 0
