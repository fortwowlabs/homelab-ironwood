# Service DNS names (`*.fort.wow`)

Every service is reachable by name over HTTPS instead of `IP:port` — e.g.
`https://jellyfin.fort.wow`, `https://sonarr.fort.wow`. This is driven entirely
by the **`caddy_services`** dict in
[inventory/group_vars/all/main.yml](../inventory/group_vars/all/main.yml): add a
service there and it gets both a DNS record and a reverse-proxy vhost. No other
file needs editing.

## How it works

```
client ──▶ DNS: <name>.fort.wow ──▶ 192.168.1.30 (svc-media)
                                         │
                         Caddy :443 (internal-CA TLS), routes by Host header
                                         │
        ┌────────────────────────────────┼─────────────────────────────┐
   127.0.0.1 backends on svc-media           192.168.1.31 (svc-download)
   jellyfin/abs/romm/jellyseerr              sonarr/radarr/prowlarr/sabnzbd
                                             (via the existing LAN socket proxies)
```

- **dnsmasq** on svc-media is authoritative for `fort.wow` (rendered from
  `caddy_services`, [roles/svc_media/templates/dnsmasq-fortwow.conf.j2](../roles/svc_media/templates/dnsmasq-fortwow.conf.j2)).
- **Caddy** on svc-media reverse-proxies each name to its backend
  ([roles/svc_media/templates/Caddyfile.j2](../roles/svc_media/templates/Caddyfile.j2)),
  with TLS from Caddy's **internal CA** (`fort.wow` is a private TLD, so no
  public Let's Encrypt).

## Three one-time manual steps

Ansible sets up dnsmasq + Caddy automatically. These three glue steps are
outside the repo's reach (pfSense/Tailscale/your devices) and only need doing
once:

### 1. Point the LAN at the resolver (pfSense)

pfSense → **Services ▸ DNS Resolver (Unbound) ▸ General ▸ Domain Overrides** →
Add:

| Domain | IP | Description |
|--------|-----|-------------|
| `fort.wow` | `192.168.1.30` | homelab service names |

Now every LAN client that uses pfSense for DNS (all DHCP clients) resolves
`*.fort.wow`.

### 2. Point the tailnet at the resolver (Tailscale)

Tailscale admin console → **DNS ▸ Nameservers ▸ Add nameserver ▸ Custom** →
`192.168.1.30`, toggle **Restrict to domain** = `fort.wow`. Because the
subnet router already advertises `192.168.1.0/24`, remote devices resolve and
reach the names exactly like on the LAN.

### 3. Trust the internal CA (each device, once)

Caddy issues certs from its own CA, so browsers need its root installed or
you'll get a warning. `make deploy` fetches the root to the repo root as
**`fort.wow-root-ca.crt`** (gitignored). Install it:

- **macOS:** `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain fort.wow-root-ca.crt`
- **iOS:** AirDrop/email the file → Settings ▸ General ▸ VPN & Device Management
  ▸ install profile, then Settings ▸ General ▸ About ▸ Certificate Trust
  Settings ▸ enable it.
- **Linux:** copy to `/etc/pki/ca-trust/source/anchors/` (EL) or
  `/usr/local/share/ca-certificates/` (Debian) → `update-ca-trust` /
  `update-ca-certificates`.
- **Firefox** uses its own store: Settings ▸ Privacy ▸ Certificates ▸ View
  Certificates ▸ Import, tick "trust for websites".

(Skip this if you're fine clicking through the browser warning, or later swap
`local_certs` for a real domain + DNS-01 wildcard — only the Caddy `tls`
directive changes.)

## Adding a service later

Add one line to `caddy_services`, then `make media USE_VAULT_FILE=1`:

```yaml
caddy_services:
  mynewapp: { backend: "127.0.0.1:1234" }        # on svc-media
  # or:      { backend: "192.168.1.31:5678" }    # on svc-download
  # https backend w/ self-signed cert:
  # cockpit-media: { backend: "127.0.0.1:9090", scheme: https, tls_skip_verify: true }
```

dnsmasq gets `mynewapp.fort.wow → svc-media` and Caddy proxies it. (A
svc-download backend also needs its port opened in that VM's nftables backstop
input chain and a socket proxy — see the download role.)
