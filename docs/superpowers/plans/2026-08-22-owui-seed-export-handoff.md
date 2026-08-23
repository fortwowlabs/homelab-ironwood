# Handoff: Open WebUI seeding + settings-as-code

**Written 2026-08-22 from TERRA (Windows GPU host).**
**Branch `feat/owui-seed-and-export`, 3 commits, pushed, tree clean, 3 ahead of `main`.**

```bash
git fetch origin && git switch feat/owui-seed-and-export
```

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

## THE IMPORTANT PART: none of it has touched the live instance

Both scripts need `OWUI_ADMIN_TOKEN`, which TERRA does not have. **Every API
call is unexercised.** They are built from Open WebUI's own source
(`backend/open_webui/models/models.py` for `ModelForm`,
`models/access_grants.py` for sharing) rather than guessed, but that is not the
same as working.

What *was* tested: argument handling, missing token, unreachable host,
malformed responses, redaction against planted secrets, and both gates'
positive controls (revert a rule → the self-check names it).

### First live run — do this before anything else

```bash
export OWUI_ADMIN_TOKEN='...'       # Settings -> Account -> API keys
make owui-personas ARGS=--dry-run   # EXPECT: both report "present", nothing to do
```

That is the cheap check that the API shape is right. Both personas already
exist (created by hand), so a correct tool reports `present` for each and exits
0 without writing. If it reports `MISSING`, the id matching is wrong — do not
let it create duplicates; investigate first.

```bash
make owui-export                    # writes inventory/group_vars/all/openwebui-config.yml
```

**Read the generated file before committing it.** Confirm no secret leaked past
the allowlist. Then commit it — the drift gate is inert until it exists.

---

## Gotchas that will cost you time

**The drift gate currently protects nothing and says so.** With no export it
prints `INCONCLUSIVE` and exits 0. That is deliberate: failing the build on a
fresh clone where nobody has a token would just get the gate disabled. It
starts protecting after the first `make owui-export` is committed.

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

**Two gate groups fail on Windows, pre-existing.** `shell` (a Windows temp path
used as a regex/exec path) and `secrets` (wants `.venv/bin/ansible-playbook`).
Confirmed by stashing and re-running on a clean tree. **If they fail on macOS,
that is a real finding, not this.**

**Heredocs mangle backslashes in this Git Bash.** Two files and one Makefile
edit were corrupted that way before I switched to `python -c` with `chr()`
codes. If you edit the `.PHONY` continuation, check it with
`sed -n '60,63p' Makefile | cat -A` — a literal `\n` instead of a newline is
the failure.

---

## To finish

```bash
make validate          # from a POSIX box
make infra             # expect changed=3, then
make infra             # must be changed=0
make verify
git switch main && git merge --ff-only feat/owui-seed-and-export
git push origin main && git branch -d feat/owui-seed-and-export
git push origin --delete feat/owui-seed-and-export
```

Note: **nothing in this branch is deployed by Ansible.** `personas.yml` and the
scripts are read only by the make targets and gates, so `make infra` should
report no change from them beyond the usual svc-infra archive sync. That is by
design — a task that POSTs every run would report `changed` every run and
destroy the `changed=0` proof.

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
