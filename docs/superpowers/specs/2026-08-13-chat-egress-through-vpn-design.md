# Chat egress through the VPN

## Context

Open WebUI's web search was believed to be VPN-routed because SearXNG sits in
the Mullvad jail on svc-download. It is not, and the gap is structural rather
than a misconfiguration.

A web search is two round trips. SearXNG makes the first one — the query to the
upstream engines — and that really does leave via Mullvad, because the only
route out of the `vpn` namespace is `wg0`. SearXNG then returns a list of URLs
and stops. **Open WebUI makes the second round trip itself**, fetching each
result page from its own container on svc-infra, which has no egress policy at
all. The same path serves in-chat URL retrieval.

So the search engines see a Mullvad exit and **every site in the results sees
the home IP**. This was found on 2026-08-13 when a model was asked to fetch
`ifconfig.me` and returned the home address; the model's account of *why* was
confabulated, but the address was real.

`docs/services.md` contributed to the misreading. It says SearXNG is "jailed for
its egress" and draws the AI stack with web search arriving via Mullvad, which
is true of the query and silently untrue of the fetch.

## Verified facts the design rests on

Everything below was read out of the repo, not assumed.

- **The jail is the only egress enforcement in the estate.**
  `service_guarded_egress` is `true` on svc-download alone
  (`inventory/host_vars/svc-download.yml`); svc-media and svc-infra both set it
  `false` and run firewalld inbound-only. Neither filters outbound at all.
- **Rootless podman erases container identity at the packet level.**
  `roles/svc_infra/templates/infra-app.container.j2` runs catalog apps rootless
  with `PublishPort`, so outbound traffic is re-originated by a userspace
  network process running as the host user. By the time a packet reaches the
  host output hook its source is the host IP. **A container cannot be
  identified by address**, which rules out the obvious firewall approach and
  drives the cgroup match below.
- **`proxy: true` is not usable for a non-UI service.**
  `roles/svc_media/templates/Caddyfile.j2` loops
  `download_apps | dictsort if app.proxy` with no opt-out, publishing a vhost
  for every proxy-enabled entry; `tests/validate_generated_catalog.py` drives
  the verify probes and the backstop's LAN-wide port set from the same
  predicate. A forward proxy would get a website it cannot serve and a health
  check it must fail.
- **Bespoke-outside-the-catalog is an established pattern.** Recyclarr and the
  three Beszel agents are all deliberately not catalog entries, each with a
  comment saying why.
- **Inside the jail, every client looks like `10.77.0.1`.** The socket proxy
  runs in the init namespace and dials `10.77.0.2:<port>` across the veth, so a
  jailed service cannot distinguish its callers. This is already documented as
  the reason SearXNG runs `limiter: false`. It means **access control for the
  proxy can only be expressed in nftables**, not in the proxy's own config.
- **Own-table nftables discipline coexists with firewalld.**
  `roles/svc_download/templates/host-backstop.nft.j2` establishes the pattern:
  own table, explicit `delete table` guard, never `flush ruleset`. Independent
  tables at the same hook are all evaluated and a `drop` in any is final.
- **New alerters must be registered.** `tests/validate_alert_topics.py` carries
  an explicit `ALERT_TEMPLATES` list backed by a discovery sweep, so an
  unregistered alert script fails `make validate`.

## Scope

**In scope:** Open WebUI's outbound traffic on svc-infra, enforced rather than
requested, failing closed with an alert.

**Explicitly out of scope**, recorded because the audit below shows chat is one
leak among many and it would be dishonest to let this change read as more than
it is: every other service on svc-infra and svc-media, the maintenance-egress
window on svc-download, OS updates, container image pulls, and the Windows GPU
host. Those are named in the appendix so the decision is visible rather than
implied.

## Design

### 1. The egress path

A **tinyproxy container inside the `vpn` netns** on svc-download, listening on
`10.77.0.2:8118`. Its outbound connections originate inside the namespace, so
they leave via `wg0` by construction — the identical property SearXNG has, for
the identical reason, introducing nothing new to trust.

It is a **bespoke quadlet in `roles/svc_download`, not a `download_apps`
entry**, for the reasons under Verified facts. Its image is added to
`scan_images` explicitly so it is not an unscanned pin.

A `systemd-socket-proxyd` unit in the init namespace listens on svc-download's
LAN IP and dials `10.77.0.2:8118`. Its backstop input rule is scoped to
svc-infra alone:

```nftables
# Chat egress proxy (tinyproxy in the jail). open-webui on svc-infra is the
# only consumer, so this is scoped to that host like node_exporter below,
# not to $LAN_ADMIN.
ip saddr $INFRA_HOST tcp dport <proxy_port> accept
```

`$INFRA_HOST` rather than `$LAN_ADMIN` because tinyproxy cannot make this
distinction itself — every request reaches it from `10.77.0.1`. This line is
not one layer of two; it is **the only place the decision can be expressed**,
and a `$LAN_ADMIN` here could not be narrowed anywhere downstream.

On svc-infra, open-webui gains `http_proxy` / `https_proxy` pointing at
svc-download's LAN IP, and a `no_proxy` covering the GPU host, svc-download,
`lan_cidr` and localhost — so inference, image generation and search stay
direct at LAN speed.

### 2. Enforcement

Once the proxy is a LAN address, the whole policy for chat is *may reach the
LAN, may reach nothing else*. Everything chat legitimately does — GPU host,
SearXNG, the proxy — is on the LAN; anything internet-bound that bypassed the
proxy has an internet destination by definition.

```nftables
table inet chat_egress
delete table inet chat_egress

table inet chat_egress {
    chain output {
        type filter hook output priority 0; policy accept;

        # Everything on this VM except chat passes untouched. This table has
        # exactly one subject; svc-download's backstop guards a whole VM,
        # this guards a single unit, which is why the policy is accept.
        socket cgroupv2 level 5 \
            "user.slice/user-<uid>.slice/user@<uid>.service/app.slice/open-webui.service" \
            jump chat_policy
    }

    chain chat_policy {
        oifname "lo" accept
        ct state established,related accept
        ip daddr <lan_cidr> accept
        log prefix "chat-egress-drop " counter drop
    }
}
```

Angle-bracketed values above are rendered from inventory (`svc_uid`,
`lan_cidr`, the proxy port), not literals — per the repo's rule against
copying addresses into role data or documentation. The `level 5` depends on
the user-manager slice path and is confirmed in plan step 1, not assumed.

**Fail-closed needs no mechanism.** If the tunnel or jail is down the proxy is
unreachable, the fetch fails, and the firewall means there is no direct path to
fall back to. There is no fallback to forget to disable. Chat itself keeps
running — local inference, image generation and history are LAN or on-disk — so
an outage costs web fetching only.

### 3. Proving it works

The naive positive control is invalid and the trap is worth stating: "from
inside chat, a direct fetch must fail" is satisfied identically by a working
firewall and by a dead probe. A missing container, a broken podman or a typo in
the exec all produce *connection failed*, which reads as *enforcement working*.
That is the credential-canary failure mode described in `CLAUDE.md`.

So the probe is three checks. A stands on its own; B1 is the enforcement test,
and B2 exists solely to make B1 interpretable:

| | Check | Required result |
|---|---|---|
| **A** | From inside chat, via its configured proxy, fetch `am.i.mullvad.net/json` | 200 and `mullvad_exit_ip: true` |
| **B1** | From inside chat, `--noproxy '*'`, fetch an echo service | fails, **and** the nft drop counter increments |
| **B2** | The same direct fetch from svc-infra's host context, outside chat's cgroup | succeeds |

B2 is what gives B1 meaning: it proves the internet is reachable and curl works
at this moment, so B1's failure is attributable to the rule rather than to a
dead network or a broken harness. Sampling the drop rule's `counter` around B1
proves it was *this* rule that stopped it. **If B2 fails the verdict is
`inconclusive`, never `ok`.**

Verdicts are tri-state — `ok`, `broken`, `inconclusive` — following the pattern
commit `057e1e4` established for the credential canary. The loudest case is A
returning 200 with a non-Mullvad address: that is the original bug recurring.

**Alerting.** A timer-driven script on svc-infra publishing to ntfy, registered
in `ALERT_TEMPLATES` in `tests/validate_alert_topics.py`. Per `CLAUDE.md` the
alert is proven by publishing one and reading it back out of ntfy, not by
observing that the timer is active.

**Metrics**, via `homelab-metric-write`: `homelab_chat_egress_enforced`,
`homelab_chat_egress_vpn_verified`, `homelab_chat_egress_probe_success`. Emit
before asserting; emit the same values the alert used; on `inconclusive` exit
*before* writing rather than publishing zeros, since a zero would read as "not
enforced" and cry wolf.

**Anti-drift validator.** The nftables rule names `open-webui.service` inside a
cgroup path while the quadlet unit name derives from the catalog key — the same
fact written twice, which the `container-drift.yml` experience says will drift.
The template derives the path from the catalog key, and a validator asserts the
rendered rule matches the rendered unit name.

### 4. Documentation

- `docs/services.md` — correct the AI-stack section, which currently reads as
  though SearXNG covers chat's web traffic.
- `docs/services.md` — fix the `ENABLE_PERSISTENT_CONFIG` paragraph, which says
  `false` while `infra-apps.yml` sets `true`; its stated conclusion about
  admin-UI edits not surviving a restart is backwards as written.
- `docs/security.md` — add the chat egress boundary beside the download
  backstop, and record the appendix audit so "only svc-download filters egress"
  is written down rather than rediscovered.

## Implementation-time checks (flagged, not guessed)

- **Does `socket cgroupv2` matching work on this host?** This is plan step 1 and
  the design's one real unknown. It requires the rootless container's outbound
  socket to belong to the quadlet unit's cgroup, and the correct `level` for the
  user-manager slice path. If it does not hold, the escalation is a dedicated
  rootless podman user for open-webui matched by `meta skuid` — more machinery,
  no string to drift — and that change must be surfaced, not silently
  substituted with a best-effort `http_proxy`-only build.
- **Which tinyproxy image to pin.** The repo rejects tag refs and scans every
  digest, so this needs a maintained image chosen deliberately. Squid is the
  fallback if none is suitable.
- **Confirm `podman exec` inherits the container's proxy environment**, which
  check A depends on. If it does not, A must set the proxy explicitly and a
  separate assertion must confirm open-webui's own environment is correct —
  otherwise a wrong env var reads as a tunnel outage.
- **Confirm the nft version on svc-infra supports `socket cgroupv2 level`.**

## Residual limits, stated plainly

- **DNS is only partly covered.** Through an HTTP proxy, chat normally does not
  resolve destination hostnames — it sends `CONNECT host:443` and the proxy
  resolves inside the jail. But a library that pre-resolves before proxying
  would send that query to dnsmasq on svc-media, which forwards upstream over
  the home connection: the hostname leaks even though the fetch does not. The
  probe does not catch this. Closing it means giving chat a resolver inside the
  jail, and is deliberately deferred rather than bolted on.
- **This covers chat and nothing else.** See the appendix.
- **The cgroup path is a string.** A rule matching nothing fails open and
  silently. The three-check probe is what converts that from an invisible
  failure into a loud one; the rule alone is not sufficient and should not be
  described as if it were.

## Appendix: what else does not go through the VPN

Recorded because it was asked for and because the absence of this list is what
made the chat gap surprising.

**Read the two halves of this appendix differently.** The tiering below was
verified from the repo: `service_guarded_egress`, the backstop's output chain,
the netns assertions, and Syncthing's host networking with no relay or
discovery configuration are all facts on disk. **The per-service traffic column
is not.** It is inferred from each product's documented default behavior and was
confirmed against neither the pinned digests nor the running containers. Treat
it as a list of things to go and check, not as a measurement — several entries
would evaporate if the feature turns out to be disabled here.

The cheap way to settle it is a consequence of this very change: once the jail
proxy is running, its log is an authoritative record of every destination chat
attempts. The same trick — point a service at a logging proxy and read what it
asks for — answers the question for any row below, and is worth doing before
anyone acts on this table.

**Tier 1 — VPN by construction.** The nine containers in svc-download's `vpn`
netns. Only route is `wg0`, `ip_forward=0`, and `vpn-netns-up.sh` fails the unit
if any unexpected route appears.

**Tier 2 — svc-download's init namespace.** Default-drop output, controlled but
not VPN'd: the WireGuard handshake to Mullvad (necessarily visible to the ISP),
NFS to TrueNAS, ntfy, Beszel, DHCP, ICMP. Plus the `homelab-maintenance-egress`
window, during which dnf and image pulls go out direct and unfiltered.

**Tier 3 — svc-media and svc-infra, unfiltered.** Everything below leaves
directly:

| Service | Traffic |
|---|---|
| syncthing | Host networking, no relay/discovery config in the repo, so stock defaults: announces to public discovery servers, can relay through third parties. Continuous and identifying. |
| open-webui | Result-page fetches — the subject of this spec, and the one row here that is measured rather than inferred. Upstream defaults also govern its RAG embedding backend and its update checking, neither of which is set in the catalog and neither of which was confirmed to reach the network at all. |
| home-assistant | Host networking; integration clouds |
| vaultwarden | Website icon fetching — contacts the favicon of every domain held in the vault |
| mealie | Recipe import by URL; same shape as the chat gap |
| webtop / code-server / semaphore | User-driven; webtop contains a browser |
| uptime-kuma | Whatever external monitors are configured (stored in its DB, not the repo) |
| scan stack | Trivy DB downloads, GitHub API (~45 requests per release-check run), healthchecks.io pings |
| jellyfin / abs / romm / seerr / calibre-web | Metadata and artwork from TMDB, TVDB, Audible, IGDB, SteamGridDB |
| caddy / certbot | Let's Encrypt ACME and the Cloudflare DNS API |

**All hosts:** DNS, NTP, OS updates, container registry pulls. **The Windows GPU
host** is not managed by this repo at all; `ollama pull` goes straight to
ollama.com.
