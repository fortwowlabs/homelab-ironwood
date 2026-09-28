# Household guide — design

**Status: designed, 2026-09-28. Stage 1 not yet implemented.**

A guide for the two people who live with this homelab — Brandon and his
fiancée — describing how to *use* it, as opposed to the existing docs, which
describe how to build and operate it. It is written for a reader who has
never heard of a container, an indexer or SSO, and who is standing in front
of a TV or holding a phone when she reads it.

The work is staged. Stage 1 (watch, read, listen) is specified in full here.
Later stages are catalogued so the whole estate is accounted for, and each
gets its own brainstorm and spec when it is picked up.

## Why this needs more than writing

A read-only audit on 2026-09-27 found that none of Stage 1's player apps had
ever been set up:

| Probe | Result |
|---|---|
| `jellyfin …/System/Info/Public` | `"StartupWizardCompleted": false` |
| `seerr …/api/v1/settings/public` | `"initialized": false` |
| `abs …/status` | `"isInit": false` |
| `/srv/media/*` on svc-media | 2 movies, 2 shows, 5 books (Calibre library), 0 audiobooks, `ebooks/` empty |

A guide cannot be verified against apps that do not work, and this repo's rule
is that verification means using the thing. So Stage 1 includes finishing the
admin setup, not only writing pages.

The same audit found three documentation errors, fixed as part of Stage 1:

- `first-login-walkthrough.md` §3.1 gives Jellyfin libraries as host paths
  (`/srv/media/movies`); inside the container they are `/media/movies` and
  `/media/tv`.
- §3.3 gives the Audiobookshelf library as `/srv/media/audiobooks`; inside the
  container it is `/audiobooks`.
- `services.md` tells the operator to add an Audiobookshelf `/books` library.
  The Audiobookshelf Quadlet does not mount one — it mounts the empty
  `/srv/media/ebooks` at `/ebooks`, while the actual Calibre library is
  `/srv/media/books`.

## The shape the estate already has

The household-facing paths are already separate from the admin ones, which is
what makes a household guide possible without changing access control:

- **Behind SSO, `admins` only:** Sonarr, Radarr, Prowlarr, SABnzbd, Bazarr,
  LazyLibrarian, jDownloader and the other admin tools. The family accounts
  authenticate and then get a 403, deliberately (`first-login-walkthrough.md`
  §0.3).
- **Outside SSO:** Seerr (request movies/TV), Shelfmark (find books and
  audiobooks, `AUTH_METHOD: none`), Jellyfin, Audiobookshelf, Calibre-Web and
  Homepage. These carry their own accounts or none.

So "download" for the household means *request* — Seerr for video, Shelfmark
for books — and the admin pipeline behind them does the rest.

## Decisions

| Question | Decision |
|---|---|
| Setup scope | Stage 1 finishes the admin setup, then writes and verifies the guide |
| Devices covered | iPhone/iPad, Android phone/tablet, Google TV Streamer (Jellyfin for Android TV). No e-reader |
| Away from home | Yes, via Tailscale on her devices; the existing split DNS serves the names |
| Where the guide lives | Markdown in `docs/household/`, rendered at deploy and served at `https://guide.fortwow.dev`, linked from Homepage |
| Site build | Pinned pure-Python `markdown` on the controller; one renderer script used by both deploy and validate |
| Ebook reading | Both, in the guide: Audiobookshelf as default, Readest + Calibre-Web as the "if you read a lot" option |
| Request approval | Seerr auto-approves for both accounts |
| Accounts | One per person in Jellyfin, Seerr (imported from Jellyfin), Audiobookshelf and Calibre-Web. Shelfmark has none |

### Ebook reading — why both, and what each costs

Progress sync only works within one family of apps; no option syncs across
families. So the choice is which service is the hub.

- **Audiobookshelf (default).** One app per phone, one login, one progress
  state for books *and* audiobooks. Android uses the official app. iPhone uses
  **Still** (third-party; EPUB/PDF/CBZ, progress sync, audio↔ebook handoff),
  because the official iOS app is still a TestFlight beta. Cost:
  Audiobookshelf's own docs say its ebook support "still needs work" — some
  EPUBs render poorly and typography settings can be ignored.
- **Readest + Calibre-Web (alternative).** A better reader on both platforms,
  browsing the Calibre library over OPDS. Calibre-Web Automated ships a
  KOReader-protocol sync server. **Unverified:** whether Readest can sync
  through that server rather than through its own account-based cloud. Stage 1
  measures this and the guide states what was observed.

Rejected: Libby (reads public-library loans only; cannot import files),
Google Play Books and Send to Kindle (good sync, but the library leaves the
house and every book is a manual upload), Apple Books (no Android), Calibre-Web
in a browser as the primary path (kept only as a fallback).

**Fallback.** If EPUB rendering in Still or the Audiobookshelf app is poor in
real use, Readest becomes the default for reading. Audiobooks stay on
Audiobookshelf either way.

## Stage 1 — watch, read, listen

### Admin setup (never seen by the household)

Order follows the dependencies.

0. **Readiness audit, read-only.** Record Prowlarr indexers, Sonarr/Radarr
   download client and root folders, SABnzbd categories, Shelfmark's
   Prowlarr/SABnzbd wiring, and whether Calibre-Web's ingest folder
   (`/srv/media/book-ingest`) actually imports. Everything after starts from
   facts, not from the July walkthrough.
1. **Jellyfin.** Wizard; admin account; libraries Movies `/media/movies` and
   Shows `/media/tv`; one user each.
2. **Prowlarr / Sonarr / Radarr.** Indexers pushed to both apps via
   *Settings > Apps*; root folders and the SABnzbd client confirmed.
3. **Seerr.** Sign in with Jellyfin; connect Sonarr and Radarr; import both
   Jellyfin users; auto-approve for both.
4. **Audiobookshelf.** Root user; libraries Audiobooks `/audiobooks` and Ebooks
   `/books` (after the repo change below); one user each; scheduled backup to
   `/config/backups`.
5. **Calibre-Web.** One account each — Readest needs them for OPDS and sync.
6. **Shelfmark.** Prowlarr and SABnzbd wired per `services.md`. Prove one ebook
   travels Shelfmark → `/srv/media/book-ingest` → Calibre library → visible in
   Audiobookshelf, and one audiobook travels Shelfmark →
   `/srv/media/audiobooks` → Audiobookshelf.
7. **Tailscale.** Her phone(s) join the tailnet; `jellyfin.fortwow.dev` and
   `abs.fortwow.dev` resolve and load over cellular.
8. **Readest spike.** Point Readest at Calibre-Web's OPDS feed and its sync
   endpoint; record whether progress moves between two devices through *our*
   server.

UI configuration is done by hand, as `services.md` intends ("Ansible
intentionally does not automate application wizards"). Where the implementer
can reach a web UI it does the setup; otherwise the plan gives the operator a
checklist with exact values.

### Repo changes (one branch, normal workflow)

- **Audiobookshelf mount.** `roles/svc_media/templates/audiobookshelf.container.j2`
  gains `Volume=/srv/media/books:/books:ro`. Read-only: Audiobookshelf's
  metadata lives in its own appdata, and Calibre owns that tree. The existing
  `/ebooks` mount is left alone. Deployed with `make media`.
- **Guide site.**
  - `docs/household/*.md` and `docs/household/img/` — the source.
  - `scripts/render_household_guide.py` — the one renderer: Markdown to HTML,
    one inline mobile-first stylesheet (light/dark), a top bar linking to
    Start here. Output is byte-deterministic so an unchanged guide deploys as
    `changed=0`.
  - `markdown` pinned in `requirements.txt` (controller runtime).
  - `guide` entry in `infra_apps` — a second static-web-server, modelled on
    `scan-reports`, serving `/opt/homelab/appdata/guide` read-only, directory
    listing off, `backup_paths: []` (every file is regenerated from git).
  - A render task in `roles/svc_infra` (runs on the controller, copies the
    output to svc-infra). This is not the metrics-file trap in `CLAUDE.md`:
    the content changes only when a commit changes it.
  - `guide` vhost in `caddy_services`, deliberately **not** in
    `sso_protected_services` — the family accounts cannot pass the admins-only
    rule, and the guide holds no secrets. Listed with the other deliberately
    open services and its reason.
  - A Homepage tile for the guide.
- **Gate.** `tests/validate_household_guide.py`, wired into `make validate`:
  every page renders through the same script; every relative link and image
  resolves; no page contains a vault variable name, a `password:`/`token:`
  style assignment, or anything matching the repo's existing secret patterns.
- **Doc fixes** listed under "Why this needs more than writing", plus
  `first-login-walkthrough.md` and `services.md` updated to reflect the
  household accounts and the new `/books` library.

### Pages

Source in `docs/household/`, one task per page.

| Page | Covers |
|---|---|
| Start here (`index.md`) | What the home server does for you, one line per thing, with links. Bookmark this site. |
| Getting connected | At home it just works. Away: turn Tailscale on — setup for iPhone and Android. Which accounts you have (usernames only). |
| Watch movies and TV | Jellyfin on the Google TV Streamer, iPhone, Android: sign in, find, resume, subtitles, download for offline. |
| Ask for a movie or show | Seerr: search, request, what each status means, roughly how long, where it appears. |
| Find a book or audiobook | Shelfmark: search, choose ebook or audiobook, what happens next and when to look. |
| Read a book | Default: Still (iPhone) / Audiobookshelf (Android). Then "If you read a lot: Readest", with setup and an honest note on its sync. |
| Listen to an audiobook | Audiobookshelf apps: play, speed, sleep timer, download for offline. |
| Something's not working | Five symptoms, each with a first check: nothing loads (Wi-Fi or Tailscale?), request stuck, book never appeared, video buffers, when to ask Brandon. |

**Writing rules.**

- Organized by task, never by app ("watch a movie", not "Jellyfin overview").
- Numbered steps; each device in its own labelled section.
- No infrastructure vocabulary: no container, NFS, indexer, SSO, VM, VPN jail.
  App names appear only where the reader taps them.
- **No passwords, ever.** Usernames only. Passwords belong in Vaultwarden,
  which is Stage 2; until then they are handed over in person.
- Screenshots only where a step is visually ambiguous (the Tailscale toggle,
  Seerr's request button, ebook vs audiobook in Shelfmark), taken during
  verification. The text must work without them.
- Every page ends with `Last checked <date> on <devices>`, and that line is
  true.

### Done means

Using her account, not an admin one:

1. **Movie:** requested in Seerr → appears in Jellyfin → plays on the Google TV
   Streamer, iPhone and Android; resuming on a second device picks up the
   position.
2. **TV:** one season requested → episodes appear and play the same way.
3. **Ebook:** found in Shelfmark → appears in Calibre-Web and in
   Audiobookshelf → opens in Still (iPhone) and the Audiobookshelf app
   (Android); reading on one moves the position on the other. Opens in Readest
   from the OPDS feed; Readest's sync behaviour recorded as observed.
4. **Audiobook:** found in Shelfmark → appears in Audiobookshelf → plays on
   both phones; a downloaded copy plays in airplane mode; progress syncs.
5. **Away from home:** on cellular with Tailscale on, one video and one
   audiobook play.
6. **Cold read:** she follows at least "Ask for a movie or show" and "Listen to
   an audiobook" on her own phone with the guide as her only help. Every
   hesitation is a defect and the page is fixed.
7. Every page carries an accurate `Last checked` line.

Repo gates: `make validate`; commit; clean tree; `make deploy-proof
TARGET=media` and `TARGET=infra` (the infra sync trio on the first run, then a
fully clean second run); `make verify`; merge, push, delete the branch.

Steps needing a human — app-store installs, device checks, the cold read — are
marked 🧑 in the plan. A step that cannot be made to work takes its stated
fallback, and the guide documents what works, never what should.

### Out of scope for Stage 1

- **LazyLibrarian** — background author/series monitoring; admin-only, and
  nothing the household does depends on it.
- **Bazarr** — subtitles happen in the background; nothing to document.
- **Music** — no music library exists under `/srv/media`.
- **Access-control changes** — none needed; every Stage 1 app is already
  outside SSO.

## Later stages — the rest of the estate

Every web service in `caddy_services` and `download_apps` is assigned below,
as are the two Minecraft servers. Each later stage repeats Stage 1's
pattern: readiness audit → admin setup → accounts for both → pages → verify on
real devices. No page is published before someone has followed it and it
worked.

| Stage | Theme | Services | Why here |
|---|---|---|---|
| 1 | Watch, read, listen | Jellyfin, Seerr, Shelfmark, Audiobookshelf, Calibre-Web, Homepage (+ Tailscale, not an estate service) | This spec |
| 2 | Everyday essentials | Vaultwarden (shared passwords — and where this guide's passwords finally live), Immich (phone photo backup, shared albums) | Highest daily value; photos are irreplaceable, so backup comes early |
| 3 | Running the house | Mealie (recipes, meal plans, shopping lists), Paperless-ngx (scan and search household paperwork), Nextcloud (files, shared folders) | Jointly useful; nothing breaks without them |
| 4 | Smart home | Home Assistant (companion app, dashboards) | Depends on which devices are actually owned; scope it then |
| 5 | Fun and extras | Open WebUI (AI chat and images — only while TERRA is on), RomM (retro games), Minecraft ×2, Bambuddy (3D printer) | Optional; some depend on the desktop being on |

**Admin-only — deliberately not in the household guide.** Sonarr, Radarr,
Prowlarr, SABnzbd, Bazarr, LazyLibrarian, jDownloader, SearXNG, Syncthing,
ComfyUI (direct), Grafana, Prometheus, Beszel, Uptime Kuma, Glances, NetBox,
Semaphore, code-server, Webtop, IT Tools, scan, ntfy, the three Cockpit vhosts
and Authelia. They remain covered by the existing admin docs. Listed so the
catalogue reads as complete rather than accidentally short: 15 household-facing
entries plus 26 admin-only entries account for all 41 web services (32 in
`caddy_services`, 9 in `download_apps`, counted 2026-09-28). The two Minecraft
servers are not web services and are counted separately, in Stage 5.

If a service is added to the estate later, it is assigned to a stage or to the
admin list in this table as part of adding it.
