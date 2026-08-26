# Handoff: Open WebUI seeding + settings-as-code

**DONE 2026-08-26.** Merged to `main` as `5488391`, deployed, verified, CI green.
Kept as the record of what was built and of what running it live disproved.

| Step | Result |
|---|---|
| `make validate` | exit 0, including `systemd-analyze` and `gitleaks` |
| `make infra` ×1 | `changed=3` — archive build, unpack, record revision |
| `make infra` ×2 | **`changed=0`** |
| `make verify` | clean on all five hosts |
| CI on `main` | success, 1m24s |
| `make owui-personas --dry-run` | both personas `present`, exit 0 |
| Drift gate | `OK (2 enforced keys checked against 447 exported)` |

## Three bugs only a live run could find

This page originally led with "none of the API calls have run against the live
instance". That was worth flagging, because all three of these would have
shipped:

1. **`/api/v1/models/` — with the trailing slash — returns HTTP 200 and the
   SPA's HTML.** A wrong path here never 404s; it looks like a healthy server
   right up to the parse. The bare path returns JSON. The tool now reports
   "reached it, got HTML" separately from "could not reach it", because the old
   message named the wrong fault and sent the first diagnosis at the network.
2. **The listing is `{"data": [...]}`, not a bare list.** The shape guard
   refused rather than guessing, which was correct: an unrecognised envelope
   must not read as an empty estate, or the seeder would have created
   duplicates of two personas that already existed.
3. **The generated export failed the repo's own yamllint** — 15 indentation
   errors, because PyYAML writes sequences flush with their parent key. Caught
   by `make validate` on the first real export.

`make owui-personas --dry-run` reporting both personas `present` and exiting 0
is the idempotence check passing for real. The export was audited before being
committed: 122 shown values of 447 keys, no credential among them.

---

## What this branch does

Completes the model agreed on 2026-08-10 — **seeded from git → modified in the
UI → captured by the backup** — which until now only had the middle and last
parts.

| File | Purpose |
|---|---|
| `inventory/group_vars/all/personas.yml` | Persona catalog (`thera`, `unfiltered`) |
| `scripts/owui_personas.py` | Seeder. `make owui-personas` |
| `tests/validate_personas.py` | Gate, group `catalog` |
| `scripts/owui_config_export.py` | Live-config exporter. `make owui-export` |
| `tests/validate_openwebui_config_drift.py` | Drift gate, group `catalog` |
| `Makefile` | Two targets + `.PHONY` |
| `docs/chat-models.md` | Persona section rewritten |
| `docs/plans/openwebui-settings-as-code.md` | Status now "built, not yet run" |

Commits: `dd323e7` personas, `7953647` exporter+gate, `4734686` docs.

---

## How it was verified

Both scripts were built from Open WebUI's own source
(`backend/open_webui/models/models.py` for `ModelForm`,
`models/access_grants.py` for sharing) rather than guessed -- but that is not
the same as working, and the first three live runs proved it.

Offline: argument handling, missing token, unreachable host, malformed
responses, redaction against planted secrets, and both gates' positive controls
(revert a rule, the self-check names it).

Live, against chat.fortwow.dev with the vault's admin token: the persona
dry-run reports both `present` and exits 0, and the export is idempotent
(a second run reports `unchanged`).

## Gotchas that will cost you time

**The drift gate is live now, but only because an export is committed.** With
no export it prints `INCONCLUSIVE` and exits 0 rather than failing -- deliberate,
because failing the build on a fresh clone where nobody has a token would just
get the gate disabled. Delete the export file and it silently goes back to
protecting nothing.

**The seeder never updates.** Editing `personas.yml` does not change a persona
that already exists. Delete it in the UI and re-seed. This is the design — an
updating seeder would revert UI edits every run and rename `Thera` back to
`Therapist`.

**Only two keys are enforced by the drift gate** — `ui.enable_signup` and
`ui.default_user_role`. Everything else is meant to drift. Widen `ENFORCED` in
the gate if something else deserves it.

**Redaction hides drift for redacted keys.** Rotating a token produces no diff.
Accepted; this tracks settings, not secrets. If a key you care about shows as
`<redacted>`, add its prefix to `SAFE_PREFIXES` in the exporter — the drift
gate already fails loudly if an *enforced* key is redacted.

**Two gate groups fail under Git Bash on Windows** -- `shell` (a Windows temp
path used as a regex/exec path) and `secrets` (wants `.venv/bin/ansible-playbook`).
Confirmed pre-existing by stashing. Both **pass in WSL**, which is where
`make validate` should be run on this machine anyway. If they fail there or on
macOS, that is a real finding.

**Heredocs mangle backslashes in this Git Bash.** Two files and one Makefile
edit were corrupted that way before I switched to `python -c` with `chr()`
codes. If you edit the `.PHONY` continuation, check it with
`sed -n '60,63p' Makefile | cat -A` — a literal `\n` instead of a newline is
the failure.

---

## Done, for the record

```bash
# in WSL on TERRA -- Ansible cannot run on native Windows
cd ~/dev/homelab-ironwood
USE_VAULT_FILE=1 make validate
USE_VAULT_FILE=1 make infra      # changed=3, then changed=0
USE_VAULT_FILE=1 make verify
```

**Nothing here is deployed by Ansible.** `personas.yml` and the scripts are read
only by the make targets and gates, so the only `changed` on deploy was the
usual svc-infra archive sync. That is by design: a task that POSTs every run
would report `changed` every run and destroy the `changed=0` proof.

See `docs/deployment.md` for the WSL specifics, including the Git Bash quoting
trap that made one of these runs report the wrong branch.

---

## Still open, beyond this branch

1. **The importer.** `POST /api/v1/configs/import` is what would make a
   clean-clone rebuild real. Open WebUI is now restorable from backup and
   *observable* from git, but not reproducible from it.
2. **`feat/mac-control-node`** — 15 commits, roughly 5 of 13 tasks done.
   Deliberately untouched. Plan at
   `docs/superpowers/plans/2026-08-12-mac-control-node.md`.
3. **Image/video models** (MiniMax H3, Qwen Image Edit) — unblocked since
   in-chat generation started working 2026-08-20. VRAM is the hard part.
4. **Abliterated Muse Glimmer** — blocked on llama.cpp PR #26185, not an Ollama
   version bump.

Working notes with fuller reasoning are in `LLM-TODO-LIST.md` (gitignored, so
it is on TERRA only — copy it across if Codex needs it).

---

## Repo conventions these follow

Worth knowing before editing them, because they are load-bearing here:

- **Gates are discovered, not listed.** A `tests/validate_*.py` declares
  `GATE_GROUP = "catalog"` at line start and `tests/run_gates.py` finds it.
  Adding a gate is a new file, not a Makefile line. A gate with no group makes
  discovery refuse to run at all.
- **Every gate carries a self-check** — a case table proving each rule still
  fires. A gate against silent failure may not fail silently itself.
- **Three states, never two.** `OK` / `FAIL` / `INCONCLUSIVE`. "Could not look"
  must never render as an all-clear.
- **Exit codes**, matching `scripts/owui_image_config.py`: `0` fine, `1` could
  not look and nothing was written, `2` ambiguous or readback disagreed, `3`
  catalog invalid.
