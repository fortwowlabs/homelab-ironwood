# Python Lint Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lint the ~7,000 lines of Python in `tests/` and `scripts/` that currently have no linter at all, and take the four free ansible-lint correctness rules while the lint config is open.

**Architecture:** Add a pinned `ruff` to the dev requirements and invoke it directly from a new `validate-python` Make target, mirroring exactly how ShellCheck and gitleaks are already invoked — not as a `tests/validate_*.py` gate, because it lints those gates and a gate that lints itself through `run_gates.py` inverts the dependency for no gain. Rule selection is committed in `ruff.toml`.

**Tech Stack:** ruff 0.16.5 (pinned), ansible-lint 26.6.0 (already pinned), GNU Make.

**Spec:** The architecture review of 2026-09-03 (this repo, conversation record). Findings #4 and the ansible-lint item under "Smaller things".

## Global Constraints

- `ruff` is **pinned by exact version** in `requirements-dev.txt`, like every other tool this repo installs. Never `>=`.
- `validate-tools` must fail with exit 127 and an actionable message when ruff is absent, matching the existing checks for `ansible-lint`, `yamllint`, ShellCheck and gitleaks.
- The rule selection is explicit in `ruff.toml`. Do **not** rely on ruff's built-in default select — it has changed between ruff releases, and an unpinned rule set makes an upgrade silently widen or narrow the gate.
- `.ruff_cache/` must be gitignored. It is written into the repo root on every run, and the workflow requires `git status --porcelain` to print nothing.
- CI installs through `make deps-dev`; adding the pin is sufficient, no workflow edit is needed.

---

## Current state (measured 2026-09-03, ruff 0.16.5)

43 Python files, ~7,000 lines, zero linting. ShellCheck covers shell; nothing covers Python — including the 30 validation gates that everything else in this repo trusts.

With `select = ["E4", "E7", "E9", "F", "B", "UP", "I"]` the whole tree reports **15 findings**:

| Rule | Count | What it is |
|---|---|---|
| `E741` | 7 | Ambiguous variable name `l`, all in `tests/validate_metric_write.py` |
| `I001` | 2 | Import block unsorted |
| `F401` | 2 | Unused import (`os` in `validate_metric_write.py`; `AttrDict` in `validate_shell_templates.py`) |
| `UP035` | 1 | Deprecated `typing` import |
| `UP031` | 1 | `%`-format instead of f-string |
| `B905` | 1 | `zip()` without an explicit `strict=` |
| `B005` | 1 | `.strip()` with a multi-character argument |

`B905` and `B005` are genuine correctness rules, not style: a `zip()` over mismatched lengths truncates silently, and `.strip("foo")` strips a character *set*, not a suffix — the classic misreading.

**Note on ordering:** the `F401` in `tests/validate_shell_templates.py` is also fixed by the shell-gate-discovery branch (Task 2, Step 4). Whichever branch merges second will find it already gone. That is fine — do not treat its absence as a missing finding.

Adding `jinja`, `no-free-form`, `empty-string-compare` and `deprecated-module` to `.ansible-lint`'s `enable_list` reports **zero** new findings across all 65 files. It costs nothing today and ratchets the floor for tomorrow.

## File Structure

- Create: `ruff.toml` — rule selection and target version. Root, alongside `.ansible-lint`, `.yamllint.yml`, `.gitleaks.toml`.
- Modify: `requirements-dev.txt` — add the pin.
- Modify: `Makefile` — `RUFF` variable, `validate-tools` presence check, `validate-python` target, add it to `validate` and to `.PHONY`.
- Modify: `.gitignore` — `.ruff_cache/`.
- Modify: `.ansible-lint` — four rules into `enable_list`, with the measurement recorded in a comment.
- Modify: `tests/validate_metric_write.py`, `tests/validate_shell_templates.py`, and whichever files carry the remaining six findings — the fixes themselves.

---

### Task 1: Pin ruff, configure it, and wire the Make target

**Files:**
- Create: `ruff.toml`
- Modify: `requirements-dev.txt`, `Makefile`, `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: `make validate-python`, invoked by `make validate`. Task 2 runs it to drive the fixes.

- [ ] **Step 1: Pin the version**

Append to `requirements-dev.txt`:

```
ruff==0.16.5
```

- [ ] **Step 2: Install it**

Run: `.venv/bin/python -m pip install --requirement requirements-dev.txt`

Expected: ruff 0.16.5 installed. Confirm with `.venv/bin/ruff --version`.

- [ ] **Step 3: Write the rule selection**

Create `ruff.toml`:

```toml
# Python lint configuration.
#
# WHY THIS EXISTS AT ALL
#
# Shell in this repo goes through ShellCheck; the ~7,000 lines of Python in
# tests/ and scripts/ went through nothing. That is the wrong asymmetry,
# because the 30 files in tests/ are the gates every other claim in this repo
# rests on — an unchecked bug there does not fail loudly, it passes quietly.
#
# WHY THE SELECTION IS EXPLICIT
#
# ruff's built-in default select has changed between releases. Relying on it
# would mean a version bump could silently widen the gate (a wall of new
# findings at the worst moment) or narrow it (rules stop being enforced with
# no diff to show it). Name the rules; the pin in requirements-dev.txt and
# this list then fully describe what runs.
#
# Measured 2026-09-03 against the whole tree: 15 findings, all fixed in the
# branch that added this file. Two of the fifteen were correctness rather
# than style — a zip() with no strict= and a .strip() given a multi-character
# argument, which strips a character SET rather than the suffix its author
# meant.
target-version = "py313"

[lint]
select = [
    "E4",   # pycodestyle: imports
    "E7",   # pycodestyle: statement issues, incl. E741 ambiguous names
    "E9",   # pycodestyle: syntax and IO errors
    "F",    # pyflakes: undefined names, unused imports and variables
    "B",    # flake8-bugbear: likely bugs, incl. B905 zip-without-strict
    "UP",   # pyupgrade: constructs superseded by the pinned Python
    "I",    # isort: import ordering
]

# Deliberately NOT selected, with reasons, so the next person does not have to
# re-derive them:
#   S (bandit)   — subprocess and shell use is the entire job of these scripts;
#                  it would report the design, not a defect.
#   ANN / D      — annotations and docstrings. The gates are already unusually
#                  well documented in prose; enforcing shape adds noise.
#   E501         — line length. Comments here carry incident history and the
#                  reflow would cost more than the consistency buys.
```

- [ ] **Step 4: Ignore the cache directory**

Add to `.gitignore`, near the other tool scratch entries:

```
# ruff writes this into the repo root on every run. The change workflow
# requires `git status --porcelain` to print nothing, so an un-ignored cache
# would make the clean-tree check fail for a reason that is not a change.
.ruff_cache/
```

- [ ] **Step 5: Add the Make plumbing**

In `Makefile`, add the variable alongside `SHELLCHECK` and `GITLEAKS`:

```make
RUFF ?= $(BIN)ruff
```

Add the presence check to `validate-tools`, after the gitleaks line:

```make
	@test -x "$(RUFF)" || { echo "missing $(RUFF); run 'make deps-dev'" >&2; exit 127; }
```

Add the target, after `validate-shell`:

```make
validate-python:
# Invoked directly rather than through tests/run_gates.py, the same way
# ShellCheck and gitleaks are. run_gates.py exists so that ADDING a gate is a
# new file rather than a new Makefile line; ruff is not a gate, it is a tool
# that lints the gates, and routing it through the runner it lints would
# invert that dependency for nothing.
	$(RUFF) check --no-cache tests scripts
```

Add `validate-python` to the `validate` target's prerequisite list, between `validate-shell` and `validate-links`:

```make
validate: validate-tools validate-syntax validate-ansible validate-yaml validate-shell validate-python validate-links validate-catalog validate-provisioning validate-systemd validate-secrets validate-ci ## Run every offline validation gate
```

Add `validate-python` to the `.PHONY` list.

- [ ] **Step 6: Run it and confirm it fails with exactly 15 findings**

Run: `make validate-python`

Expected: FAIL, `Found 15 errors.` If the count differs, do not adjust the config to match — read the findings. A higher count means the tree moved since measurement; a lower one means the selection did not apply.

- [ ] **Step 7: Commit the tooling, before the fixes**

```bash
git add ruff.toml requirements-dev.txt Makefile .gitignore
git commit -m "build: add a pinned ruff gate over tests/ and scripts/

Shell went through ShellCheck; ~7,000 lines of Python — including the 30
gates every other claim here rests on — went through nothing.

Rules are named explicitly rather than inherited from ruff's default
select, which has moved between releases: with the pin, this file and
requirements-dev.txt fully describe what runs. Fifteen findings at the
time of writing; they are fixed in the following commit so this one shows
the gate arriving red."
```

---

### Task 2: Fix the fifteen findings

**Files:**
- Modify: `tests/validate_metric_write.py` (8 findings), `tests/validate_shell_templates.py` (1), and the four files carrying `I001`, `UP035`, `UP031`, `B905`, `B005` — identify them from the gate output.

**Interfaces:**
- Consumes: `make validate-python` from Task 1.
- Produces: a clean lint run. Nothing later depends on the specific fixes.

- [ ] **Step 1: List the findings with their files**

Run: `.venv/bin/ruff check --no-cache tests scripts --output-format=concise`

Write the list down before changing anything — the two `B` findings need reading, not auto-fixing.

- [ ] **Step 2: Apply the mechanical fixes only**

Run: `.venv/bin/ruff check --no-cache tests scripts --fix`

This resolves the safely-fixable subset (import sorting, unused imports). It will **not** touch `E741`, `B905` or `B005`. Do not pass `--unsafe-fixes`.

- [ ] **Step 3: Rename the ambiguous variables by hand**

The seven `E741` findings are all `l` in `tests/validate_metric_write.py`, used for a metrics line. Rename each to `line`. Check for shadowing of an existing `line` in the same scope before renaming; if one exists, use `metric_line`.

Run: `.venv/bin/python tests/validate_metric_write.py`

Expected: PASS. The rename is meaning-preserving; if this gate now fails, the rename hit the wrong binding.

- [ ] **Step 4: Read and fix B905 and B005**

These are the two that matter. For each:

- `B905` — a `zip()` with no `strict=`. Decide which is true: if the two sequences must be the same length, pass `strict=True` so a mismatch raises instead of truncating. If a shorter sequence is expected and intended, pass `strict=False` and add a one-line comment saying why. Do not pick `strict=False` to silence it.
- `B005` — `.strip("...")` with more than one character. Confirm what the author meant. If they meant to remove a prefix or suffix, replace with `.removeprefix("...")` / `.removesuffix("...")`. If they genuinely meant a character set, leave the behaviour and add `# noqa: B005` with a comment saying the set is intentional.

- [ ] **Step 5: Fix UP035 and UP031**

`UP035` is a `typing` import superseded on Python 3.13 — take ruff's suggested replacement. `UP031` is a `%`-format string; convert to an f-string, preserving the exact output including any padding.

- [ ] **Step 6: Confirm the gate is clean**

Run: `make validate-python`

Expected: `All checks passed!`

- [ ] **Step 7: Confirm nothing regressed**

Run: `make validate`

Expected: PASS across every group. This matters more than usual — Task 2 edited gate scripts, so a gate broken by its own lint fix would show up here.

- [ ] **Step 8: Commit**

```bash
git add tests scripts
git commit -m "fix: resolve the fifteen findings the new ruff gate reports

Twelve are style. Two are not: a zip() that would truncate silently on a
length mismatch rather than raise, and a .strip() handed a multi-character
argument, which strips a character SET and not the suffix it reads as.

The seven ambiguous-name findings are one variable, l, in
validate_metric_write.py, renamed to line."
```

---

### Task 3: Take the four free ansible-lint rules

**Files:**
- Modify: `.ansible-lint`

**Interfaces:**
- Consumes: nothing from Tasks 1–2; independent, but shares the branch because it is the same concern.
- Produces: nothing consumed later.

- [ ] **Step 1: Measure before changing, so the comment can state a fact**

Run:

```bash
ANSIBLE_INVENTORY=tests/fixtures/inventory.yml .venv/bin/ansible-lint --offline --profile min \
  --enable-list no-log-password,risky-file-permissions,risky-octal,risky-shell-pipe,latest,package-latest,jinja,no-free-form,empty-string-compare,deprecated-module \
  site.yml preflight.yml verify.yml verify-disruptive.yml scan.yml release.yml
```

Expected: `Passed: 0 failure(s), 0 warning(s) in 65 files processed`. If it reports findings, they are real and must be fixed in this task before the rules go in — do not add a rule the tree does not satisfy.

- [ ] **Step 2: Add the rules with the measurement recorded**

In `.ansible-lint`, extend `enable_list` and its comment block. Append to the existing list of "what each one guards":

```
#   jinja                   — a template expression that does not parse
#   no-free-form            — `command: foo=bar` parsed as a module arg, not a word
#   empty-string-compare    — `when: x == ""`, which is not the emptiness test
#                             it looks like under Ansible's type coercion
#   deprecated-module       — a module scheduled for removal from a pinned collection
#
# These four were measured on 2026-09-03 against all 65 files: zero findings.
# They are a free ratchet — they cost nothing today and fail the build the
# first time one of these mistakes is made. That is the opposite trade from
# `--profile safety`, which reports 353 findings of which 352 are style.
```

and to the list itself:

```yaml
  - jinja
  - no-free-form
  - empty-string-compare
  - deprecated-module
```

- [ ] **Step 3: Run the ansible lint group**

Run: `make validate-ansible`

Expected: PASS, 0 failures.

- [ ] **Step 4: Commit**

```bash
git add .ansible-lint
git commit -m "build: enable four ansible-lint correctness rules

jinja, no-free-form, empty-string-compare and deprecated-module. Measured
against all 65 files on 2026-09-03: zero findings, so this costs nothing
today and fails the build the first time one of these is written.

The profile stays at min for the reason already documented above the
enable_list — these are correctness rules pulled in individually, not a
step toward raising it."
```

---

## Verification before merge

No role code changes, so no deploy and no `make verify`.

- [ ] `make validate` passes end to end.
- [ ] `git status --porcelain` prints nothing — specifically confirm `.ruff_cache/` does not appear, which is the whole reason for the `.gitignore` entry.
- [ ] Sanity-check the control: add `import os` to any file in `tests/`, run `make validate-python`, confirm it FAILS with `F401`, then remove it.
- [ ] Confirm the tool check works: `RUFF=/nonexistent make validate-tools` exits 127 with the "run 'make deps-dev'" message.

## Merge

Standard workflow: commit, confirm clean tree, merge to `main`, push, delete the branch. No deploy step.

CI will pick ruff up automatically on the next push to `main`, because the workflow installs through `make deps-dev`. Watch that run — per `docs/deployment.md`, checking it needs the `gh` recipe with the quote-strip.
