# DNS on pfSense for `*.fort.wow` (Caddy)

How to make `https://jellyfin.fort.wow` (and every other service name) work from
any device on the LAN, instead of only from something that already knows to ask
`192.168.1.30`.

This is the one-time router-side setup referenced from
[services.md](services.md#naming-and-tls). Nothing here is automated by this
repo — pfSense is not an Ansible target.

---

## Current state (measured 2026-07-25)

Verified from the workstation, so you know exactly what is and is not working:

| Check | Result |
|---|---|
| `dig @192.168.1.30 jellyfin.fort.wow` | ✅ `192.168.1.30` — dnsmasq is serving correctly |
| `dig @192.168.1.1 jellyfin.fort.wow` | ❌ empty — **pfSense has no override yet** |
| `dig jellyfin.fort.wow` (normal client lookup) | ❌ empty |
| `curl https://mealie.fort.wow` | ❌ fails, name does not resolve |
| `dig @192.168.1.1 example.com` | ✅ resolves — pfSense itself is a healthy resolver |

So: the homelab side is **done and correct**; the router side is **not set up**.
Every service check in recent sessions used `curl --resolve`, which skips DNS —
that is why this gap stayed invisible.

After the steps below, the last three rows should all succeed.

---

## How the pieces fit

```
        client (DHCP hands out 192.168.1.1 as DNS)
                     |
                     v
    pfSense Unbound @ 192.168.1.1
       |                      \
       | *.fort.wow            \  everything else
       v                        v
  dnsmasq @ 192.168.1.30      upstream / root servers
  (authoritative, fort.wow only)
       |
       | every name -> 192.168.1.30
       v
     Caddy @ 192.168.1.30:443  --reverse proxy-->  the actual service
       (internal CA TLS)                            on .30 / .31 / .32
```

Three things worth understanding, because they explain most failure modes:

1. **Every `*.fort.wow` name resolves to `192.168.1.30`**, regardless of which
   VM the service actually runs on. Caddy is the single entry point and routes
   by `Host` header. Mealie lives on `192.168.1.32`, but its DNS answer is
   `.30` — that is correct, not a bug.

2. **dnsmasq on svc-media is authoritative for `fort.wow` and nothing else.**
   Its config sets `no-resolv` with no upstream forwarding
   (`roles/svc_media/templates/dnsmasq-services.conf.j2`). It will answer
   `fort.wow` queries and *fail* everything else. Never point a client's
   general-purpose DNS at it — use it only as a per-domain override.

3. **Caddy issues certificates from its own internal CA**, not a public one.
   Clients must trust that CA or every HTTPS request throws a certificate
   warning. That is a separate step from DNS and is easy to forget.

The A records are generated from `caddy_services` in
`inventory/group_vars/all/main.yml`, plus proxy-enabled `download_apps`, plus
the `extra_dns_records` list (used by the Minecraft servers, which are raw TCP
and have no Caddy vhost). Adding a service to the catalog gets it a DNS name
automatically on the next `make media` — no router change ever needed again
after this one-time setup.

---

## Step 1 — Domain Override in pfSense

pfSense's default resolver is **Unbound** (Services > DNS Resolver). If you use
the DNS Forwarder instead, see the variant at the end of this step.

1. pfSense web UI > **Services > DNS Resolver > General Settings**.
2. Scroll to **Domain Overrides**, click **Add**.
3. Fill in:

   | Field | Value |
   |---|---|
   | Domain | `fort.wow` |
   | IP Address | `192.168.1.30` |
   | Description | `homelab services (dnsmasq on svc-media)` |

   Leave the port blank (defaults to 53).
4. **Save**, then **Apply Changes**.

That tells Unbound: for anything under `fort.wow`, ask `192.168.1.30`; for
everything else, resolve normally. It does not route any other traffic through
the homelab.

**If you use the DNS Forwarder (dnsmasq) on pfSense instead of the Resolver**,
the equivalent lives at Services > DNS Forwarder > Domain Overrides with the
same two values. Only one of Resolver/Forwarder can own port 53 — configure
whichever is enabled.

### If it still does not resolve: DNSSEC

This is the most common cause of a correct-looking override that returns
nothing. If **DNSSEC support** is enabled in the DNS Resolver, Unbound may
refuse answers from a domain that has no chain of trust — `fort.wow` is a
private domain, so it has none.

Two ways to fix, preferring the first:

- Add `fort.wow` as a **domain insecure** entry. In Services > DNS Resolver >
  General Settings, open **Display Custom Options** and add:

  ```
  server:
      domain-insecure: "fort.wow"
  ```

  This disables DNSSEC validation for that one domain only, leaving it on
  everywhere else.

- Or disable DNSSEC globally (uncheck **DNSSEC support**). Simpler, but it
  weakens validation for all your traffic — prefer `domain-insecure`.

### Check for a conflicting Host Override

If anything under Services > DNS Resolver > **Host Overrides** already mentions
`fort.wow` or a specific service name, it wins over the domain override and will
shadow it. Remove those entries — the whole point of the override is that you
never maintain per-host records on the router.

---

## Step 2 — Trust the Caddy internal CA

DNS alone gets you to Caddy; without this you reach it and get a certificate
warning on every service.

The CA certificate is fetched to the repo root by `make access` (it is
gitignored, and already present as `fort.wow-root-ca.crt` from 2026-07-20). To
re-fetch:

```bash
make access USE_VAULT_FILE=1
```

Then install `fort.wow-root-ca.crt` into each client's trust store:

- **macOS** — double-click the file to add it to Keychain Access, then open it
  and set **Always Trust**. Or:

  ```bash
  sudo security add-trusted-cert -d -r trustRoot \
      -k /Library/Keychains/System.keychain fort.wow-root-ca.crt
  ```

- **iOS / iPadOS** — AirDrop or email the file, install the profile, then
  **explicitly** enable it under Settings > General > About > Certificate Trust
  Settings. iOS will not trust it until you toggle that second switch.

- **Linux** — copy to `/usr/local/share/ca-certificates/` (Debian/Ubuntu) or
  `/etc/pki/ca-trust/source/anchors/` (RHEL/Rocky), then run
  `update-ca-certificates` or `update-ca-trust`.

- **Firefox** — keeps its own trust store; import separately under
  Settings > Privacy & Security > Certificates > View Certificates >
  Authorities > Import.

- **Android** — Settings > Security > Encryption & credentials > Install a
  certificate > CA certificate. Note some apps ignore user-installed CAs.

This CA is unrelated to the Proxmox API CA in
[deployment.md](deployment.md#trust-the-proxmox-api-certificate) — trusting one
does not trust the other.

---

## Step 3 — Verify

From a LAN client, after renewing DHCP or flushing DNS:

```bash
# 1. The router now answers for the private domain
dig +short @192.168.1.1 jellyfin.fort.wow      # expect 192.168.1.30

# 2. Normal client resolution works (no explicit server)
dig +short mealie.fort.wow                     # expect 192.168.1.30

# 3. TLS is trusted and Caddy routes correctly
curl --fail --head https://mealie.fort.wow     # expect HTTP/2 200, no cert error

# 4. A service that lives on a DIFFERENT VM still answers .30
dig +short nextcloud.fort.wow                  # expect 192.168.1.30 (runs on .32)
```

Flush the client cache first if you tested before configuring:

- macOS: `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder`
- Windows: `ipconfig /flushdns`
- Linux (systemd-resolved): `sudo resolvectl flush-caches`

Browsers cache aggressively too — use a private window for the first check.

Expected HTTP codes across the estate: `200` for most, `302` for Nextcloud /
Grafana / Prometheus / code-server / Paperless (login redirects), `401` for
Webtop (basic auth). Anything returning `000` is a DNS or TLS failure, not an
application failure.

---

## Remote access (Tailscale)

The LAN override does not apply over the tailnet. For remote access:

1. In the Tailscale admin console > **DNS** > **Nameservers**, add
   `192.168.1.30` as a nameserver **restricted to `fort.wow`** (split DNS).
   Restricting it matters — dnsmasq cannot resolve anything else.
2. Ensure the dedicated subnet router advertises `192.168.1.0/24` and that the
   route is approved, so `192.168.1.30:443` is reachable.
3. Install the same CA certificate on the remote device.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `dig @192.168.1.1 <svc>.fort.wow` empty, `dig @192.168.1.30` works | Override missing/not applied, or DNSSEC blocking — see Step 1 |
| Some names resolve, one does not | That service is not in `caddy_services`; add it and run `make media` |
| Resolves but connection times out | firewalld on svc-media, or Caddy down — `make verify` |
| Certificate warning in browser | CA not trusted on that device (Step 2), or Firefox's separate store |
| Works on Mac, fails on iPhone | iOS needs the second "full trust" toggle |
| Everything broke at once | Check the workstation is not on a VPN capturing `192.168.1.0/24` — `netstat -rn -f inet \| grep 192.168.1` |
| Name resolves to `.30` but service runs on `.32` | Correct — Caddy fronts everything, routing by Host header |

**Diagnostic order** — work outward, stopping at the first failure:

```bash
dig +short @192.168.1.30 <svc>.fort.wow   # dnsmasq itself
dig +short @192.168.1.1  <svc>.fort.wow   # router override
dig +short               <svc>.fort.wow   # client path
curl -k --head https://<svc>.fort.wow     # reachability, ignoring TLS trust
curl    --head https://<svc>.fort.wow     # TLS trust
```

If the first line fails, the problem is in this repo (run `make media`), not on
the router.

---

## Why not put the records in pfSense directly?

You could add Host Overrides per service on the router, but then every new
service needs a manual router edit, and the catalog in
`inventory/group_vars/all/main.yml` stops being the single source of truth. The
domain override is configured **once**; after that, adding a service to the
catalog and running `make media` publishes its DNS name, Caddy vhost, and
dashboard tile together.
