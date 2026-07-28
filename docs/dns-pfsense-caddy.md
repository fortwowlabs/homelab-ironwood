# DNS and HTTPS for `*.fortwow.dev`

This runbook makes `https://jellyfin.fortwow.dev` and every other catalogued
service work on the LAN and tailnet. The names use split-horizon DNS: dnsmasq
answers them privately, while the public Cloudflare zone has no service
records. Nothing in this design opens an inbound Internet port.

pfSense and Tailscale are not Ansible targets, so their one-time DNS settings
remain manual.

## How the pieces fit

```text
LAN client --> pfSense Unbound -- fortwow.dev --> dnsmasq on svc-media
tailnet client --> Tailscale split DNS --------> dnsmasq on svc-media
                                                   |
                     every service name -> 192.168.1.30
                                                   |
                                                   v
                             Caddy on svc-media:443
                                      |
                                      `--> service on svc-media/download/infra

Certbot --> Cloudflare DNS API --> temporary _acme-challenge TXT record
        --> Let's Encrypt DNS-01 --> publicly trusted *.fortwow.dev certificate
```

Every HTTP service name resolves to svc-media even when its backend runs on a
different VM. Caddy is the single HTTPS entry point and routes by hostname.
dnsmasq is authoritative for the service zone only and has no upstream
resolver; never configure it as a client's general-purpose nameserver.

The A records and Caddy routes come from `caddy_services` plus proxy-enabled
`download_apps`. `extra_dns_records` supplies names for raw protocols such as
Minecraft. Adding a catalog entry and running `make media` updates dnsmasq and
Caddy without another router edit.

## Public-zone and DNSSEC requirements

Keep the Cloudflare `fortwow.dev` zone active but empty of service records.
Certbot creates and removes the ACME TXT record during issuance and renewal.

Leave Cloudflare zone DNSSEC—and therefore registrar DS records—disabled. The
`.dev` parent is signed; publishing a DS record for the public Cloudflare zone
would cause validating resolvers to reject the unsigned split-horizon answers
from dnsmasq.

If DNSSEC must be enabled for the public zone, add this narrowly scoped
exception in pfSense under Services > DNS Resolver > General Settings >
Display Custom Options:

```text
server:
    domain-insecure: "fortwow.dev"
```

Do not disable DNSSEC globally.

## Configure pfSense

First check whether the old override exists:

```bash
dig +short @192.168.1.1 jellyfin.fort.wow
dig +short @192.168.1.30 jellyfin.fortwow.dev
```

In pfSense, open Services > DNS Resolver > General Settings. Under Domain
Overrides, add:

| Field | Value |
|---|---|
| Domain | `fortwow.dev` |
| IP Address | `192.168.1.30` |
| Description | `homelab services (dnsmasq on svc-media)` |

Save and apply the change. Remove any `fort.wow` domain override and any
`domain-insecure: "fort.wow"` custom option. Also remove conflicting host
overrides for individual `fortwow.dev` names because they take precedence over
the domain override.

If pfSense uses DNS Forwarder rather than DNS Resolver, add the equivalent
Domain Override there. Configure only the service that owns port 53.

## Configure Tailscale split DNS

In the Tailscale admin console under DNS > Nameservers, replace the `fort.wow`
split-DNS domain with `fortwow.dev`; keep `192.168.1.30` as its restricted
nameserver. Confirm the dedicated subnet router still advertises and has
approval for `192.168.1.0/24`.

No custom CA installation is required on LAN or remote devices. Let's Encrypt's
normal public chain is trusted by browsers, iOS/iPadOS, Android, and standard
operating-system trust stores.

## Certificate issuance and renewal

`svc-media` installs Certbot and its Cloudflare DNS plugin from EPEL. The
Cloudflare token lives only in the encrypted Ansible vault and renders to
root-only `/etc/letsencrypt/cloudflare.ini`. The token needs Zone/DNS/Edit for
the `fortwow.dev` zone and no broader scope.

Certbot stores the wildcard under
`/etc/letsencrypt/live/fortwow.dev/`. An Ansible task performs the initial copy
to `/etc/caddy/certs/`; the deploy hook at
`/etc/letsencrypt/renewal-hooks/deploy/caddy.sh` repeats the copy and reloads
Caddy after every successful renewal. `certbot-renew.timer` runs unattended.

Test the complete ACME path without consuming production rate limits:

```bash
sudo certbot renew --dry-run --run-deploy-hooks
sudo systemctl is-active certbot-renew.timer
sudo systemctl is-enabled certbot-renew.timer
sudo systemctl list-timers 'certbot-renew*'
```

## Verify

After applying the pfSense and Tailscale changes, flush the client DNS cache or
renew DHCP, then test:

```bash
dig +short @192.168.1.30 jellyfin.fortwow.dev  # dnsmasq: 192.168.1.30
dig +short @192.168.1.1 jellyfin.fortwow.dev   # pfSense: 192.168.1.30
dig +short jellyfin.fortwow.dev                # normal client path

curl --fail --head https://home.fortwow.dev
openssl s_client -connect home.fortwow.dev:443 \
  -servername home.fortwow.dev </dev/null 2>/dev/null \
  | openssl x509 -noout -issuer -ext subjectAltName
```

The certificate must have a Let's Encrypt issuer and `DNS:*.fortwow.dev` in
its SAN. Run the same DNS and HTTPS checks off-LAN over Tailscale. Finally, open
a service on an iPad or phone that has no Caddy root installed; it should show
a normal padlock with no warning.

Expected application responses include redirects and authentication
challenges. A `000` response is the important failure: DNS, routing, or TLS did
not complete.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Direct dnsmasq query works but pfSense query is empty | Missing/unapplied domain override, conflicting host override, or DNSSEC rejection |
| LAN works but tailnet does not | Tailscale split-DNS domain or subnet route is missing |
| One name is missing | The service is absent from `caddy_services`, proxy-enabled `download_apps`, or `extra_dns_records` |
| All names resolve to `.30` although backends use `.31`/`.32` | Correct: Caddy fronts every HTTPS service |
| Certificate is untrusted or names the wrong domain | Caddy is serving the old/internal certificate or the Certbot copy hook failed |
| Renewal fails | Check token scope, `/etc/letsencrypt/cloudflare.ini` permissions, Certbot logs, and Cloudflare zone status |

Diagnose from the authoritative server outward:

```bash
dig +short @192.168.1.30 <service>.fortwow.dev
dig +short @192.168.1.1  <service>.fortwow.dev
dig +short                <service>.fortwow.dev
curl --head https://<service>.fortwow.dev
```

Avoid `curl -k` in acceptance testing: chain verification is one of the
properties this configuration is meant to prove.
