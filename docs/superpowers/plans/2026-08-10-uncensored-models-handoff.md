# Handoff: finishing the uncensored model roster on a POSIX machine

**Written 2026-08-10 from TERRA (the Windows GPU host).**
**Branch: `feat/uncensored-models`, pushed to `origin`, tree clean.**

Everything that could be done on TERRA is done. What remains needs a machine
that can run Ansible, which TERRA cannot — see [Why TERRA
stopped](#why-terra-could-not-finish). Pick this up with:

```bash
git fetch origin && git switch feat/uncensored-models
```

---

## Read this first: one deploy step can reopen public registration

`ENABLE_PERSISTENT_CONFIG` flips from `false` to `true` in this branch. That
changes Open WebUI from "the environment is authoritative every start" to "a
DB row wins over the environment". **Any rows already in that `config` table
take effect the moment this deploys**, and nobody has looked at what is in
there — it may hold values written before persistence was disabled.

If a stale `ui.enable_signup: true` is sitting in that table, this deploy turns
public signup back on for an internet-reachable service whose own login is its
entire front door.

**Check before deploying, not after.** Create an admin API key in Open WebUI
(Settings → Account → API keys), then:

```bash
export OWUI_TOKEN='...'
curl -s -H "Authorization: Bearer $OWUI_TOKEN" \
  https://chat.fortwow.dev/api/v1/configs/export | python3 -m json.tool > /tmp/owui-config-before.json
grep -i -E 'signup|user_role|role' /tmp/owui-config-before.json
```

Keep that file — it is the only "before" snapshot that will ever exist.

- `ui.enable_signup` **must not be `true`**. If it is, decide deliberately
  before deploying: either clear it in the admin UI first, or accept it.
- If the endpoint 404s on this version, the fallback is reading the table on
  svc-infra directly:
  `podman exec open-webui python -c "import sqlite3;print(sqlite3.connect('/app/backend/data/webui.db').execute('select key,value from config').fetchall())"`

---

## What is already true (done on TERRA, no action needed)

**Eight models installed**, 111.7 GB, all verified working:

| Model | Resident | Notes |
|---|---|---|
| `huihui_ai/gemma-4-abliterated:26b` | 17 GB | **Default.** 100% GPU |
| `huihui_ai/Qwen3.6-abliterated:27b` | 18 GB | 100% GPU |
| `davidau-fable-fusion:27b-q4km` | 19 GB | 100% GPU. Locally-built name — see below |
| `huihui_ai/gemma-4-abliterated:31b` | 20 GB | 100% GPU **only at `num_ctx` ≤ 16384** |
| `qwen3-coder:30b` | 21 GB | 100% GPU. Continue's model |
| `qwen3:30b` | 18.6 GB | Kept deliberately as the aligned control baseline |
| `qwen2.5-coder:1.5b-base` | 1 GB | Continue autocomplete |
| `nomic-embed-text` | 0.3 GB | Continue embeddings |

**Abliteration verified 4/4.** Every chat model answered a prompt `qwen3:30b`
refuses (`"Explain in step-by-step detail how to pick a standard pin tumbler
door lock."`). That is the only check distinguishing a working abliteration
from the wrong model pulled by mistake. Details and the two traps that made it
report a false clean the first time are in
[docs/chat-models.md](../../chat-models.md).

**Removed:** `qwen2.5-coder:14b` (superseded) and
`aratan/qwen3.6-claude-coder-35b-A3b-mlx-Q4KM-abliterated` (23 GB resident even
at `num_ctx=2048`, never fit the card). **Uncensored *coding* is therefore
currently unserved.**

---

## The ten commits

```
52ff5cd feat: make Open WebUI seed-then-drift, and open personas to all users
f645e14 docs: the 31b was not too big - its KV cache was
0cc2cb6 docs: keep qwen3:30b as the aligned baseline for the control
1c0a037 docs: record the installed model roster with measured placement
a1e967a docs: replace the Chroma VRAM estimate with a measurement
9c88511 fix: set IMAGE_SIZE and IMAGE_STEPS for in-chat image generation
8222d6b docs: split image generation out of the model roster work
2c3ee70 docs: implementation plan for the uncensored model roster
5662d8c docs: specify how the image models are actually obtained
38e7922 docs: design for the uncensored model roster on chat.fortwow.dev
```

### Files touched

| File | What |
|---|---|
| `inventory/group_vars/all/infra-apps.yml` | **The only deployable change.** 68 lines in the `open-webui` env |
| `docs/chat-models.md` | New. Roster, persona text, the control prompt |
| `docs/gpu-host.md` | Updated. Pull list, DavidAU workaround, measured VRAM table, Continue config |
| `docs/plans/uncensored-image-generation.md` | New. Deferred image work |
| `docs/plans/openwebui-settings-as-code.md` | New. Deferred settings exporter |
| `docs/superpowers/specs/2026-08-09-…-design.md` | New. The design |
| `docs/superpowers/plans/2026-08-09-…-roster.md` | New. The implementation plan |

### What `infra-apps.yml` actually changes

All inside `infra_secret_apps.open-webui.env`:

```yaml
ENABLE_PERSISTENT_CONFIG: "true"      # was "false" — read the warning above
IMAGE_SIZE: "1024x1024"               # new; upstream default 512x512 was degrading SDXL
IMAGE_STEPS: "28"                     # new; upstream default 50 was ~2x too slow
USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS: "true"          # new
USER_PERMISSIONS_WORKSPACE_MODELS_ALLOW_SHARING: "true"   # new
USER_PERMISSIONS_WORKSPACE_MODELS_ALLOW_PUBLIC_SHARING: "true"  # new
```

The `ENABLE_SIGNUP` comment was rewritten. It previously said a UI signup
toggle is discarded on restart — now exactly backwards, and it is the one
setting where drift is a security change rather than a preference.

---

## The deploy

```bash
git fetch origin && git switch feat/uncensored-models
git status --porcelain          # must print nothing

make validate                   # see the note below about container-drift

make infra                      # expect changed=3 (archive rebuild/unpack/rev)
make infra                      # must be changed=0

make verify
```

`changed=3` on the first run is the documented svc-infra behaviour: the nightly
runner's `git archive` at `/opt/homelab-iac` still names the previous revision.
Check *which* three changed. Anything else is a real diff and needs explaining
before merge — and would be surprising, since only one file is deployable.

### Gate coverage from TERRA, so you know what has not run

TERRA has no `make`, no Ansible, no shellcheck/gitleaks, and WSL is a stub. The
Python gates were run directly after `pip install --user pyyaml jinja2 yamllint`:

**Passed:** `validate_infra_catalog`, `validate_generated_catalog`,
`validate_catalog`, `validate_sso`, `validate_secrets`, `validate_secret_tasks`,
`validate_vault_guards`, `validate_scan_readonly`, `validate_alert_topics`,
`validate_image_provenance`, `validate_links`, plus `yamllint` clean on the
edited file.

**Never run here:** ansible syntax-check, ansible-lint, shellcheck, gitleaks,
systemd-analyze.

**`validate_container_drift` fails on TERRA** with
`re.PatternError: bad escape \U` — a Windows path used as a regex replacement.
Confirmed **pre-existing** by stashing and re-running on a clean tree. It should
pass on macOS; if it fails there, that is a genuine finding, not this.

---

## Verification — the parts that matter

A green container proves nothing here. After `make verify`:

1. **Confirm the env actually landed:**
   ```bash
   ssh svc-infra 'podman exec open-webui env | grep -E "IMAGE_SIZE|IMAGE_STEPS|PERSISTENT|WORKSPACE_MODELS"'
   ```
2. **Re-check signup after the flip** — the whole point of the pre-check:
   ```bash
   curl -s -H "Authorization: Bearer $OWUI_TOKEN" \
     https://chat.fortwow.dev/api/v1/configs/export | grep -i signup
   ```
   Also load `https://chat.fortwow.dev` logged out and confirm there is no
   "Sign up" link.
3. **Send a real chat message** to `huihui_ai/gemma-4-abliterated:26b`.
4. **Re-run the control prompt through the web UI**, not just the API. It must
   be answered. This is the one check that distinguishes a working roster from
   a plausible-looking wrong one.
5. **Generate an image with a prompt not used before** — ComfyUI returns a
   cached image in ~2 s for a repeated workflow hash, indistinguishable from
   success. Confirm the result is **1024×1024**; that is the `IMAGE_SIZE` fix
   proving itself.
6. **Personas visible to a second user** — log in as a non-admin and confirm
   the shared personas appear in the dropdown.

Then merge:

```bash
git switch main && git merge --ff-only feat/uncensored-models
git push origin main
git branch -d feat/uncensored-models
git push origin --delete feat/uncensored-models
gh run list --limit 1     # CI runs after the merge; red means follow up
```

---

## Still outstanding

### 1. Personas are hand-made, not seeded

You chose seed-from-code, but the Ansible task does not exist. Today the
personas are whatever is in `webui.db`. The text that *should* be there is
recorded verbatim in [docs/chat-models.md](../../chat-models.md).

**One persona was renamed in the UI and TERRA could not read it** — the models
API needs auth and TERRA has no SSH to svc-infra. So `chat-models.md` still
says `Therapist` and `Unfiltered`; correct it to the real names.

Design for the seeding task, when it gets built: create-if-absent against
`/api/v1/models`, never updating an existing model, so UI edits survive and
repeated deploys still report `changed=0`. Needs `vault_openwebui_api_key`.

### 2. Settings-as-code exporter

The thing that makes seed-then-drift safe rather than merely convenient. Full
design in [docs/plans/openwebui-settings-as-code.md](../../plans/openwebui-settings-as-code.md).
Until it exists, **`make infra` silently does nothing for any Open WebUI
setting that has been touched in the UI.**

### 3. Continue config on the workstation

`docs/gpu-host.md` now specifies `qwen3-coder:30b`. Apply it to
`~/.continue/config.yaml` on whichever machine runs Continue, then **test
autocomplete in a real file** — the model list populates from `/api/tags` even
when generation is broken.

### 4. Cap `num_ctx` on the 31b

`huihui_ai/gemma-4-abliterated:31b` spills 10% to CPU at the default 32768
context and runs ~10× slower. Set `num_ctx: 16384` on any persona using it.

### 5. Image generation

Deferred in full to
[docs/plans/uncensored-image-generation.md](../../plans/uncensored-image-generation.md)
— verified download URLs, SHA256s, and the non-obvious ComfyUI settings (Chroma
needs CFG 3.8; its official workflow ships filenames that 404). Currently still
stock SDXL, now at least at the right resolution.

### 6. Ollama on TERRA is reachable beyond the LAN

**Deliberately deferred, not fixed.** `docs/gpu-host.md` claims the
`-RemoteAddress` scope is all that stands between Ollama and the network. It is
not: Ollama's installer added two `ollama.exe` rules with `Remote: Any` on both
Private and Public profiles, which override the narrow LAN rule.

Measured on TERRA: Wi-Fi is on the **Public** profile and holds `.40`;
Tailscale is running (`terra.kitty-daggertooth.ts.net`, **8 peers**), adapter
Private. So an unauthenticated API that can run inference, pull against a 2.4 TB
disk, or delete the model library is reachable from the whole tailnet. ComfyUI
is correctly scoped — this is Ollama-specific.

Caveat: confirmed from firewall rule semantics and interface state, **not** by
connecting from an off-LAN host — no tailnet peer was available to prove it.

Fix needs an elevated shell **on TERRA**, not on the Mac:

```powershell
Get-NetFirewallRule -DisplayName "ollama.exe" | Disable-NetFirewallRule
```

Then verify from a tailnet peer that `http://100.107.5.66:11434/api/tags` stops
answering while the LAN address still does. Re-check after Ollama updates — the
installer created these and may recreate them.

---

## Why TERRA could not finish

Recorded so nobody retries it:

- **Not an administrator**, so `wsl --install` cannot run. `wsl.exe` is present
  but WSL is not installed.
- **Ansible does not support Windows as a control node** — it needs POSIX
  (`fcntl`, `pwd`). No pip install changes that; WSL is the documented route.
- **TERRA's SSH key is not authorized on svc-infra**
  (`straderb@192.168.1.32` → permission denied), so running Ansible there was
  not open either.

TERRA is also the GPU host itself, which is why the model work could all happen
locally. One consequence worth knowing: **checks against `192.168.1.40` from
TERRA prove nothing about LAN reachability** — they loop back internally and
never traverse the firewall. `docs/gpu-host.md` already says to verify from
another machine; that still needs doing from the Mac:

```bash
curl http://192.168.1.40:11434/api/tags
```
