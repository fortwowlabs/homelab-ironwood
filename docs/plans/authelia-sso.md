# Authelia SSO — forward-auth for 11 services

## Context

Most of this estate has no authentication. Anyone on the LAN or the tailnet can
reach Syncthing's GUI, Prometheus, and the seven download apps selected below
without a password. Syncthing has been an open item in
`docs/automation-opportunities.md` since the last batch, deferred because
fixing it meant writing a bcrypt hash into a file Syncthing rewrites itself.

A reverse-proxy SSO layer fixes all of them at once, in one place, without
touching any application's own config. Caddy already fronts every service by
hostname, so adding `forward_auth` to selected vhosts puts a login in front of
them with no per-app work.

**Chosen: Authelia**, single container, config entirely in-repo. Rejected
Authentik — it needs 4 containers and ~2 GB, and its configuration lives in its
own database behind a web UI, which would convert this repo's declarative model
back into manual follow-up steps. Authelia's file-based config means the users,
policies and rules are all version-controlled Jinja templates.

Scope this phase is **forward-auth only**. No native OIDC clients; apps keep
their own logins underneath.

### Why this is low-risk here (verified, not assumed)

Proxy auth usually breaks machine-to-machine integrations. In this repo it
cannot: every M2M path already bypasses Caddy entirely — ntfy publishers, the
Beszel agents, Prometheus scraping, Grafana's datasource, Recyclarr, and the
arr apps talking to each other all use IP:port or jail-localhost
(`main.yml:120`, `beszel-agent.container.j2:17`, `prometheus.yml.j2:24`,
`grafana-datasource.yml.j2:31`, `recyclarr.yml.j2:10,17`). Homepage is a pure
link launcher with no API-key widgets (`homepage/services.yaml.j2:17-19`).

## What gets protected

Eleven services, all currently unauthenticated or weakly authenticated:

| Group | Services |
|---|---|
| Download stack | `sonarr` `radarr` `prowlarr` `sabnzbd` `bazarr` `lazylibrarian` `jdownloader` |
| High-risk | `code-server` `webtop` |
| Open infra | `syncthing` `prometheus` |

**Explicitly not protected** — native/mobile clients cannot follow a browser
redirect: `jellyfin` `immich` `nextcloud` `abs` `ntfy` `vaultwarden`. Also left
alone: `home` (stays the open landing page), `it-tools`, `glances` (its
`/mcp/sse` endpoint has no cookie jar), `bambuddy`, `cockpit-*` (own PAM auth),
`shelfmark` (deliberately anonymous on the private LAN/tailnet), and every
service that already has a real login.

**`calibre-web` is deliberately excluded** — it keeps its own built-in auth and
gains no SSO layer. Consequence to carry forward: its default
`admin`/`admin123` remains live and remains a manual first-login fix. This plan
does not address it. See "Docs to update".

## Design

### 1. Deployment shape — catalog entry, not bespoke

Authelia is a single container, so it belongs in `infra_secret_apps`, not the
bespoke multi-container pattern. Config files come from two supplementary
template tasks, exactly like Prometheus (`roles/svc_infra/tasks/files.yml:589-596`).

Entry in `inventory/group_vars/all/infra-apps.yml`:

```yaml
authelia:
  image: "ghcr.io/authelia/authelia@sha256:<resolve at implementation time>"
  ui_port: 9092          # 9091 (Authelia's default) is taken by prometheus
  container_port: 9091
  volumes:
    - "%h/appdata/authelia:/config:Z"
  backup_paths: [authelia]
  env:
    TZ: "{{ timezone }}"
    AUTHELIA_SESSION_SECRET: "{{ vault_authelia_session_secret }}"
    AUTHELIA_STORAGE_ENCRYPTION_KEY: "{{ vault_authelia_storage_encryption_key }}"
    AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET: "{{ vault_authelia_jwt_secret }}"
  require_vault:
    - vault_authelia_session_secret
    - vault_authelia_storage_encryption_key
    - vault_authelia_jwt_secret
```

**Secrets go through env, deliberately.** That keeps `authelia.yml.j2` free of
any `vault_` reference, so it renders as a normal `0644` template with no
`no_log` — the config stays diffable and reviewable. Only `users.yml.j2`, which
carries the argon2id password hash, needs `0600` + `no_log: true` +
`diff: false` (required by `tests/validate_secret_tasks.py:74-81`, which keys
off the filename containing a vault ref).

New vault vars, documented in `inventory/group_vars/all_vault.yml.example`:

```yaml
vault_authelia_session_secret:            "REPLACE_openssl_rand_hex_32"
vault_authelia_storage_encryption_key:    "REPLACE_openssl_rand_hex_32"
vault_authelia_jwt_secret:                "REPLACE_openssl_rand_hex_32"
vault_authelia_user_password_hash:        "REPLACE_argon2id_PHC_string"
```

The hash is generated once with
`podman run --rm ghcr.io/authelia/authelia:<tag> authelia crypto hash generate argon2 --password '<pw>'`.
It lives in a YAML file, not a systemd `Environment=` line, so the `$` in the
PHC string needs no escaping.

### 2. Config templates

`roles/svc_infra/templates/authelia.yml.j2` — schema verified against current
Authelia docs (4.38+ renamed several keys; these are the current forms):

```yaml
theme: 'auto'
server:
  address: 'tcp://:9091/'          # 4.38+ replaced server.host/server.port
log:
  level: 'info'
authentication_backend:
  file:
    path: '/config/users.yml'
    password:
      algorithm: 'argon2'
access_control:
  default_policy: 'one_factor'     # password only, per decision
session:
  name: 'authelia_session'
  same_site: 'lax'
  expiration: '12h'
  inactivity: '2h'
  remember_me: '1M'
  cookies:                          # 4.38+ replaced the flat session.domain
    - domain: '{{ service_domain }}'
      authelia_url: 'https://auth.{{ service_domain }}'
storage:
  local:
    path: '/config/db.sqlite3'
notifier:
  filesystem:
    filename: '/config/notification.txt'   # no SMTP in this estate
regulation:
  max_retries: 5
  find_time: '2m'
  ban_time: '10m'
```

`roles/svc_infra/templates/users.yml.j2`:

```yaml
users:
  {{ authelia_admin_user }}:
    displayname: '{{ authelia_admin_user }}'
    password: '{{ vault_authelia_user_password_hash }}'
    email: '{{ authelia_admin_user }}@{{ service_domain }}'
    groups: ['admins']
```

Both render into `/opt/homelab/appdata/authelia/`, register for the restart
cascade (`files.yml:795-835`), and must be created before the unit starts.

### 3. The Caddy change — one list, both loops

`roles/svc_media/templates/Caddyfile.j2` has two vhost loops (`caddy_services`,
then proxied `download_apps`) and no auth directive of any kind today. The
protected set belongs in **one** place rather than as a field on two different
catalogs with two different validators:

```yaml
# inventory/group_vars/all/main.yml
# Services fronted by the Authelia login. One list, consumed by both Caddyfile
# vhost loops. Adding a name here is the ONLY step needed to protect a service;
# removing it is the rollback. The auth portal itself must never appear here —
# that would be a redirect loop (asserted in roles/svc_media/tasks/access.yml).
sso_protected_services:
  - sonarr
  - radarr
  - prowlarr
  - sabnzbd
  - bazarr
  - lazylibrarian
  - jdownloader
  - code-server
  - webtop
  - syncthing
  - prometheus
```

Jinja added inside **both** `reverse_proxy` blocks in `Caddyfile.j2`:

```jinja
{% if name in sso_protected_services | default([]) %}
	# Authelia forward-auth. Unauthenticated requests get redirected to the
	# portal; the copy_headers line passes identity through to the backend
	# for apps that can consume it (none do yet — harmless, and it is what
	# makes a later native-OIDC phase a smaller change).
	forward_auth {{ hostvars[infra_host].ansible_host }}:{{ infra_secret_apps.authelia.ui_port }} {
		uri /api/authz/forward-auth
		copy_headers Remote-User Remote-Groups Remote-Email Remote-Name
	}
{% endif %}
```

Plus a `caddy_services` entry for the portal (`main.yml`), which must **not** be
in the protected list:

```yaml
auth:
  backend: "{{ hostvars[infra_host].ansible_host }}:9092"
  group: Infrastructure
  icon: authelia
```

### 4. Guards — the silent-failure mode is the dangerous one

A typo in `sso_protected_services` leaves a service **unprotected while looking
configured**. That fails open, so it gets a gate rather than a comment.

Extend `tests/validate_infra_catalog.py` (or a small sibling test) to assert:

- every name in `sso_protected_services` exists in `caddy_services` or in
  `download_apps` with `proxy: true`
- `auth` is not in `sso_protected_services` (redirect loop)
- the list has no duplicates

This runs offline in `make validate`, so it fails before any deploy.

### 5. Verification — make a broken SSO fail loudly

The existing Caddy smoke test (`roles/svc_media/tasks/verify.yml:111-132`)
fails only on `000` or `5xx`, so a `302` passes — meaning it would equally pass
if forward-auth were silently *not* applied. Split it:

- protected services must return a redirect/challenge, not `200`
- the `auth` portal must return `200`
- everything else keeps the current permissive check

One genuine unknown: Authelia returns `302` to browser-ish requests and `401`
to API-ish ones based on the `Accept` header, and the smoke test uses `curl`
without `-L`. Accept **either `302` or `401`** and confirm which actually
appears during the first deploy rather than guessing.

Note this makes Authelia a single point of failure for `make verify` — if it is
down, Caddy's `forward_auth` emits `502`, which the existing `^5[0-9]{2}$` rule
already fails on. That is the correct behaviour, but it is new coupling worth
knowing about.

## Files to change

| File | Change |
|---|---|
| `inventory/group_vars/all/infra-apps.yml` | `authelia` entry in `infra_secret_apps` |
| `inventory/group_vars/all/main.yml` | `sso_protected_services` list; `auth` in `caddy_services` |
| `inventory/group_vars/all_vault.yml.example` | 4 new documented vars |
| `roles/svc_infra/templates/authelia.yml.j2` | new — config (0644, no secrets) |
| `roles/svc_infra/templates/users.yml.j2` | new — users (0600, `no_log`) |
| `roles/svc_infra/tasks/files.yml` | 2 render tasks + restart-cascade clauses |
| `roles/svc_media/templates/Caddyfile.j2` | `forward_auth` block in both loops |
| `roles/svc_media/tasks/access.yml` | assert portal not in protected list |
| `roles/svc_media/tasks/verify.yml` | tighten the smoke test |
| `tests/validate_infra_catalog.py` | `sso_protected_services` gate |

## Ordering, and how to back out

This is remotely-accessible infrastructure and a broken `forward_auth` locks
out 11 services at once. **Two branches, not one** — deploy the identity
provider and prove it works before anything depends on it.

**Branch 1 — `feat/authelia`.** Deploy Authelia alone. Nothing else changes;
no vhost is protected yet. Confirm `https://auth.fortwow.dev` serves the portal
and the password logs in successfully. Then:

```bash
# The new backup_path creates its own artifact, and verify asserts each is
# fresh within 2 days (roles/svc_infra/tasks/verify.yml:7-40), so run the
# backup once before verifying or make verify fails on a missing archive.
systemctl --user -M homelab@ start backup-infra-appdata.service
```

**Branch 2 — `feat/sso-forward-auth`.** Add `sso_protected_services` and the
Caddyfile block. `make access` re-renders Caddy only, so the blast radius is
one config file on one host and the turnaround is seconds.

**Rollback:** empty `sso_protected_services`, run `make access`. Caddy is
validated at render time (`caddy validate`, `access.yml:126`), so a malformed
directive fails the deploy rather than taking the proxy down.

**Bypass while debugging:** every protected service stays reachable at its
direct `IP:port`, since only the Caddy vhost gains auth — the seven selected
svc-download apps on `.31`, plus `prometheus` (`.32:9091`), `syncthing` (`.32:8384`),
`code-server` (`.32:8443`) and `webtop` (`.32:3003`). Nothing in the protected
set publishes on loopback only, so there is no service that a broken
`forward_auth` can cut off entirely. (This is a direct consequence of excluding
`calibre-web`, which is loopback-only on svc-media — it was the one service
with no escape hatch.)

## Verification

1. `make validate` — offline gates, including the new `sso_protected_services` check.
2. `make infra` — Authelia up; `systemctl --user -M homelab@ is-active authelia`.
3. Portal loads at `https://auth.fortwow.dev`, login succeeds with the vault password.
4. `make access` — Caddy re-rendered; `caddy validate` passes.
5. Logged out (fresh private window): a protected service redirects to the portal;
   after login it loads. Confirm the session cookie carries across two different
   protected services without a second login.
6. Unprotected services still load with no prompt — check `home`, `jellyfin`,
   `ntfy`, `immich`.
7. **Mobile check:** the Immich and Nextcloud apps still sync (they were
   excluded, but this is the failure everyone regrets not testing).
8. Redeploy both roles — `changed=0`.
9. `make verify` green across all three VMs.
10. Confirm the arr apps still talk to each other: Prowlarr → Sonarr test,
    and a SABnzbd queue item, since those paths are the ones a proxy-auth
    change would plausibly break.

## Double logins: which services still prompt underneath

Forward-auth is an **outer gate**. It does not disable any application's own
login, so a service that has one will ask twice. Of the eleven, only two do:

| Service | After SSO | Why |
|---|---|---|
| The 7 SSO-protected download apps | **one login** | `AuthenticationRequired=DisabledForLocalAddresses`; Caddy proxies from a LAN address, so they never prompt |
| `prometheus` | **one login** | has no auth of its own |
| `syncthing` | **one login** | GUI is unauthenticated today |
| `code-server` | two logins | `PASSWORD` env |
| `webtop` | two logins | KasmVNC basic auth |

Neither `code-server` nor `webtop` supports header-based auth, so the second
prompt stays. That is arguably correct for both — they are effectively remote
shells, and a second factor in front of a shell is not a bad trade.

Do **not** try to remove their local passwords to smooth this out. Both publish
on svc-infra's LAN IP, so the local login is what protects them if Caddy is
bypassed or forward-auth fails open.

## Docs to update

- `docs/first-login-walkthrough.md` — Phase 4's "✅ no login needed on LAN" and
  Phase 6's "No login required" tables both become false; add an SSO login step
  ahead of the services it now fronts.
- `docs/automation-opportunities.md` — the "still live" warning for **Syncthing**
  is resolved by this; say so. The **Calibre-Web** `admin`/`admin123` warning
  stays exactly as it is — this change deliberately does not cover it, and it
  remains the most exposed default in the estate.
- `docs/services.md`, `docs/dns-pfsense-caddy.md` — the `curl --fail --head`
  examples that "expect HTTP/2 200" are wrong for protected names.

## Open questions to resolve at implementation time

- Pin the Authelia image digest from the registry (repo convention: never a tag).
- Confirm whether the forward-auth challenge is `302` or `401` under `curl`.
- Confirm Authelia's cookie is accepted across sibling subdomains as configured
  (`domain: fortwow.dev`) — this is what makes it *single* sign-on rather than
  eleven logins.
