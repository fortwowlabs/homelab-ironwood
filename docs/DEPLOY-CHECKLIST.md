# Deployment checklist (v6)

Ordered runbook for rolling out the v6 features (service DNS names, monitoring,
media acquisition) onto the running homelab, and for a fresh install later.
Check items off as you go. Details for each are in the linked docs.

The three v6 features live on stacked branches (`feat/service-dns` →
`feat/monitoring` → `feat/media-acquisition`). Deploy in that order so DNS
exists before the things that are reached by name.

---

## 0. Before anything

- [ ] **Set the new vault secrets** — `make vault-edit` and fill the `vault_romm_*`
      block (DB passwords + `openssl rand -hex 32` auth key; metadata keys
      optional). See [media.md](media.md). Template: `all_vault.yml.example`.
- [ ] (Optional) confirm `vault_ntfy_token` — leave empty for anonymous LAN ntfy.

## 1. feat/service-dns — names for everything

- [ ] Merge/checkout `feat/service-dns`, then `make check USE_VAULT_FILE=1`
      (dry-run), then `make media USE_VAULT_FILE=1`.
- [ ] **pfSense**: Services ▸ DNS Resolver ▸ Domain Overrides →
      `fort.wow` = `192.168.1.30`.
- [ ] **Tailscale** admin ▸ DNS ▸ Nameservers → add `192.168.1.30`,
      *Restrict to domain* `fort.wow` (remote access via the subnet router).
- [ ] **Install the Caddy root CA** on your devices — `make media` fetches it to
      `./fort.wow-root-ca.crt`. Steps per-OS in [dns-and-names.md](dns-and-names.md).
- [ ] Verify: `dig jellyfin.fort.wow` → 192.168.1.30; browse
      `https://jellyfin.fort.wow` with a trusted lock.

## 2. feat/monitoring — Cockpit + Homepage + alerting

- [ ] Merge/checkout `feat/monitoring`, `make deploy USE_VAULT_FILE=1`.
- [ ] Verify `https://home.fort.wow` (dashboard), `https://cockpit-media.fort.wow`
      and `https://cockpit-dl.fort.wow` (log in with your system account),
      `https://ntfy.fort.wow`.
- [ ] **Subscribe your phone** to topic `homelab-deploy` in the ntfy app pointed at
      `http://192.168.1.30:8080`.
- [ ] **Install the PVE disk guard on thurgadin** (manual — the box that filled on
      2026-07-20). Command block in [monitoring.md](monitoring.md#disk-alarms).
- [ ] Test an alarm: `sudo systemctl start homelab-diskguard.service` on thurgadin
      and confirm a phone ping (temporarily lower `THRESH` if pools are healthy).

## 3. feat/media-acquisition — books/audiobooks + ROM metadata

- [ ] Merge/checkout `feat/media-acquisition`, `make deploy USE_VAULT_FILE=1`.
- [ ] **RomM DB adoption** (one-time): either set `vault_romm_db_password` to the
      value currently in `/opt/homelab/appdata/romm/romm-db.env`, **or** reset the
      no-data mysql dir. See [media.md](media.md#adopting-the-existing-romm-db).
- [ ] Verify `make verify USE_VAULT_FILE=1` stays green **with LazyLibrarian now in
      the leak-canary set**; browse `https://lazylibrarian.fort.wow`.
- [ ] **UI wiring** (like the original Phase 7):
  - [ ] Prowlarr ▸ Apps ▸ add LazyLibrarian; add book/ebook indexers.
  - [ ] LazyLibrarian ▸ add SABnzbd downloader; set dirs `/data/{books,ebooks,audiobooks}`.
  - [ ] Audiobookshelf ▸ add libraries on `/audiobooks` and `/books`/`/ebooks`.
  - [ ] RomM ▸ drop ROM files into `/srv/media/romm/roms/<platform>/`.

## Still-manual (carried over, not v6-specific)

- [ ] Original app wiring if not done: SAB Usenet provider; Prowlarr indexers →
      Sonarr/Radarr; Jellyfin libraries + admin account; Seerr ↔ Jellyfin.
- [ ] Nightly verify timer from `contrib/` on an always-on box (README).
- [ ] Pin container images by digest after burn-in (README image policy).

---

## Fresh install (from zero) — where v6 slots in

Follow the base install in the top-level [README](../README.md) (control-node deps,
PVE token + scoped ACLs, vault, deploy). v6 changes only these:

- The **vault** now also needs the `vault_romm_*` block (§0).
- `make deploy` now also stands up dnsmasq, Caddy hostname routing, Cockpit,
  Homepage, ntfy, LazyLibrarian, and the disk alarms — no extra Ansible steps.
- The **three DNS glue steps** (§1) and the **PVE disk guard** (§2) are the only
  new manual actions. Everything else is documented per-feature in `docs/`.
