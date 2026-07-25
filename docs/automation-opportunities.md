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

**Now:** `https://sabnzbd.fort.wow` → `403 Access denied - Hostname
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

**Now:** `https://home-assistant.fort.wow` → `400 Bad Request` until
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

### 2.3 Calibre-Web's `admin` / `admin123`

Same shape, worse: this default is live right now and widely known. Calibre-Web
has no env-var override, so automating it means writing to its SQLite `app.db`
at deploy time.

**Risk: medium** — direct DB manipulation is fragile across upgrades.
**Recommendation: change it by hand** at first login (it is one step), and do
*not* automate. Flagged here because it is the most exposed default in the
estate, not because it should be scripted.

### 2.4 Syncthing GUI password

Syncthing's web UI is unauthenticated until you set a password; it is currently
open to anyone on the LAN. The password can be written into its `config.xml`.

**Risk: medium.** Syncthing rewrites `config.xml` itself and wants a bcrypt
hash, so the same stop-edit-start dance as SABnzbd. **Recommendation: set it by
hand now** (30 seconds), and automate only if it keeps getting lost on rebuilds.

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

Deferred deliberately: Calibre-Web and Syncthing passwords (change by hand —
automating them is more fragile than the problem), and Uptime Kuma monitors
(no supported provisioning path).

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
  manual change. **Still live — change it early.**
- **Syncthing** GUI password — unauthenticated until set; needs a bcrypt hash
  written into a file Syncthing rewrites itself. **Still live — set it early.**
- **Uptime Kuma monitors** — no supported provisioning path.
- Everything in Tier 4: account creation where only you should know the
  password, indexer credentials, printer pairing, Semaphore's project wiring.
