# Secret Guard Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-written `REPLACE_` placeholder asserts guarding the bespoke multi-container stacks with a declarative per-host list, extending the `require_vault` pattern the single-container catalog already uses.

**Architecture:** `infra_secret_apps` entries already carry `require_vault: [vault_x]`, one generic `subelements` assert consumes them, and `tests/validate_vault_guards.py` already parses that form. This extends the same idea to the stacks that are not catalog entries — Paperless, NetBox, Immich, Nextcloud, RomM, Minecraft — via a `bespoke_secrets` mapping in each role's defaults, plus one generic assert per role. The gate learns the new source **before** any inline assert is removed, so it is never blind.

**Tech Stack:** Ansible, Jinja `subelements`, Python (the gate).

**Spec:** The architecture review of 2026-09-03 (this repo, conversation record). Finding #5, narrowed as recommended there — the Quadlet renders are **not** generalised, only the secret guards.

---

## Read this before starting: this is the weakest of the six branches

Stated plainly, because the plan should not oversell the work it describes.

**What it buys:** roughly 75 net lines removed; one place per host that lists every secret it requires; a gate reading structured YAML instead of regexing `that:` clause prose, which retires several of the blind spots `validate_vault_guards.py` documents in its own docstring.

**What it costs:**

- **`fail_msg` quality drops, and this is a real regression.** The current NetBox assert names all four variables in one message and explains *why* the secret key needs 50 characters. A generic loop emits one message per variable with no service-specific context. An operator hitting this at 11pm gets less help than they do today.
- **Three deploys.** It touches `svc_infra`, `svc_media` and the gate, so it needs `make infra`, `make media` and a `changed=0` proof on both.
- **The asserts move.** Today each guard sits immediately above the render it protects. A single upfront loop fails earlier — arguably better, but it is a change in behaviour, not just in shape.

**Recommendation: do this last, after the other five have merged, or decide not to do it.** "Leave it alone" is a defensible answer here in a way it is not for the shell gate or the media sweep. If you want the line reduction without the `fail_msg` regression, a smaller version is available: migrate only Paperless and Immich (whose asserts carry no length constraints and little explanatory text) and leave NetBox and Nextcloud inline. That is Task 4 below, marked optional.

---

## Global Constraints

- **The gate is updated first.** `tests/validate_vault_guards.py` must understand `bespoke_secrets` and pass against the *unmigrated* tree before a single inline assert is deleted. A window where the gate cannot see a guard is exactly the failure this repo keeps writing down.
- **Never remove a `REPLACE_` guard without a replacement in the same commit.** The gate's whole purpose is that a length check is not a placeholder check; `vault_netbox_secret_key` escapes today only because the placeholder happens to be 27 characters.
- `no_log: true` on every task that touches a vault variable. `diff: false` where a render is involved.
- Preserve the two length constraints exactly: `vault_netbox_secret_key` needs `>= 50`, `vault_romm_auth_secret_key` needs `>= 32`. Losing either is a silent downgrade.
- Do **not** touch `require_vault` or the existing `infra_secret_apps` assert. It works, the gate understands it, and widening its schema would put a working control at risk for cosmetic consistency.
- Authelia's guards stay inline. They are not a "secret is set" check — they assert every account has a *distinct* argon2 hash, which is a different shape and does not fit this loop.

---

## Current state (measured 2026-09-03)

15 `is match('REPLACE_')` clauses across three files: 10 in `roles/svc_infra/tasks/files.yml`, 4 in `roles/svc_media/tasks/files.yml`, 1 in `roles/svc_download/tasks/files.yml`.

In scope for migration:

| Stack | Role | Variables | Length constraint |
|---|---|---|---|
| Paperless-ngx | svc_infra | `vault_paperless_secret_key`, `vault_paperless_admin_password` | none |
| NetBox | svc_infra | `vault_netbox_db_password`, `vault_netbox_secret_key`, `vault_netbox_redis_password`, `vault_netbox_superuser_password` | `secret_key >= 50` |
| Immich | svc_infra | `vault_immich_db_password` | none |
| Nextcloud | svc_infra | `vault_nextcloud_db_password`, `vault_nextcloud_admin_password` | none |
| RomM | svc_media | `vault_romm_db_password`, `vault_romm_db_root_password`, `vault_romm_auth_secret_key` | `auth_secret_key >= 32` |
| Minecraft | svc_media | `vault_minecraft_rcon_password` | none |

Out of scope: Authelia (different shape, see constraints), the `svc_download` guard (a single assert, no duplication to remove), and the existing `infra_secret_apps` loop (already declarative).

Two comments in the current asserts carry reasoning that must survive the migration — move them into the defaults file rather than deleting them with the block:

- RomM: `vault_romm_auth_secret_key` is only caught by the `>= 32` check because its placeholder happens to be 27 characters. That is luck, not a control, which is why it is guarded explicitly as well. Also: once MariaDB's data directory is initialised, changing the root password means `ALTER USER` inside the running database, never a redeploy.
- NetBox: `vault_netbox_superuser_password` is the account NetBox creates for itself on first start, on a service every host on the LAN can reach.

---

## File Structure

- Modify: `tests/validate_vault_guards.py` — teach it `bespoke_secrets`
- Modify: `roles/svc_infra/defaults/main.yml` — add `infra_bespoke_secrets`
- Modify: `roles/svc_media/defaults/main.yml` — add `media_bespoke_secrets`
- Modify: `roles/svc_infra/tasks/files.yml` — one generic assert; delete four inline blocks
- Modify: `roles/svc_media/tasks/files.yml` — one generic assert; delete two inline blocks

## The declared form

A `bespoke_secrets` value is a mapping of stack name to a list of entries. An entry is either a bare variable name, or a mapping when a length floor applies:

```yaml
infra_bespoke_secrets:
  paperless:
    - vault_paperless_secret_key
    - vault_paperless_admin_password
  netbox:
    - vault_netbox_db_password
    - { var: vault_netbox_secret_key, min_length: 50 }
    - vault_netbox_redis_password
    - vault_netbox_superuser_password
```

Two forms rather than one because the bare string is the overwhelmingly common case and a repo-wide `{var: ...}` would be noise. `require_vault` in `infra-apps.yml` keeps its bare-string-only schema, untouched.

---

### Task 1: Teach the gate the new source, before anything migrates

**Files:**
- Modify: `tests/validate_vault_guards.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `bespoke_require_vault() -> dict[str, str]` — vault variable → `"<role>:<stack>"` that declares it. Merged into the existing `catalog` set so a declared variable counts as guarded.

- [ ] **Step 1: Read the gate's existing catalog path**

Run: `sed -n '109,125p' tests/validate_vault_guards.py`

`catalog_require_vault()` is the function to mirror. Understand how its result is consumed in `main()` before writing the parallel one — in particular, whether a variable appearing in the catalog is treated as both *required* and *guarded*, or only one of the two.

- [ ] **Step 2: Write the failing test**

The gate has no pytest harness. Add a temporary assertion at the top of `main()` that pins the intended behaviour:

```python
    # TEMPORARY — proves bespoke_secrets is parsed from both role defaults.
    bespoke = bespoke_require_vault()
    assert "vault_netbox_secret_key" in bespoke, sorted(bespoke)
    assert bespoke["vault_netbox_secret_key"] == "svc_infra:netbox", bespoke
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/python tests/validate_vault_guards.py`

Expected: FAIL with `NameError: name 'bespoke_require_vault' is not defined`.

- [ ] **Step 4: Implement the parser**

Add below `catalog_require_vault()`:

```python
def bespoke_require_vault() -> dict[str, str]:
    """vault var -> "<role>:<stack>" for every bespoke_secrets declaration.

    The multi-container stacks (Paperless, NetBox, Immich, Nextcloud, RomM,
    Minecraft) are not catalog entries, so they cannot carry `require_vault`
    the way infra_secret_apps entries do. They declare `*_bespoke_secrets` in
    their role defaults instead, and one generic assert per role consumes it.

    An entry is a bare variable name, or a {var, min_length} mapping where a
    length floor applies. Both forms are recognised here; a mapping without a
    `var` key is a declaration error and is reported, not skipped, because a
    silently ignored entry is an unguarded secret.
    """
    owners: dict[str, str] = {}
    for defaults in sorted(ROLES.glob("*/defaults/main.yml")):
        document = yaml.safe_load(defaults.read_text(encoding="utf-8")) or {}
        role = defaults.parent.parent.name
        for key, stacks in document.items():
            if not isinstance(key, str) or not key.endswith("_bespoke_secrets"):
                continue
            if not isinstance(stacks, dict):
                continue
            for stack, entries in stacks.items():
                for entry in entries or []:
                    if isinstance(entry, str):
                        owners.setdefault(entry, f"{role}:{stack}")
                    elif isinstance(entry, dict) and "var" in entry:
                        owners.setdefault(str(entry["var"]), f"{role}:{stack}")
                    else:
                        owners.setdefault(
                            f"<malformed entry in {role}:{stack}>", f"{role}:{stack}"
                        )
    return owners
```

- [ ] **Step 5: Merge it into the gate's view and add its positive control**

In `main()`, after the existing `catalog = catalog_require_vault()` line:

```python
    bespoke = bespoke_require_vault()
    malformed = [name for name in bespoke if name.startswith("<malformed")]
    if malformed:
        failures.append(
            "a *_bespoke_secrets entry is neither a string nor a {var: ...} "
            f"mapping: {malformed}. A silently skipped entry is an unguarded "
            "secret, so this is fatal rather than ignored."
        )
    catalog = {**catalog, **bespoke}
```

Place the `bespoke`/`malformed` block **after** `failures` is initialised. Note there is deliberately **no** "empty means broken" control on `bespoke` yet — it is legitimately empty until Task 2 lands. Task 3 Step 5 adds that control once it should never be empty again.

- [ ] **Step 6: Run and confirm the temporary assertion now fails for the right reason**

Run: `.venv/bin/python tests/validate_vault_guards.py`

Expected: FAIL on the temporary assertion — `vault_netbox_secret_key` is not in `bespoke`, because nothing declares it yet. That is correct: the parser works, the data does not exist. Remove the temporary assertion.

- [ ] **Step 7: Confirm the gate passes against the unmigrated tree**

Run: `make validate-secrets`

Expected: PASS. This is the critical checkpoint — the gate understands the new form and still fully covers the old one. **Do not proceed to Task 2 until this passes.**

- [ ] **Step 8: Commit**

```bash
git add tests/validate_vault_guards.py
git commit -m "test: teach the vault guard gate to read bespoke_secrets

The multi-container stacks cannot carry require_vault the way catalog
entries do, so they will declare *_bespoke_secrets in role defaults. The
gate learns that source BEFORE anything migrates to it — a window where a
guard exists but the gate cannot see it is the failure mode this repo
keeps finding.

A malformed entry is fatal rather than skipped: a silently ignored
declaration is an unguarded secret."
```

---

### Task 2: Declare and enforce svc-infra's bespoke secrets

**Files:**
- Modify: `roles/svc_infra/defaults/main.yml`, `roles/svc_infra/tasks/files.yml`

**Interfaces:**
- Consumes: `bespoke_require_vault()` from Task 1.
- Produces: `infra_bespoke_secrets`.

- [ ] **Step 1: Read the four inline asserts and transcribe them exactly**

```bash
grep -n "is match('REPLACE_')" roles/svc_infra/tasks/files.yml
```

For each of the four blocks, write down every variable and every length constraint. **Transcribe, do not recall** — Nextcloud's variable list is not stated in this plan for exactly this reason.

- [ ] **Step 2: Add the declaration**

In `roles/svc_infra/defaults/main.yml`:

```yaml
# Secrets required by the bespoke multi-container stacks — the ones that are
# not infra_apps/infra_secret_apps catalog entries and so cannot carry
# `require_vault` the way those do. One generic assert in files.yml consumes
# this, and tests/validate_vault_guards.py reads it to confirm every one is
# placeholder-guarded.
#
# An entry is a bare variable name, or {var, min_length} where a length floor
# applies. The floors are requirements of the software, not style: NetBox
# refuses to start with a SECRET_KEY under 50 characters.
#
# A length check is NOT a placeholder check and never substitutes for one. The
# generic assert applies both to every entry here, because a var still holding
# "REPLACE_openssl_rand_hex_24" is non-empty and would otherwise deploy a
# credential whose plaintext is published in a committed file.
infra_bespoke_secrets:
  paperless:
    - vault_paperless_secret_key
    - vault_paperless_admin_password
  netbox:
    - vault_netbox_db_password
    - { var: vault_netbox_secret_key, min_length: 50 }
    - vault_netbox_redis_password
    - vault_netbox_superuser_password
  immich:
    - vault_immich_db_password
  nextcloud:
    - vault_nextcloud_db_password
    - vault_nextcloud_admin_password
```

Cross-check every line against Step 1's transcription before continuing. If a stack's real list differs from the above, the tree is right and this plan is stale — use the tree.

- [ ] **Step 3: Add the generic assert**

In `roles/svc_infra/tasks/files.yml`, immediately **before** the "Create Paperless-ngx appdata directories" task — so every bespoke secret is checked before any of the four stacks renders anything:

```yaml
# One guard for every bespoke stack's secrets, replacing four hand-written
# assert blocks that had drifted apart in wording while making the same two
# checks. Mirrors the infra_secret_apps loop above; the difference is only
# where the declaration lives, because these stacks are not catalog entries.
#
# Both checks apply to every entry. Non-empty alone is not a guard: a var
# still holding its all_vault.yml.example value ("REPLACE_openssl_rand_hex_24")
# is non-empty, and deploying it publishes the credential in a committed file.
# vault_netbox_superuser_password is the sharpest case — NetBox creates that
# account for itself on first start, on a service the whole LAN can reach.
- name: Require every bespoke infra stack's vault secrets
  ansible.builtin.assert:
    that:
      - >-
        lookup('vars', (item.1.var | default(item.1)), default='') | string
        | length >= (item.1.min_length | default(1))
      - >-
        not (lookup('vars', (item.1.var | default(item.1)), default='')
             | string is match('REPLACE_'))
    fail_msg: >-
      {{ item.1.var | default(item.1) }} is empty, shorter than the
      {{ item.1.min_length | default(1) }} characters
      {{ item.0.key }} requires, or still set to its all_vault.yml.example
      REPLACE_* placeholder — set a real value in the encrypted inventory.
  loop: >-
    {{ infra_bespoke_secrets | default({}) | dict2items
       | subelements('value', skip_missing=True) }}
  loop_control:
    label: "{{ item.0.key }}:{{ item.1.var | default(item.1) }}"
  no_log: true
  diff: false
```

`item.1.var | default(item.1)` resolves both forms: on a bare string, `.var` is undefined and the default supplies the string itself.

- [ ] **Step 4: Verify the loop expression before deleting anything**

Run: `make validate`

Expected: PASS. Then confirm the loop actually produces the expected pairs, without a deploy:

```bash
ANSIBLE_INVENTORY=tests/fixtures/inventory.yml .venv/bin/ansible-playbook \
  --syntax-check site.yml
```

A `subelements` mistake usually surfaces as a runtime error, not a syntax one, so also eyeball the rendered pairs by hand:

```bash
.venv/bin/python - <<'PY'
import yaml, pathlib
d = yaml.safe_load(pathlib.Path("roles/svc_infra/defaults/main.yml").read_text())
for stack, entries in d["infra_bespoke_secrets"].items():
    for e in entries or []:
        name = e["var"] if isinstance(e, dict) else e
        floor = e.get("min_length", 1) if isinstance(e, dict) else 1
        print(f"{stack:12} {name:38} min_length={floor}")
PY
```

Cross-check this output against Step 1's transcription. Every variable and every floor must appear, with nothing added and nothing lost.

- [ ] **Step 5: Delete the four inline asserts**

Remove the four `assert` blocks transcribed in Step 1 — and only those. Leave every surrounding comment that explains something other than the assert itself (the NetBox block's note about `vault_netbox_secret_key` escaping only by placeholder length is worth keeping; move it into the defaults file if it does not survive here).

- [ ] **Step 6: Confirm the gate still sees every variable**

Run: `make validate-secrets`

Expected: PASS, with the same set of variables reported as required and guarded as before Task 2. If the gate's summary now reports fewer guarded variables, a declaration is missing — find it before continuing.

- [ ] **Step 7: Commit, then deploy**

```bash
git add roles/svc_infra/defaults/main.yml roles/svc_infra/tasks/files.yml
git commit -m "refactor: declare svc-infra's bespoke stack secrets

Four hand-written assert blocks making the same two checks, replaced by
one loop over infra_bespoke_secrets — the same shape the infra_secret_apps
catalog already uses, with the declaration in role defaults because these
stacks are not catalog entries.

Both length floors are preserved. tests/validate_vault_guards.py learned
this source in the previous commit, so nothing was unguarded in between."
```

Run: `make infra`, then `make verify`, then `make infra` again requiring `changed=0`.

**Operator step — deploys are not available to an agent in this environment.**

- [ ] **Step 8: Prove the guard actually fires**

An assert that never fails is indistinguishable from an assert that does not run. Prove it once:

```bash
make infra ARGS="-e vault_immich_db_password=REPLACE_openssl_rand_hex_24 --tags files --check"
```

Expected: the deploy FAILS on the new assert, naming `vault_immich_db_password`. If it passes, the loop is not reaching that entry.

---

### Task 3: Declare and enforce svc-media's bespoke secrets

**Files:**
- Modify: `roles/svc_media/defaults/main.yml`, `roles/svc_media/tasks/files.yml`, `tests/validate_vault_guards.py`

**Interfaces:**
- Consumes: `infra_bespoke_secrets` as the pattern to mirror.
- Produces: `media_bespoke_secrets`.

- [ ] **Step 1: Transcribe the two inline asserts**

```bash
grep -n "is match('REPLACE_')" roles/svc_media/tasks/files.yml
```

Two blocks: "Require RomM database and authentication secrets" and "Require the Minecraft RCON password". Transcribe every variable and the `>= 32` floor on `vault_romm_auth_secret_key`.

- [ ] **Step 2: Add the declaration**

In `roles/svc_media/defaults/main.yml`, with a comment pointing at the svc-infra one rather than repeating it:

```yaml
# Secrets required by svc-media's bespoke stacks. Same contract as
# infra_bespoke_secrets in roles/svc_infra/defaults/main.yml — read the
# comment there for why the two forms exist and why a length check is not a
# placeholder check.
media_bespoke_secrets:
  romm:
    - vault_romm_db_password
    - vault_romm_db_root_password
    # 32 is RomM's own floor. The placeholder happens to be 27 characters, so
    # this check catches it by luck; the REPLACE_ guard the generic assert
    # also applies is what actually protects it.
    - { var: vault_romm_auth_secret_key, min_length: 32 }
  minecraft:
    - vault_minecraft_rcon_password
```

- [ ] **Step 3: Add the generic assert**

Insert into `roles/svc_media/tasks/files.yml` before "Render rootless media Quadlets". Copy the task from Task 2 Step 3 verbatim, changing `infra_bespoke_secrets` → `media_bespoke_secrets` and the name to "Require every bespoke media stack's vault secrets". Do not paraphrase the comment — the reasoning is identical and two divergent explanations of the same control is how they drift.

- [ ] **Step 4: Delete the two inline asserts and validate**

Run: `make validate`

Expected: PASS.

- [ ] **Step 5: Close the positive control on the gate**

Now that both roles declare `bespoke_secrets`, an empty parse means the gate has stopped looking. Add to `tests/validate_vault_guards.py` in `main()`, beside the existing `if not catalog:` control:

```python
    if not bespoke:
        failures.append(
            "parsed no *_bespoke_secrets declarations out of roles/*/defaults/"
            "main.yml. Both svc_infra and svc_media declare one, so an empty "
            "read means this gate is no longer reaching them — not that there "
            "is nothing to guard."
        )
```

This is deliberately added now rather than in Task 1, where it would have failed against a tree that legitimately had none.

- [ ] **Step 6: Confirm the control works**

Temporarily rename `media_bespoke_secrets` to `media_bespoke_secrets_x` in the defaults, run `make validate-secrets`, and confirm it still passes (svc_infra's declaration remains) — then rename **both** and confirm it now FAILS with the new message. Restore both names.

- [ ] **Step 7: Full validation**

Run: `make validate`

Expected: PASS.

- [ ] **Step 8: Commit, then deploy**

```bash
git add roles/svc_media/defaults/main.yml roles/svc_media/tasks/files.yml \
        tests/validate_vault_guards.py
git commit -m "refactor: declare svc-media's bespoke stack secrets

Same shape as svc-infra's, RomM's >= 32 floor preserved.

With both roles declaring one, an empty parse now means the gate stopped
looking rather than that there is nothing to guard, so it is fatal."
```

Run: `make media`, `make verify`, then `make media` again requiring `changed=0`. **Operator step.**

---

### Task 4 (OPTIONAL): the reduced-scope alternative

Only relevant if the `fail_msg` regression described at the top is judged too expensive for NetBox and Nextcloud, whose inline messages carry the most explanation.

- [ ] Migrate Paperless, Immich, RomM and Minecraft only.
- [ ] Leave the NetBox and Nextcloud assert blocks inline, untouched.
- [ ] Add a comment above each surviving block saying it is deliberately not migrated, and why: its `fail_msg` explains a requirement (the 50-character floor, the superuser-on-first-start hazard) that a generic message cannot carry.
- [ ] The gate needs no change — a variable guarded inline is recognised by the existing `that:`-clause path, and one declared in `bespoke_secrets` by the new path. Both count.

This keeps roughly half the line reduction and none of the operator-experience cost. It is the version to choose if Task 2 Step 5 feels like it is deleting something valuable.

---

## Verification before merge

Role code on two live hosts, so validation alone is not sufficient.

- [ ] `make validate` passes.
- [ ] `git status --porcelain` prints nothing.
- [ ] `make validate-secrets` reports the same coverage as before this branch. The baseline, measured on 2026-09-03 against `main`:

  ```
  Vault placeholder guards: OK (25 of 31 placeholder vars guarded across 59
  asserts, 11 via the infra_secret_apps loop, 6 required by no visible assert)
  ```

  After the migration the *first* number must still be **25 or higher** and the last must still be **6 or lower**. The middle two will move — fewer asserts, more variables resolved through a loop — and that is the change working. A drop in the guarded count is the one failure this branch could plausibly cause that nothing else would catch.
- [ ] `make infra` and `make media` each deployed from the clean tree, each followed by a second run reporting `changed=0`.
- [ ] `make verify` passes on all three VMs.
- [ ] Task 2 Step 8's deliberate-failure probe was run and did fail.
- [ ] Every one of the 15 original `REPLACE_` clauses is either still present or accounted for by a declaration: `grep -c "is match('REPLACE_')" roles/*/tasks/*.yml` plus the declared entries should cover the same variable set.

## Merge

Standard workflow, with both deploys and the live verify as evidence. Merge to `main`, push, delete the branch.
