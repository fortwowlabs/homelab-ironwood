# What else could be automated

> **Status 2026-07-25 — the recommended batch has been implemented and
> deployed.** Tier 1 (SABnzbd, Home Assistant), Tier 2.1/2.2 (Paperless and
> Mealie admin seeding) and Tier 3.1 (Grafana dashboards) are all done; the
> walkthrough no longer lists them as manual steps. Implementing them also
> uncovered two live faults not visible from reading the config — see
> "What implementing this found" at the end. The rest of this page is kept as
> the reasoning record, and the deliberately-not-automated items still stand.

Answering "is there anything you can set up more?" — yes. Below is everything in
[first-login-walkthrough.md](first-login-walkthrough.md) that does **not** have
to be a manual UI step, ranked by value, with the honest risk of each.

Written before implementation; the status note above records what was actually
done. Several of these touch services holding live state, which is why they were
proposed rather than done silently.

**Summary:** of ~20 manual steps in the walkthrough, roughly **8 could be
eliminated** and 2 more partly so. The rest genuinely require a human (choosing
passwords you alone should know, wiring an external Usenet provider, pairing a
printer).

---

## Tier 1 — Real bugs, worth fixing regardless

### 1.1 SABnzbd returns 403 through its own published URL 🐛

**Now:** `https://sabnzbd.fortwow.dev` → `403 Access denied - Hostname
verification failed`. The service is published through Caddy, has a DNS name and
a dashboard tile, and cannot be used through any of them.

**Cause:** SABnzbd's anti-DNS-rebinding allowlist. `host_whitelist` in
`/srv/appdata/sabnzbd/sabnzbd.ini` holds only the container's own hostname
(`037866f4dcbc`), which changes every time the container is recreated —
so this is not a one-time fix, it can regress on any image update.

**Automation:** an Ansible task ensuring `host_whitelist` contains
`sabnzbd.{{ service_domain }}`, derived from the catalog rather than hardcoded.

**Risk: medium.** SABnzbd rewrites `sabnzbd.ini` on shutdown, so editing it
while running can be clobbered — the task must stop the unit, edit, and start
it. That file also holds your Usenet provider credentials, so the edit must be
surgical (`lineinfile` on exactly that key), never a template that regenerates
the file.

**Recommendation: do it.** It is the only service in the estate that is
advertised but non-functional at its advertised address.

### 1.2 Home Assistant returns 400 through Caddy 🐛

**Now:** `https://home-assistant.fortwow.dev` → `400 Bad Request` until
`trusted_proxies` is hand-edited into `configuration.yaml`. This has been an
open manual follow-up since the first service batch.

**Automation:** template the `http:` block into HA's `configuration.yaml`,
deriving the proxy address from `hostvars[media_host].ansible_host` like
everything else in this repo.

**Risk: medium-high.** `configuration.yaml` is user-owned — HA and its UI editors
write to it, and clobbering it would destroy your automations. Safe form is a
`blockinfile` with a marker, applied idempotently, *not* a full template. It also
has an ordering constraint: HA must be onboarded first, because it will not start
with a partially-written config on a fresh install.

**Recommendation: do it, with `blockinfile` + marker,** gated on the file already
existing so it never runs on an un-onboarded instance.

---

## Tier 2 — Removes a manual step cleanly, low risk

### 2.1 Paperless-ngx superuser

Paperless supports `PAPERLESS_ADMIN_USER` / `PAPERLESS_ADMIN_PASSWORD` /
`PAPERLESS_ADMIN_MAIL` env vars that create the superuser on first start — the
same pattern NetBox and Nextcloud already use here, where the account arrives
pre-made from the vault.

**Risk: low.** Add a vault var + two env lines to the existing quadlet. Paperless
only applies them when no superuser exists, so it is inert on an already-set-up
instance. **Recommendation: do it** — it is strictly consistent with how the
other bespoke services already work.

### 2.2 Mealie default admin

Mealie seeds `changeme@example.com` / `MyPassword` when unset — a publicly-known
default on a LAN-reachable service. It can be pointed at vault-supplied values
instead.

**Risk: low.** Env-var only. **Recommendation: do it**, and it closes a small
security hole rather than merely saving a click.

### 2.3 Calibre-Web's `admin` / `admin123` — CHANGED BY HAND 2026-08-03

Same shape, worse: this default was live and widely known, and for a long time
it was the most exposed default in the estate. Calibre-Web has no env-var
override, so automating it means writing to its SQLite `app.db` at deploy time.

**Risk: medium** — direct DB manipulation is fragile across upgrades.
**Recommendation, unchanged and still correct: change it by hand** at first
login (it is one step), and do *not* automate. That reasoning does not expire
with the fix — a rebuilt Calibre-Web ships the same default again, and the
right answer will still be 30 seconds in its own UI rather than a deploy-time
write into SQLite.

**Done on 2026-08-03**, by hand, in the UI. Confirmed independently rather than
assumed: the nightly credential canary now reports
`default rejected (HTTP 200, login form re-rendered)` for calibre-web, which is
the measured rejection signature and not merely the absence of an acceptance.
See `roles/service_vm/templates/credential-canary.sh.j2`.

### 2.4 Syncthing GUI password — RESOLVED 2026-07-28

Syncthing's web UI was unauthenticated and open to anyone on the LAN. Writing a
password into its `config.xml` was rejected as too fragile: Syncthing rewrites
that file itself and wants a bcrypt hash, so it needed the same stop-edit-start
dance as SABnzbd.

**Solved from the other direction instead.** Syncthing is now behind Authelia
forward-auth (`sso_protected_services`), so `syncthing.fortwow.dev` requires a
login without touching Syncthing's own config at all. That is what made this
tractable — the fix was one line in a list, not a config round-trip.

Residual, and worth being precise about: the GUI is still unauthenticated
*underneath*. Only the Caddy hostname is gated, so `192.168.1.32:8384` is still
open on the LAN. That is an acceptable trade here (the direct port is also the
escape hatch if SSO breaks) but it is not the same as "Syncthing now has a
password".

---

## Tier 3 — Provisioning, not credentials

### 3.1 Grafana dashboards

The Prometheus datasource is already auto-provisioned. Dashboards can be too —
drop JSON into a provisioning directory and Grafana imports them at boot, so
"import dashboard 1860" stops being a manual step and survives a rebuild.

**Risk: low.** Pure file provisioning, same mechanism as the existing datasource.
**Recommendation: do it** — highest value-per-risk item on this page. Node
Exporter Full (1860) gives instant dashboards for all three VMs.

### 3.2 Uptime Kuma monitors

Every service already has a known URL and expected HTTP code — the same data
`make verify`'s Caddy smoke test uses. In principle monitors could be seeded from
`caddy_services`.

**Risk: high.** Uptime Kuma has no supported provisioning API; it would mean
writing directly to its SQLite DB or driving socket.io. **Recommendation: do not
automate.** Add the handful you care about by hand.

### 3.3 Immich machine learning

Currently disabled, gated on RAM. svc-infra now has 16 GB with ~9 GB free, so
there is headroom. This is a config change, not onboarding: add the
`immich-machine-learning` quadlet (image already pinned) and flip
`IMMICH_MACHINE_LEARNING_ENABLED=true`.

**Risk: low-medium** — it is a memory-hungry container on a VM that also runs
Nextcloud now. **Recommendation: your call**; I would do it, then watch
`free -m` for a day.

---

## Tier 4 — Genuinely needs a human

Not automatable, listed so the "what's left" picture is complete.

| Step | Why it stays manual |
|---|---|
| Vaultwarden account | Only you should know this password — it guards everything else |
| Jellyfin / Immich / Uptime Kuma / Bambuddy admin accounts | You choose the credentials; no env-var seeding offered upstream |
| Jellyfin libraries | Needs your judgement about paths and metadata |
| Seerr → Jellyfin link | Interactive OAuth-ish flow against a live Jellyfin |
| Prowlarr indexers | Your indexer accounts and API keys |
| Bambuddy printer pairing | Physical device pairing |
| Semaphore project/repo/SSH key | Deliberately out of scope; it would let a UI drive this repo |
| Beszel password change | Currently a known plaintext in `LLM-TODO-LIST.md` |
| Minecraft whitelist | Per-player, ongoing — use `rcon-cli` |

---

## Suggested batch

If you want these done, the sensible grouping is:

1. **Fix the two bugs** — SABnzbd `host_whitelist`, HA `trusted_proxies`. Both
   make an already-deployed service work at the URL it already advertises.
2. **Seed the three credentials** — Paperless superuser, Mealie admin, and a
   Grafana dashboard provisioning directory. All low-risk, all remove a step.
3. **Optionally** enable Immich ML now that the RAM exists.

That would take the walkthrough from ~20 manual steps to ~12, and every one
remaining would be a genuine human decision rather than a chore.

Deferred deliberately: Calibre-Web's password (change by hand — automating it
is more fragile than the problem; done by hand 2026-08-03, see 2.3) and Uptime
Kuma monitors (no supported provisioning path). Syncthing was on this list too,
until SSO removed the need to touch its config at all — see 2.4.

---

## What implementing this found (2026-07-25)

Two faults that neither the config nor `make verify` revealed, both surfaced
only by deploying the change and checking the result:

**Paperless had been broken for a day.** Seeding its superuser did nothing at
first, because `paperless-redis` had been wedged since 2026-07-24 06:40 with
`MISCONF ... unable to persist to disk`, which blocked Django's migrations
entirely. Every health check was green — the container was "up", the service
was "active", and the Caddy smoke test passed — while the application could not
complete startup.

That is the same incident commit `06ee437` was supposed to have fixed. Its
recorded lesson, *"use valkey, never stock redis"*, blamed the wrong thing:
valkey's entrypoint also drops from container-root to its own unprivileged
user, which under rootless podman is a subuid that can never own a
homelab-owned bind mount. The image was never the problem — the **volume** was.
Fixed by removing it and disabling persistence, matching `immich-redis` and
`nextcloud-redis`. The same latent bug was fixed in `nextcloud-redis` earlier
the same day, found the same way.

**Mealie's public default credentials were live.** `changeme@example.com` /
`MyPassword` accepted logins on the LAN. Worth stating plainly: this was found
by *trying it*, not by reading the catalog, which looked entirely reasonable.

The lesson for both: a service being deployed, active and returning 200 is not
evidence that it works. Several of the checks in this repo confirm the
container is running, not that the application is functional.

## Still deliberately not automated

Unchanged from the analysis above:

- **Calibre-Web** `admin` / `admin123` — no env-var override; automating means
  writing to its SQLite `app.db`, which is more fragile than the 30-second
  manual change. That stays the decision. **The password itself was changed by
  hand on 2026-08-03** and the nightly credential canary confirms the default
  is rejected, so this is no longer an open exposure — it is a rebuild step.
  Note the SSO work of 2026-07-28 did *not* cover this service: Calibre-Web is
  loopback-only on svc-media, so unlike every service that went behind
  forward-auth it would have had no direct `IP:port` escape hatch. That is
  still true, and is why the manual first-login change matters on any rebuild.
- **Syncthing** GUI password — no longer urgent: the web UI is behind SSO as of
  2026-07-28 (see 2.4). Its own GUI password is still unset, which only matters
  on the direct `192.168.1.32:8384` path.
- **Uptime Kuma monitors** — no supported provisioning path.
- Everything in Tier 4: account creation where only you should know the
  password, indexer credentials, printer pairing, Semaphore's project wiring.

## Consider later — accepted risks from the 2026-07-29 review

Each of these was found, understood and deliberately left alone. They are here
so that "we decided" does not decay into "nobody noticed". None is a live
compromise; all are things that make a future mistake more expensive.

- **Direct `IP:port` bypasses SSO.** The fourteen protected services are still
  reachable unauthenticated from the whole flat LAN and the tailnet, because
  firewalld and the nftables backstop open every catalog `ui_port` to
  `lan_cidr`. Kept as the recovery path if Authelia breaks. Closing it means
  scoping those ports to svc-media's address, which would also need
  `searxng` (Open WebUI dials it cross-VM), `shelfmark` and Cockpit left open,
  an update to the rendered-nft assertion in `validate_generated_catalog.py`,
  and a check of every hand-made Uptime Kuma monitor.
  - **The scan report page is the sharpest case of this, and was accepted
    knowingly on 2026-07-30.** `scan.<domain>` is behind Authelia, but the same
    content is served with no authentication at all on svc-infra:8085, because
    the `scan-reports` catalog entry gets the same `lan_cidr` firewalld rule as
    every other app. Unlike the other twelve, what leaks here is not a service
    someone could already reach — it is the *index*: every unpatched CVE by
    host and severity, and once later branches land, every open port, every
    failed benchmark rule, and every service still on a default credential.
    That is a target list, and it is one an attacker on the LAN would otherwise
    have to spend time and noise assembling. It was accepted for consistency
    with the other entries rather than because the content is comparable; if
    exactly one port in this estate is ever scoped to svc-media, it should be
    this one, via a `firewall_source` field on the catalog entry plus the
    corresponding update to the rendered-nft assertion.
- **The Authelia session cookie is scoped to the parent domain**, so Caddy
  forwards it to the ~20 vhosts *outside* forward_auth as well. Any of those
  backends can read a logged-in user's `authelia_session` and replay it against
  a protected one. Fixing it means stripping just that cookie per-vhost, which
  Caddy can do but fiddlily; the alternative — narrowing the cookie domain —
  is what makes single sign-on work at all.
- **The nightly runner's SSH key and vault password.** See
  `unattended.md`. Narrowing it means a forced-command wrapper and a dedicated
  sudoers entry instead of reusing the `NOPASSWD:ALL` deploy account.
- **Ping UUIDs and the ntfy token travel as `curl` argv**, where
  `/proc/<pid>/cmdline` is world-readable, so any local account can read them
  during the request. A healthchecks UUID is a bearer credential: holding one
  lets you keep a check green forever, which is worse than no check. Fix is
  `curl --config` or reading the value from the already-injected environment by
  name. Currently near-moot — `vault_ntfy_token` is empty and ntfy runs
  unauthenticated.
- **`netbox-redis` keeps a chowned persistent volume** (`--appendonly yes`),
  against the rule in `CLAUDE.md` that cache and broker containers should get no
  volume at all. Blast radius is limited by its authenticated `PING`
  healthcheck, but this is the configuration that wedged two redis containers
  here before.
- **svc-download's node_exporter runs `SecurityLabelDisable=true` while
  rootful and mounting `/`.** The justifying comment was copied from the
  rootless svc-media/svc-infra copies, where it is accurate. The narrower fix
  is `SecurityLabelType=spc_t`, which keeps a label.
- **~~No scheduled canary publishes to `homelab-alerts`.~~ Largely closed
  2026-08-04 — read what it does and does not cover.** Every alert path is
  exercised only when something breaks, so a broken delivery path — wrong topic
  subscription, an ACL, a rejected token — produced silence that was
  indistinguishable from good news. The heartbeat only probes ntfy's
  `/v1/health`, which proves its HTTP server answers, not that a message can be
  published, and `scan.yml`'s nightly summary publishes to `homelab-deploy`,
  which is a different topic and the muted one.

  Worth recording why this looked covered when it was not: until 2026-08-03 the
  alert topic *did* get nightly traffic, but only because Calibre-Web still
  accepted its shipped default and the scan escalated it every night. That was
  a symptom being read as a heartbeat. Fixing the password took the topic to
  zero scheduled messages, which is how the gap became visible.

  **Now covered:** `homelab-alert-canary.timer` on svc-infra publishes one
  `min`-priority message to `ntfy_alert_topic` every Monday at 08:00, through
  the same `/etc/homelab-notify.env` topic resolution every shell alerter uses,
  and then reads the message back off the topic — so a 200 from ntfy is not
  mistaken for the message having landed where it was addressed. It runs from
  svc-infra rather than svc-media on purpose: svc-media *is* the ntfy host, so
  a canary there would succeed over loopback in exactly the scenario where
  every other host's alerts have gone silent. Failure is reported three ways
  that do not all depend on ntfy: syslog, a `failed` unit that
  `homelab-failedunits.service` and `make verify` both see locally, and an
  `OnFailure` push.

  **Not covered, and not coverable server-side:** that a phone is still
  subscribed, and that anybody reads it. The canary bounds how long delivery
  can be broken without anyone knowing at one week — it does not enforce that
  the week's message is looked at. If these stop arriving, the alert path is
  down and every quiet night since the last one proved nothing.
- **`pve_mon_verified` is never asserted** by the final play in `verify.yml`, so
  a `pve_mon_hosts` play that matched nothing still yields a green run. Left
  alone because the nightly runner legitimately excludes that play by `--limit`,
  so the fix needs an explicit "was the hypervisor in scope" flag rather than a
  bare assert. Partly mitigated: the zed and smartd wiring assertions now also
  run daily inside `pve-health.sh` on the host itself.
- **`systemd-analyze verify` has never run** against this repo's units.
  `validate_systemd_units.py` downgrades itself to static checks on non-Linux
  hosts, the workstation is macOS, and CI has never fired.
