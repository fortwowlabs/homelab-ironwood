# Open WebUI settings as code

**Status: built 2026-08-22, not yet run against the live instance.** Wanted
since 2026-08-10, when `ENABLE_PERSISTENT_CONFIG` was flipped to `true` and
this repo stopped being the authority on Open WebUI's configuration.

## What exists now

| Piece | Where |
|---|---|
| Exporter | `scripts/owui_config_export.py`, `make owui-export` |
| Drift gate | `tests/validate_openwebui_config_drift.py`, in `make validate` |
| Persona seeder | `scripts/owui_personas.py`, `make owui-personas` |
| Persona catalog | `inventory/group_vars/all/personas.yml` |

```bash
export OWUI_ADMIN_TOKEN='...'   # Settings -> Account -> API keys
make owui-export                # writes inventory/group_vars/all/openwebui-config.yml
```

**The one step left is running that export once.** Until it has been, the drift
gate reports `INCONCLUSIVE` and protects nothing — it says so in its own output
rather than printing a reassuring OK. It deliberately does not fail the build
in that state: on a fresh clone nobody has an admin token yet, and a gate that
blocks all work until someone finds one would simply be disabled. The state
that fails is an export that exists and disagrees with the repo.

**Only two keys are enforced**, and that narrowing is deliberate rather than a
first cut. Most of this config is *meant* to drift — that is the point of
seed-then-modify. `ui.enable_signup` and `ui.default_user_role` are different
in kind, because `chat.fortwow.dev` is publicly reachable and deliberately not
behind Authelia, so Open WebUI's own login is the whole front door. Widen
`ENFORCED` in the gate if another setting turns out to deserve the same
treatment.

**Redaction is by allowlist.** Values are written only for keys under known-safe
prefixes; everything else is recorded by name with the value replaced. The limit
is real: drift in a redacted key is invisible, so rotating a token produces no
diff. Accepted — this tracks settings, not secrets.

**Still not built: the importer.** `POST /api/v1/configs/import` is what would
make a clean-clone rebuild real. Until then the honest statement is that this
service is restorable from backup and now *observable* from git, but not
reproducible from it.

The original design follows.

The agreed model for this app is **seeded from git → modified in the UI →
captured by the backup**. Backup alone is not enough: a `webui.db` inside a
nightly tarball is not reviewable, not diffable, and does not rebuild from a
clean clone the way everything else here does. This page is about closing that
last gap.

## What changed, and why it matters

Before 2026-08-10 the environment was authoritative on every container start,
so `inventory/group_vars/all/infra-apps.yml` fully described the running
config. That is no longer true. Resolution now works like this
(`backend/open_webui/models/config.py`):

```python
if not Config.persistent_enabled_for(key):
    return Config.default_value(key, default)   # the environment value
row = await db.get(Config, key)
return row.value if row else Config.default_value(key, default)
```

- A key with **no DB row** still takes the environment value.
- A key **with a DB row** ignores the environment permanently.
- A row appears the moment that setting is touched in the admin UI.

**The failure this creates is silent.** Edit a value in `infra-apps.yml`, run
`make infra`, and nothing happens for any key that has ever been changed in the
UI. There is no error. `changed=0` still reports success, because the Quadlet
genuinely is up to date — the drift is inside the container's database, which
the deploy never looks at.

That is a hole in the guarantee `CLAUDE.md` is built around: *a deploy that
reports `changed=0` against a clean tree is proof that what is running equals
what is committed.* For this one service, it no longer is.

## What to build

### 1. An exporter (`make owui-export`)

`GET /api/v1/configs/export` with an admin token returns the whole config as
JSON. Write it into the repo sorted and normalised, so drift appears in
`git diff` rather than nowhere:

```
inventory/group_vars/all/openwebui-config.yml
```

Three constraints, each load-bearing:

- **Strip secrets before writing.** The export includes API keys and the OAuth
  client secret. It cannot be committed raw. Whatever is stripped must be
  stripped by an allowlist of keys to keep, not a denylist of keys to drop —
  a denylist silently leaks any secret upstream adds later.
- **It must be a `make` target, never a task in `roles/svc_infra`.** A file
  that changes on every run would make every `make infra` report `changed` and
  destroy the `changed=0` proof — the same rule the metrics writes follow, and
  for the same reason.
- **Sort deterministically.** An export whose key order wobbles produces diff
  noise that trains you to ignore it.

### 2. A validator (`tests/validate_openwebui_config_drift.py`)

The exporter alone only *records* drift. This is the part that makes it fail
loudly: compare the exported file against what `infra-apps.yml` declares, and
fail when a repo-declared key has been overridden in the DB.

Wire it into `validate-catalog`. It must distinguish three states, not two —
"matches", "overridden in the DB", and "could not read the export" — because a
missing or stale export file must not read as a clean result. That is the same
tri-state reasoning as the scan probes in `CLAUDE.md`.

The export is a local artifact of a live system, so the validator has to treat
a **missing** file as inconclusive rather than passing.

### 3. An importer, eventually

An export is half a loop. `POST /api/v1/configs/import` is what makes a
clean-clone rebuild real. Until that exists, the honest statement is that this
service is restorable from backup but not reproducible from git.

## Decide at the same time

**Which keys are allowed to drift at all.** Most should — that is the point of
the change. But `ENABLE_SIGNUP` and `DEFAULT_USER_ROLE` are different in kind:
drift there is a security change, not a preference. `chat.fortwow.dev` is
publicly reachable and deliberately not behind Authelia, so its own login is
the entire front door.

The option worth considering is a small allowlist of keys the deploy actively
re-asserts — deleting their DB rows on every run so the environment wins again
— while everything else drifts freely. That keeps the useful half of
seed-then-drift without letting the front door drift.

## Before relying on any of this

**Nobody has looked at what is already in the `config` table.** It may hold
rows written before persistence was disabled, and those took effect the moment
it was re-enabled. Dump it and read it:

```bash
curl -s -H "Authorization: Bearer $OWUI_TOKEN" \
  https://chat.fortwow.dev/api/v1/configs/export | python -m json.tool
```

Confirm `ui.enable_signup` is not `true`.

## Related

Persona seeding is the other half of "seeded from git". Personas
(Workspace → Models) live in their own DB tables rather than the config blob,
so they are unaffected by `ENABLE_PERSISTENT_CONFIG` and always survived
restarts. Seeding them means a create-if-absent Ansible task against
`/api/v1/models`, which never updates an existing model — so UI edits survive
and repeated deploys still report `changed=0`. Not built either; see
[chat-models.md](../chat-models.md) for the persona text that would be seeded.
