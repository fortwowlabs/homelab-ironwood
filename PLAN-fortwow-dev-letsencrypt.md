# Real SSL certificates: fort.wow → fortwow.dev with Let's Encrypt

## Context

Devices (iPad running Readest, phones, TVs) currently need Caddy's internal root CA installed to avoid TLS warnings, because `fort.wow` is a private TLD no public CA can sign. The user owns `fortwow.dev` through Cloudflare, so we migrate the deployment to that domain and issue one real **wildcard cert `*.fortwow.dev`** via **Let's Encrypt DNS-01 through the Cloudflare API**. Nothing becomes publicly reachable — DNS-01 proves ownership with a TXT record, so no ports open to the internet. Access stays LAN + Tailscale (existing unmanaged subnet router).

Decisions confirmed with the user:
- **Flat naming** `<service>.fortwow.dev` (direct rename; one wildcard covers all 37 vhosts, and keeps service names out of public Certificate Transparency logs).
- **Split-horizon DNS**: local dnsmasq on svc-media stays authoritative; public Cloudflare zone stays empty.
- **Hard cutover**, no fort.wow compatibility period.
- **VM FQDNs move too** (`search_domain` already derives from `service_domain`). Verified in `roles/pve_vm/tasks/main.yml`: `searchdomains` is only set at VM create time (`when: not pve_vm_exists`) — existing VMs keep the old guest FQDN until recreated (accepted cosmetic drift, **no VM recreation risk**). Never `qm set --searchdomain` on live VMs (changes cloud-init instance-id).
- **Cert acquisition: certbot + stock EPEL Caddy** (chosen over a custom Caddy build after fragility discussion). `certbot` and `python3-certbot-dns-cloudflare` are both in EPEL, so every component is distro-packaged and dnf-updated. Certbot obtains/renews the wildcard; Caddy just points `tls` at the cert files. No custom binary, and certbot's Cloudflare plugin waits a fixed propagation delay instead of querying DNS — so the split-horizon ACME-propagation problem never arises. Renewal is certbot's systemd timer and is testable with `certbot renew --dry-run`.

Bonus: `.dev` is HSTS-preloaded (browsers force HTTPS), which real certs now satisfy.

## Architecture of the cert path

```
certbot (root, EPEL) --DNS-01 via Cloudflare API--> /etc/letsencrypt/live/fortwow.dev/
        └─ deploy hook: copy fullchain+privkey -> /etc/caddy/certs/ (root:caddy 0640) + reload caddy
certbot-renew.timer (EPEL unit) renews at <30 days, hook re-fires
Caddyfile: https://*.fortwow.dev { tls /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem ... }
```

Caddyfile restructure (37 site blocks → 1): a single `https://*.{{ service_domain }}` site block with `@name host …` matchers + `handle` blocks per service, preserving `bind`, `scheme`, `tls_insecure_skip_verify`, `header_up Host`; trailing `handle { abort }` for unknown names. No apex vhost (the wildcard doesn't cover the apex; nothing references it).

## Phase 0 — Prerequisites [MANUAL]

1. Cloudflare: confirm `fortwow.dev` zone active; leave it empty; **leave zone DNSSEC (DS records) disabled** — with the signed `.dev` parent, enabling it would make pfSense Unbound reject the unsigned split-horizon answers unless `domain-insecure` is added.
2. Create API token: "Edit zone DNS" template, Zone/DNS/Edit, scoped to `fortwow.dev` only.
3. `USE_VAULT_FILE=1 make vault-edit` → add `vault_cloudflare_api_token`.
4. `git switch -c feat/fortwow-dev-letsencrypt`.

## Phase 1 — Variables and catalogs

- `inventory/group_vars/all/main.yml`:
  - `:142` `service_domain: fortwow.dev`
  - Add `acme_email: brandon.strader@gmail.com` (Let's Encrypt account/expiry contact) near it.
  - Update stale "internal-CA TLS" comments at `:124`, `:131`; optionally the ssh-key comment `:70`.
- `inventory/host_vars/svc-media.yml` package list: add `certbot`, `python3-certbot-dns-cloudflare` (EPEL already enabled via `service_repo_packages`).
- `inventory/group_vars/all/minecraft.yml:51,62` — motd literals → fortwow.dev.
- `inventory/group_vars/all_vault.yml.example` — append `vault_cloudflare_api_token: "REPLACE_cloudflare_zone_dns_edit_token"` with a comment on token scoping (`REPLACE_` satisfies `tests/validate_secrets.py`).

## Phase 2 — Certbot + Caddy (`roles/svc_media/`)

**New `templates/cloudflare.ini.j2`** → `/etc/letsencrypt/cloudflare.ini`, root:root **0600**:
```
dns_cloudflare_api_token = {{ vault_cloudflare_api_token }}
```
Task must have **`no_log: true`, `diff: false`** (required by `tests/validate_secret_tasks.py`; pattern: `roles/service_vm/tasks/packages.yml:49-57`).

**New `templates/certbot-deploy-caddy.sh.j2`** → `/etc/letsencrypt/renewal-hooks/deploy/caddy.sh`, mode 0755:
copies `live/{{ service_domain }}/fullchain.pem` + `privkey.pem` → `/etc/caddy/certs/` (root:caddy, 0640, dir 0750), then `systemctl reload caddy`. Fires automatically on every renewal.

**`tasks/access.yml`** — inside the Caddy block (`:103`), ordered before the Caddyfile render:
1. Token ini template (no_log/diff:false as above).
2. Deploy-hook script template.
3. Obtain the wildcard (idempotent via `creates`):
   ```yaml
   - name: Obtain the Let's Encrypt wildcard certificate
     ansible.builtin.command:
       cmd: >-
         certbot certonly --non-interactive --agree-tos
         -m {{ acme_email }}
         --dns-cloudflare
         --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini
         --dns-cloudflare-propagation-seconds 30
         -d "*.{{ service_domain }}"
       creates: /etc/letsencrypt/live/{{ service_domain }}/fullchain.pem
     when: not ansible_check_mode
   ```
   (Cert name derives from the first `-d` with the wildcard label stripped → `live/fortwow.dev/`. Takes ~1 min on first run; a no-op forever after.)
4. `/etc/caddy/certs` directory (root:caddy 0750), then two `copy remote_src` tasks for fullchain/privkey (root:caddy 0640) — checksum-idempotent, `notify: restart caddy`. (Same effect as the hook; Ansible owns first install, the hook owns renewals.)
5. Enable `certbot-renew.timer` (`ansible.builtin.systemd: enabled: true, state: started`).
6. Caddyfile render stays as-is (`validate: "caddy validate …"` works — stock binary, no custom modules).
7. **Delete** `Wait for the internal CA root` + `Fetch the public internal CA certificate` (`:138-149`) — nothing replaces them; the certbot task is synchronous and the cert exists before Caddy (re)starts.

**`templates/Caddyfile.j2`** — rewrite: header comment updated; global block keeps `auto_https disable_redirects` + h1/h2 (SELinux QUIC note), **drops `local_certs`**; then:
```
https://*.{{ service_domain }} {
	bind {{ ansible_host }}
	tls /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem
	{% for name, svc in caddy_services | dictsort %}
	@{{ name }} host {{ name }}.{{ service_domain }}
	handle @{{ name }} { reverse_proxy … (same per-service options as today) }
	{% endfor %}
	{# same loop over download_apps entries with proxy: true #}
	handle { abort }
}
```
Keep the EPEL `caddy` package and existing unit untouched. Leave `/var/lib/caddy/.../pki/` internal-CA state on disk (inert once `local_certs` is gone).

## Phase 3 — Verification code + offline gates

- `roles/svc_media/tasks/verify.yml:113` — **drop `-k`** from the smoke-test curl: all 37 checks now also prove chain trust against the OS store.
- Add gates after it (same `media_access_layer == 'caddy'` guard):
  - Issuer/SAN: `openssl s_client -servername home.{{ service_domain }} … | openssl x509 -noout -issuer -ext subjectAltName`; `failed_when` missing `Let's Encrypt` or `*.{{ service_domain }}`.
  - Renewal wiring: assert `certbot-renew.timer` is active/enabled (`systemctl is-enabled`), `changed_when: false`.
- `tests/validate_generated_catalog.py` — extend fixtures to render `Caddyfile.j2` and `dnsmasq-services.conf.j2` offline: one matcher+handle per catalog key (caddy_services + proxy-enabled download_apps), `local_certs` absent, `tls /etc/caddy/certs/` present, one `address=/…/` line per name. Makes `make validate` catch template regressions before any VM is touched. Also fix the stale header comment in `dnsmasq-services.conf.j2:2-3`.

## Phase 4 — Docs

- `docs/dns-pfsense-caddy.md` — biggest rewrite: override target `fortwow.dev`; delete install-the-CA instructions; flip the DNSSEC section (old private-TLD `domain-insecure` workaround obsolete; new note: keep zone DNSSEC off or add `domain-insecure: "fortwow.dev"`); update Tailscale split-DNS section; document the certbot/hook/timer renewal path.
- `docs/first-login-walkthrough.md` (~35 hits, drop CA-install step), `docs/services.md` (certs publicly trusted, no client setup), `docs/deployment.md` (CA-install refs), `docs/operations.md:259-281` (domain-change runbook), `README.md:5`, `docs/automation-opportunities.md`, `docs/plans/authelia-sso.md`.
- Note: Mealie admin login stays `admin@fort.wow` (seed email only applies to an empty user table).

## Phase 5 — Deploy (per CLAUDE.md workflow)

1. `make validate` — offline gates, no VMs touched.
2. `USE_VAULT_FILE=1 make media` — dnsmasq re-zoned (dig gate queries dnsmasq directly, passes), certbot installed, token ini, wildcard issued (~1 min), certs copied, wildcard Caddyfile, Caddy restarted. **fort.wow names die here**; clients can't resolve fortwow.dev until step 4.
3. `USE_VAULT_FILE=1 make deploy` — refreshes every var-driven consumer: Homepage ALLOWED_HOSTS, Nextcloud OVERWRITE*/trusted domains, Paperless URL, Grafana root_url, Vaultwarden DOMAIN, Mealie BASE_URL, Beszel APP_URL, Cockpit Origins, SABnzbd host_whitelist (via API), Minecraft motd.
4. **[MANUAL] pfSense**: DNS Resolver → Domain Overrides: add `fortwow.dev` → 192.168.1.30; remove any `fort.wow` override and `domain-insecure: "fort.wow"` custom option. (First check from a LAN client whether the old override actually existed — docs suggest it may never have been configured; replicate whatever resolution path was real.)
5. **[MANUAL] Tailscale admin**: DNS → Split DNS: `fort.wow` → `fortwow.dev`, nameserver stays 192.168.1.30.

## Phase 6 — Verify (application-level, per CLAUDE.md)

1. `USE_VAULT_FILE=1 make verify` (now `-k`-free + issuer + timer gates).
2. Workstation: `dig @192.168.1.30 jellyfin.fortwow.dev +short` → 192.168.1.30; `dig jellyfin.fortwow.dev +short` (proves pfSense path); `curl -sI https://home.fortwow.dev` + 2-3 more **without `-k`/`--resolve`**; `openssl s_client` shows issuer Let's Encrypt (R1x/E1x), SAN `*.fortwow.dev`.
3. On svc-media: `certbot renew --dry-run --run-deploy-hooks` — proves the whole renewal path (token, plugin, LE staging API, certificate copy, and Caddy reload) end to end; then confirm the deploy hook exists and `systemctl list-timers certbot-renew*` shows the next run.
4. **[MANUAL] The real test**: iPad with no custom CA → `https://home.fortwow.dev` → padlock, no warning; open Readest's service; repeat over Tailscale off-LAN.
5. Cloudflare zone empty again (ACME TXT records are cleaned up post-issuance).

## Phase 7 — Cleanup and git

1. `rm fort.wow-root-ca.crt` (repo root, gitignored).
2. [MANUAL, optional] Remove old "Caddy Local Authority" root from device trust stores.
3. Commit (explicit paths, never `git add -A`) → `git status --porcelain` clean → final `USE_VAULT_FILE=1 make deploy` reports **`changed=0`** → `make verify` → ff-merge to main → push → delete branch.

## Residual risks / non-actions

- Existing VMs keep `fort.wow` guest search-domain until recreated (cosmetic; never touch live cloud-init config).
- LE rate limits: non-issue for one wildcard; `--dry-run` uses the staging environment and doesn't count.
- The deploy hook and the Ansible cert-copy tasks intentionally duplicate the copy logic (Ansible = first install + drift repair, hook = unattended renewals); keep them byte-compatible (same dest, owner, mode).
- If svc-media were ever rebuilt, everything reinstalls from EPEL + one certbot run — no dependence on third-party build services.

## Critical files

- `roles/svc_media/templates/Caddyfile.j2` (rewrite: wildcard site + matchers, file-based tls)
- `roles/svc_media/tasks/access.yml` (certbot install/issue, cert copies, timer, delete CA wait/fetch)
- `roles/svc_media/tasks/verify.yml` (drop `-k`, issuer + timer gates)
- `inventory/group_vars/all/main.yml` (`service_domain`, `acme_email`)
- `inventory/host_vars/svc-media.yml` (certbot packages)
- `inventory/group_vars/all_vault.yml.example` (token schema)
- `tests/validate_generated_catalog.py` (offline template rendering)
- New: `roles/svc_media/templates/cloudflare.ini.j2`, `certbot-deploy-caddy.sh.j2`
- Docs: `docs/dns-pfsense-caddy.md` and 6 others
