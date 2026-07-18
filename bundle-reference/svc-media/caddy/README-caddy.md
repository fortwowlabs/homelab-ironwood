# Caddy on svc-media (Rocky 10)

Replaces `tailscale-serve.sh` as the access layer (keep Serve if you prefer
zero-config; don't run both on the same ports). What Caddy buys over Serve:
one declarative file for every vhost, room for auth middleware/headers later,
and proxying to other hosts (the optional Sonarr/Radarr vhosts).

## Install

```bash
# Caddy ships in EPEL for EL; COPR (@caddy/caddy) is the upstream-recommended
# alternative if the EPEL build lags. Pick ONE:
dnf install -y caddy                       # EPEL (epel-release already present)
# dnf copr enable -y @caddy/caddy && dnf install -y caddy
```

## Let Caddy fetch ts.net certs from tailscaled

```bash
systemctl edit tailscaled
# add:
#   [Service]
#   Environment=TS_PERMIT_CERT_UID=caddy
systemctl restart tailscaled
```

## Environment + deploy

```bash
# /etc/sysconfig/caddy  (EL caddy unit reads this)
TS_HOST=svc-media.<your-tailnet>.ts.net
TS_IP=$(tailscale ip -4)        # pin the literal value; don't leave the $() in

install -m 644 Caddyfile /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl enable --now caddy
```

## Validate

```bash
ss -tlnp | grep caddy          # listeners ONLY on the 100.x address
curl -sk https://$TS_HOST:8444 -o /dev/null -w '%{http_code}\n'   # 200/302 from tailnet
# From a LAN-only host: 8443-8447 unreachable (expected); 8096 reachable (Jellyfin direct)
```

## Gotchas

1. **Startup ordering**: Caddy binding a specific 100.x address needs
   tailscaled up first. Add a drop-in: `systemctl edit caddy` →
   `After=tailscaled.service` + `Wants=tailscaled.service`. If Caddy still
   races the address assignment on boot, switch `bind` to `0.0.0.0` and
   firewall 8443-8447 to the tailscale0 zone instead — same result, less
   elegant.
2. **Cert fetch fails** → check `TS_PERMIT_CERT_UID`, and that HTTPS
   certificates are enabled for the tailnet (admin console → DNS → HTTPS).
3. **Jellyfin behind a proxy**: set Jellyfin's "Known proxies" to the
   svc-media host so client IPs log correctly through the :8444 path.
4. Girlfriend's shared-node access covers these ports automatically — node
   sharing exposes the machine, not ports. Hand her :8447 (Jellyseerr) and
   :8444/:8443; keep :8445/:8448/:8449 to yourself by not advertising them
   (or add Caddy basic_auth on the admin vhosts if that bothers you).
