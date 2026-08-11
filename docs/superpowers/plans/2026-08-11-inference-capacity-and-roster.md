# Inference Capacity and Roster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure what the RTX 4090 can actually hold, add an uncensored coding model and a vision model against rules written before the numbers, and record the roster in git as data so the hand-maintained capacity tables stop drifting.

**Architecture:** Three standalone Python scripts following the repo's existing
`scripts/abliteration_control.py` pattern — talk to Ollama over HTTP, no
third-party dependencies, exit codes carry the verdict. One YAML catalog under
`inventory/group_vars/all/` following the `infra-apps.yml` pattern, gated by one
offline validator wired into `make validate`. Every script carries a
`--self-check` case table proving its own logic still works, because this repo's
recurring failure is a check that returns clean because it never ran.

**Tech Stack:** Python 3 standard library only (`urllib`, `subprocess`, `json`,
`argparse`), PyYAML for the validator (already a repo dependency), Ollama HTTP
API, `nvidia-smi`.

**Spec:** [`docs/superpowers/specs/2026-08-11-inference-capacity-and-roster-design.md`](../specs/2026-08-11-inference-capacity-and-roster-design.md)

**Branch:** `docs/inference-capacity-roster` (already exists, spec committed)

## Global Constraints

- **No third-party imports in `scripts/`.** `abliteration_control.py` uses only
  the standard library and runs on TERRA, where the toolchain is minimal.
  Validators under `tests/` may use `yaml`, which is already required.
- **Three states, never two.** Every verdict is `MEASURED` / `SPILLED` /
  `INCONCLUSIVE` (or the equivalent). "Could not look" must never render as an
  all-clear. Same rule as the scan probes in `CLAUDE.md`.
- **Card total is 24564 MiB.** Idle abort threshold is **2560 MiB**.
- **Never `git add -A`.** Stage explicit paths. The repo root holds working
  notes that quote live credentials.
- **Never echo vault secrets** to terminal, logs, or a commit.
- **`make validate` must pass** before every commit that touches YAML, scripts
  or the Makefile. On Windows it fails at `validate-tools` (no ansible,
  shellcheck, gitleaks); there, run the specific Python validators directly and
  say so rather than claiming the full gate passed.
- **Context lengths under test:** 16384 and 32768.
- **Cache types under test:** `f16` (default), `q8_0`, `q4_0`.
- **`OLLAMA_KV_CACHE_TYPE` is server-global** and requires
  `OLLAMA_FLASH_ATTENTION=1`. Changing it means restarting Ollama.

---

### Task 1: The roster catalog and its gate

Establishes the data file and the validator before anything writes to it, so
later tasks have a schema to target. Pure offline work — needs no GPU.

**Files:**
- Create: `inventory/group_vars/all/models.yml`
- Create: `tests/validate_model_roster.py`
- Modify: `Makefile` (the `validate-catalog` target, around line 121)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `inventory/group_vars/all/models.yml` with top-level key `model_roster`, a
    list of mappings. Required keys per entry: `name` (str), `tier` (str, one of
    `terra` / `mbp`), `role` (str, one of `chat` / `code` / `vision` / `embed` /
    `autocomplete` / `baseline`), `abliterated` (bool), `why` (str, non-empty).
    Optional: `default` (bool), `alignment_exception` (str), `measured_mib`
    (int), `measured_on` (str, `YYYY-MM-DD`), `num_ctx` (int).
  - `tests/validate_model_roster.py` exposing `check_roster(roster: list[dict],
    control_roster: tuple[str, ...]) -> list[str]` returning a list of problem
    strings (empty means valid), and `main() -> int`.

- [ ] **Step 1: Write the failing self-check**

Create `tests/validate_model_roster.py` with only the case table and the
self-check driver. `check_roster` does not exist yet, so this fails at import
time — which is the point.

```python
#!/usr/bin/env python3
"""Validate the inference model roster's schema and its stated exceptions.

The roster spans two hosts and six roles, and until now lived as prose tables
in two documents that already disagreed with each other. This is the gate that
makes it data.

The rule that earns this file is the alignment exception. `qwen3-coder` being
un-abliterated and `qwen3:30b` being deliberately aligned are DECISIONS, and a
catalog that cannot tell a decision from an oversight is not worth keeping. So
every `abliterated: false` entry must say why, in writing.

It also cross-checks scripts/abliteration_control.py's ROSTER tuple against the
catalog. That script is the only thing that can detect a wrong or failed model,
and a model added to the catalog but not to the control is a model nobody is
checking.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROSTER_PATH = ROOT / "inventory/group_vars/all/models.yml"
CONTROL_PATH = ROOT / "scripts/abliteration_control.py"

CARD_TOTAL_MIB = 24564
TIERS = {"terra", "mbp"}
ROLES = {"chat", "code", "vision", "embed", "autocomplete", "baseline"}
REQUIRED = {"name", "tier", "role", "abliterated", "why"}
OPTIONAL = {"default", "alignment_exception", "measured_mib", "measured_on", "num_ctx"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ok_entry(**overrides) -> dict:
    """A minimal valid entry, for the self-check to mutate."""
    entry = {
        "name": "example/model:1b",
        "tier": "terra",
        "role": "chat",
        "abliterated": True,
        "why": "example",
        "default": True,
    }
    entry.update(overrides)
    return entry


# Each case is (description, roster, control_roster, substring that must appear
# in some problem). A case with `None` as the substring must produce NO
# problems. Without this table every rule below could be deleted and the gate
# would still pass everything, which is the exact failure this repo keeps
# hitting.
SELF_CHECK_CASES = (
    ("valid minimal roster",
     [_ok_entry()], ("example/model:1b",), None),
    ("missing required field",
     [{k: v for k, v in _ok_entry().items() if k != "why"}],
     ("example/model:1b",), "missing required"),
    ("unknown field (typo)",
     [_ok_entry(measured_mb=100)], ("example/model:1b",), "unknown field"),
    ("bad tier",
     [_ok_entry(tier="laptop")], ("example/model:1b",), "not a known tier"),
    ("bad role",
     [_ok_entry(role="therapy")], ("example/model:1b",), "not a known role"),
    ("duplicate name",
     [_ok_entry(), _ok_entry(default=False)],
     ("example/model:1b",), "appears twice"),
    ("no default chat model",
     [_ok_entry(default=False)], ("example/model:1b",), "exactly one"),
    ("two default chat models",
     [_ok_entry(), _ok_entry(name="other:1b", default=True)],
     ("example/model:1b", "other:1b"), "exactly one"),
    ("un-abliterated with no stated reason",
     [_ok_entry(abliterated=False)], (), "must carry `alignment_exception`"),
    ("un-abliterated with a stated reason is fine",
     [_ok_entry(abliterated=False, alignment_exception="deliberately aligned")],
     (), None),
    ("measurement with no date",
     [_ok_entry(measured_mib=20000)], ("example/model:1b",), "measured_on"),
    ("measured_mib left at the zero placeholder",
     [_ok_entry(measured_mib=0, measured_on="2026-08-11")],
     ("example/model:1b",), "not a measurement"),
    ("measurement larger than the card",
     [_ok_entry(measured_mib=30000, measured_on="2026-08-11")],
     ("example/model:1b",), "exceeds the card"),
    ("bad date format",
     [_ok_entry(measured_mib=20000, measured_on="11/08/2026")],
     ("example/model:1b",), "YYYY-MM-DD"),
    ("abliterated terra model missing from the control script",
     [_ok_entry()], (), "not in abliteration_control.py"),
    ("control script names a model not in the catalog",
     [_ok_entry()], ("example/model:1b", "ghost:7b"), "not in the catalog"),
)


def self_check() -> list[str]:
    """Prove each rule still fires. A gate must not be able to fail silently."""
    problems: list[str] = []
    for description, roster, control, expected in SELF_CHECK_CASES:
        got = check_roster(roster, control)
        if expected is None:
            if got:
                problems.append(
                    f"self-check {description!r}: expected no problems, got {got}")
        elif not any(expected in p for p in got):
            problems.append(
                f"self-check {description!r}: expected a problem containing "
                f"{expected!r}, got {got or 'no problems at all'} — the rule is "
                "not firing, so a real roster with this defect would pass")
    return problems


def main() -> int:
    failures = self_check()
    if failures:
        print("the validator's own self-check failed:", file=sys.stderr)
        for problem in failures:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("Model roster: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python tests/validate_model_roster.py`

Expected: `NameError: name 'check_roster' is not defined`.

- [ ] **Step 3: Implement `check_roster`**

Insert this function immediately above `SELF_CHECK_CASES` (it must be defined
before `self_check` runs, and the case table references only `_ok_entry`).

```python
def check_roster(roster: list[dict], control_roster: tuple[str, ...]) -> list[str]:
    """Return every problem with this roster. Empty list means valid."""
    problems: list[str] = []
    seen: set[str] = set()
    defaults: list[str] = []

    for entry in roster:
        name = entry.get("name", "<unnamed>")

        missing = REQUIRED - entry.keys()
        if missing:
            problems.append(f"{name}: missing required {sorted(missing)}")
        unknown = entry.keys() - REQUIRED - OPTIONAL
        if unknown:
            problems.append(
                f"{name}: unknown field {sorted(unknown)} — a typo here is "
                "silently ignored by everything that reads the catalog")

        if name in seen:
            problems.append(f"{name}: appears twice in the roster")
        seen.add(name)

        tier = entry.get("tier")
        if tier not in TIERS:
            problems.append(f"{name}: {tier!r} is not a known tier {sorted(TIERS)}")
        role = entry.get("role")
        if role not in ROLES:
            problems.append(f"{name}: {role!r} is not a known role {sorted(ROLES)}")

        if not str(entry.get("why", "")).strip():
            problems.append(f"{name}: `why` is empty — say what it is for")

        if entry.get("abliterated") is False and not str(
                entry.get("alignment_exception", "")).strip():
            problems.append(
                f"{name}: is not abliterated and must carry `alignment_exception` "
                "saying why. Every other chat model here is abliterated on "
                "purpose, so an un-abliterated one is either a decision or an "
                "oversight and the catalog has to say which")

        if entry.get("default"):
            defaults.append(name)

        mib = entry.get("measured_mib")
        if mib is not None:
            on = entry.get("measured_on")
            if not on:
                problems.append(
                    f"{name}: has measured_mib but no measured_on. An undated "
                    "measurement cannot be judged against the roster it was "
                    "taken with")
            elif not DATE_RE.match(str(on)):
                problems.append(f"{name}: measured_on {on!r} is not YYYY-MM-DD")
            if mib <= 0:
                problems.append(
                    f"{name}: measured_mib {mib} is not a measurement. A model "
                    "occupies memory, so zero means a placeholder was never "
                    "filled in from a survey run")
            if tier == "terra" and mib >= CARD_TOTAL_MIB:
                problems.append(
                    f"{name}: measured_mib {mib} exceeds the card ({CARD_TOTAL_MIB} "
                    "MiB), which is not a possible measurement")

    if len(defaults) != 1:
        problems.append(
            f"expected exactly one entry with `default: true`, found {defaults} "
            "— Open WebUI needs one and only one default")

    should_control = {
        e["name"] for e in roster
        if e.get("tier") == "terra" and e.get("abliterated") is True
        and e.get("role") in {"chat", "code"}
    }
    for name in sorted(should_control - set(control_roster)):
        problems.append(
            f"{name}: abliterated but not in abliteration_control.py's ROSTER — "
            "nothing would ever verify it is actually uncensored")
    for name in sorted(set(control_roster) - should_control):
        problems.append(
            f"{name}: in abliteration_control.py's ROSTER but not in the catalog "
            "as an abliterated terra chat/code model — the control would fail "
            "against a model nobody declared")
    return problems
```

- [ ] **Step 4: Run it to verify the self-check passes**

Run: `python tests/validate_model_roster.py`

Expected: `Model roster: OK`.

- [ ] **Step 5: Make it read the real files**

`main()` currently only self-checks. Add loaders and wire them in. Replace the
existing `main()` with:

```python
def load_roster() -> list[dict]:
    data = yaml.safe_load(ROSTER_PATH.read_text(encoding="utf-8")) or {}
    roster = data.get("model_roster")
    if not isinstance(roster, list) or not roster:
        raise SystemExit(
            f"{ROSTER_PATH}: no `model_roster` list. Refusing to report a clean "
            "run against a catalog that could not be read")
    return roster


def load_control_roster() -> tuple[str, ...]:
    """Read the ROSTER tuple out of abliteration_control.py without importing it.

    Importing would be simpler but that script talks to the network at import
    time in future revisions; parsing the literal keeps this gate offline.
    """
    tree = ast.parse(CONTROL_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ROSTER" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise SystemExit(
        f"{CONTROL_PATH}: no ROSTER assignment found. It was renamed or removed, "
        "and this cross-check silently stopped covering anything")


def main() -> int:
    failures = self_check()
    if failures:
        print("the validator's own self-check failed:", file=sys.stderr)
        for problem in failures:
            print(f"  {problem}", file=sys.stderr)
        return 1

    problems = check_roster(load_roster(), load_control_roster())
    if problems:
        print(f"{ROSTER_PATH.name}: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("Model roster: OK")
    return 0
```

- [ ] **Step 6: Run it to verify it fails on the missing catalog**

Run: `python tests/validate_model_roster.py`

Expected: `FileNotFoundError` for `inventory/group_vars/all/models.yml`.

- [ ] **Step 7: Write the catalog**

Create `inventory/group_vars/all/models.yml`. Measurements are transcribed from
the existing tables in `docs/gpu-host.md` and carry their original dates — Task
4 replaces them with survey output.

```yaml
---
# Inference model roster, by host tier.
#
# This file is the catalog; docs/gpu-capacity.md is the measurement report
# generated by scripts/vram_survey.py + scripts/vram_report.py. Do not hand-edit
# measured_mib — take it from a survey run and record the date it was taken.
#
# `abliterated: false` REQUIRES `alignment_exception` saying why, because every
# chat model here is abliterated on purpose and an un-abliterated one is either
# a decision or a mistake. tests/validate_model_roster.py enforces that, and
# also cross-checks that every abliterated terra model appears in
# scripts/abliteration_control.py's ROSTER.
#
# tier: terra = the RTX 4090 desktop, on-demand, one model at a time.
#       mbp   = the M1 Pro laptop, always-on, small models. NOT YET PROVISIONED.
model_roster:
  - name: huihui_ai/gemma-4-abliterated:26b
    tier: terra
    role: chat
    default: true
    abliterated: true
    measured_mib: 20339
    measured_on: "2026-08-09"
    num_ctx: 32768
    why: >-
      Warmest prose in the roster, which is why it carries the Thera and
      Unfiltered personas.

  - name: huihui_ai/Qwen3.6-abliterated:27b
    tier: terra
    role: chat
    abliterated: true
    measured_mib: 20411
    measured_on: "2026-08-09"
    num_ctx: 32768
    why: Technical and agentic work.

  - name: davidau-fable-fusion:27b-q4km
    tier: terra
    role: chat
    abliterated: true
    measured_mib: 20800
    measured_on: "2026-08-09"
    num_ctx: 32768
    why: >-
      Creative writing and roleplay. Registered from a local blob because the
      upstream pull fails on a 30s deadline - see docs/gpu-host.md.

  - name: huihui_ai/gemma-4-abliterated:31b
    tier: terra
    role: chat
    abliterated: true
    measured_mib: 23465
    measured_on: "2026-08-10"
    num_ctx: 16384
    why: >-
      Dense variant, stronger reasoning. num_ctx is capped at 16384 because the
      KV cache - not the weights - pushes it to CPU spill at 32768.

  - name: qwen3-coder:30b
    tier: terra
    role: code
    abliterated: false
    alignment_exception: >-
      Coding models rarely refuse, so abliteration buys almost nothing while
      costing measurable quality. Deliberately stock weights.
    measured_mib: 22634
    measured_on: "2026-08-09"
    num_ctx: 32768
    why: Continue's chat/edit/apply model.

  - name: qwen3:30b
    tier: terra
    role: baseline
    abliterated: false
    alignment_exception: >-
      Deliberately aligned. It is the only aligned model left on the host and
      therefore the only thing that can calibrate the abliteration control -
      every other chat model answers everything by design.
    measured_mib: 22800
    measured_on: "2026-08-08"
    num_ctx: 32768
    why: Positive control baseline for scripts/abliteration_control.py.

  - name: qwen2.5-coder:1.5b-base
    tier: terra
    role: autocomplete
    abliterated: false
    alignment_exception: >-
      A base completion model, not an instruct model. Refusal behaviour does not
      apply to it.
    why: Continue's autocomplete. Never measured; the survey will fill it in.

  - name: nomic-embed-text
    tier: terra
    role: embed
    abliterated: false
    alignment_exception: >-
      An embedding model. It does not generate text, so there is nothing to
      abliterate.
    why: >-
      Continue's embeddings. Moves to the mbp tier once that host exists, since
      RAG embedding must not depend on a desktop that gets gamed on.
```

- [ ] **Step 8: Run it to verify the catalog passes**

Run: `python tests/validate_model_roster.py`

Expected: `Model roster: OK`.

If it reports the 31b at 23465 exceeding the card — it does not, 23465 < 24564.
If it reports a control mismatch, the four `abliterated: true` names must match
`ROSTER` in `scripts/abliteration_control.py` exactly, including case.

- [ ] **Step 9: Wire it into `make validate`**

In `Makefile`, add the new validator to the `validate-catalog` target after
`validate_release_overrides.py`:

```makefile
	$(PYTHON) tests/validate_release_overrides.py
# The roster is the only description of which models exist on which host. It
# sits with the catalog gates because it validates the same kind of artifact:
# a data file this repo owns and other things read verbatim.
	$(PYTHON) tests/validate_model_roster.py
```

- [ ] **Step 10: Verify the gate runs from the Makefile**

Run: `make validate-catalog`

Expected: every catalog validator prints OK, ending with `Model roster: OK`.

On Windows `make` may be unavailable; then run each line of the target by hand
and say that is what you did.

- [ ] **Step 11: Commit**

```bash
git add inventory/group_vars/all/models.yml tests/validate_model_roster.py Makefile
git commit -m "feat: make the model roster data instead of two prose tables"
```

---

### Task 2: The capacity survey script

Measures one cache-type pass. Pure logic is self-checkable anywhere; the
hardware paths only run on TERRA.

**Files:**
- Create: `scripts/vram_survey.py`

**Interfaces:**
- Consumes: `inventory/group_vars/all/models.yml` is *not* read by this script —
  models are passed on the command line, so the survey can measure candidates
  that are not yet in the catalog.
- Produces: `scripts/vram_survey.py` writing a JSON pass file with shape
  `{"cache_type": str, "flash_attention": bool, "baseline_mib": int, "taken":
  "YYYY-MM-DD", "rows": [{"model": str, "num_ctx": int, "verdict": str,
  "card_used_mib": int | None, "size_total": int | None, "size_vram": int | None,
  "evidence": str}]}`. Exposes `classify(size_total, size_vram) -> tuple[str,
  str]` and `baseline_verdict(mib) -> tuple[bool, str]`.

- [ ] **Step 1: Write the failing self-check**

Create `scripts/vram_survey.py` with the header, constants and self-check only.

```python
#!/usr/bin/env python3
"""Measure what each model actually costs on the GPU host, one cache type/pass.

The 4090 is 24 GB with no swap. Exceeding it does not fail - Ollama spills
layers to system RAM and generation slows by roughly an order of magnitude, with
no error and no log line. `ollama ps` is the only place it shows. This script is
the thing that goes and looks.

    scripts/vram_survey.py --out pass-f16.json MODEL [MODEL ...]
    scripts/vram_survey.py --self-check

One pass measures one cache type, because OLLAMA_KV_CACHE_TYPE is a SERVER-WIDE
environment variable with no per-model override. Three passes means restarting
Ollama twice:

    f16    (default; unset OLLAMA_KV_CACHE_TYPE)
    q8_0   (with OLLAMA_FLASH_ATTENTION=1)
    q4_0   (with OLLAMA_FLASH_ATTENTION=1)

scripts/vram_report.py merges the passes and is the thing that detects the
silent fallback - see its docstring.

Exit codes:
    0  every requested measurement produced a verdict
    1  the card was not idle, or a measurement was INCONCLUSIVE
    2  bad arguments
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "http://localhost:11434"
CARD_TOTAL_MIB = 24564

# docs/gpu-host.md records ~2.5 GB idle; docs/chat-models.md records a 1920 MiB
# idle baseline on a clean card. 2560 sits above the observed idle with a
# workable margin and below anything a game would be holding.
IDLE_ABORT_MIB = 2560

# Short and deterministic. The point is to force the context cache to be
# genuinely allocated rather than merely reserved, and to prove the model can
# actually run at this context - a model that loads can still fail at 32768.
PROBE_PROMPT = "Reply with the single word: ready."

CONTEXTS = (16384, 32768)


def classify(size_total: int | None, size_vram: int | None) -> tuple[str, str]:
    """Three states, never two. Returns (verdict, evidence)."""
    if size_total is None or size_vram is None:
        return "INCONCLUSIVE", (
            "Ollama's /api/ps did not report both `size` and `size_vram`, so the "
            "CPU/GPU split cannot be read programmatically on this build. Fall "
            "back to parsing the PROCESSOR column of `ollama ps` by hand and "
            "record which you used")
    if size_total <= 0:
        return "INCONCLUSIVE", f"nonsense size {size_total}"
    if size_vram >= size_total:
        return "MEASURED", f"{size_vram} of {size_total} bytes resident on GPU (100%)"
    pct = 100.0 * size_vram / size_total
    return "SPILLED", (
        f"only {pct:.0f}% on GPU ({size_vram} of {size_total} bytes); the "
        "remainder is in system RAM and generation is roughly an order of "
        "magnitude slower")


def baseline_verdict(mib: int) -> tuple[bool, str]:
    """Refuse to survey a card that is not idle."""
    if mib > IDLE_ABORT_MIB:
        return False, (
            f"{mib} MiB already in use, above the {IDLE_ABORT_MIB} MiB idle "
            "threshold. Something else is holding the card - close it and "
            "re-run. The first version of the table in docs/gpu-host.md was "
            "taken with a game resident and was invalid on its face")
    return True, f"{mib} MiB baseline, card is idle"


# Each case is (description, args, expected verdict prefix). Without this the
# classify() rules could be inverted and every run would still print numbers
# that look entirely plausible.
CLASSIFY_CASES = (
    ("fully resident", (21_000_000_000, 21_000_000_000), "MEASURED"),
    ("more vram than total is still fine", (20_000_000_000, 20_000_000_001), "MEASURED"),
    ("spilled to CPU", (23_000_000_000, 20_700_000_000), "SPILLED"),
    ("size_vram missing", (21_000_000_000, None), "INCONCLUSIVE"),
    ("size missing", (None, 21_000_000_000), "INCONCLUSIVE"),
    ("both missing", (None, None), "INCONCLUSIVE"),
    ("nonsense total", (0, 0), "INCONCLUSIVE"),
)

BASELINE_CASES = (
    ("clean card", 1920, True),
    ("exactly at threshold", IDLE_ABORT_MIB, True),
    ("one MiB over", IDLE_ABORT_MIB + 1, False),
    ("a game is resident", 9000, False),
)


def self_check() -> list[str]:
    problems: list[str] = []
    for description, (total, vram), expected in CLASSIFY_CASES:
        got, _ = classify(total, vram)
        if got != expected:
            problems.append(
                f"classify {description!r}: got {got}, expected {expected} — the "
                "verdict logic is wrong, so the survey would report confident "
                "numbers that mean the opposite of what they say")
    for description, mib, expected in BASELINE_CASES:
        got, _ = baseline_verdict(mib)
        if got != expected:
            problems.append(
                f"baseline {description!r}: got {got}, expected {expected} — the "
                "idle gate is broken and a survey taken against a busy card "
                "would be accepted")
    return problems
```

- [ ] **Step 2: Add the self-check entrypoint and run it**

Append to the file:

```python
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("models", nargs="*", help="models to measure")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Ollama base URL (default {DEFAULT_HOST})")
    parser.add_argument("--out", help="write the pass JSON here")
    parser.add_argument("--cache-type", default="f16",
                        choices=("f16", "q8_0", "q4_0"),
                        help="what OLLAMA_KV_CACHE_TYPE is set to for this pass")
    parser.add_argument("--flash-attention", action="store_true",
                        help="record that OLLAMA_FLASH_ATTENTION=1 was set")
    parser.add_argument("--embed-only", action="append", default=[],
                        metavar="MODEL",
                        help="measure this model load-only, no generation and no "
                             "context sweep (embedding models have no /api/generate)")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="per-request timeout in seconds (default 1800)")
    parser.add_argument("--self-check", action="store_true",
                        help="prove this script's own logic still works, then exit")
    args = parser.parse_args()

    if args.self_check:
        problems = self_check()
        for problem in problems:
            print(problem, file=sys.stderr)
        print("self-check: OK" if not problems else "self-check: FAILED")
        return 1 if problems else 0

    if not args.models and not args.embed_only:
        parser.print_usage(sys.stderr)
        print("give at least one model, or --embed-only MODEL", file=sys.stderr)
        return 2
    if not args.out:
        parser.print_usage(sys.stderr)
        print("--out is required; a survey nobody wrote down is not a survey",
              file=sys.stderr)
        return 2
    return run_survey(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

Run: `python scripts/vram_survey.py --self-check`

Expected: `self-check: OK`.

- [ ] **Step 3: Verify the self-check can actually fail**

Temporarily invert one rule — change `if size_vram >= size_total:` to
`if size_vram > size_total:` — and re-run.

Run: `python scripts/vram_survey.py --self-check`

Expected: FAILS, naming `classify 'fully resident'`. **Revert the change.** A
self-check you have never seen fail is a self-check you cannot trust.

- [ ] **Step 4: Implement the measurement paths**

Insert above `main()`:

```python
def nvidia_smi_used_mib() -> int:
    """Whole-card usage, which is the only figure that includes other processes."""
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True)
    return int(out.stdout.strip().splitlines()[0])


def _post(host: str, path: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        f"{host.rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _get(host: str, path: str, timeout: int) -> dict:
    with urllib.request.urlopen(f"{host.rstrip('/')}{path}", timeout=timeout) as resp:
        return json.load(resp)


def load_and_probe(host: str, model: str, num_ctx: int | None, timeout: int) -> None:
    payload: dict = {"model": model, "prompt": PROBE_PROMPT, "stream": False,
                     "options": {"num_predict": 16}}
    if num_ctx is not None:
        payload["options"]["num_ctx"] = num_ctx
    _post(host, "/api/generate", payload, timeout)


def load_embed(host: str, model: str, timeout: int) -> None:
    _post(host, "/api/embed", {"model": model, "input": "probe"}, timeout)


def ps_entry(host: str, model: str, timeout: int) -> dict | None:
    for entry in _get(host, "/api/ps", timeout).get("models", []):
        if entry.get("name") == model or entry.get("model") == model:
            return entry
    return None


def unload(host: str, model: str, timeout: int) -> None:
    """keep_alive 0 evicts immediately. Without this every measurement inherits
    the previous model's residency and the numbers are cumulative nonsense."""
    try:
        _post(host, "/api/generate", {"model": model, "keep_alive": 0}, timeout)
    except Exception:
        pass
    time.sleep(3)


def measure(host: str, model: str, num_ctx: int | None, timeout: int,
            embed: bool) -> dict:
    row: dict = {"model": model, "num_ctx": num_ctx}
    try:
        if embed:
            load_embed(host, model, timeout)
        else:
            load_and_probe(host, model, num_ctx, timeout)
    except urllib.error.HTTPError as exc:
        row.update(verdict="INCONCLUSIVE", card_used_mib=None, size_total=None,
                   size_vram=None,
                   evidence=f"HTTP {exc.code} loading it: {exc.read()[:200]!r}")
        # Unload even on the error path. A load that failed partway can leave the
        # model resident, and the next measurement would then include it — every
        # subsequent row in the pass would be silently inflated.
        unload(host, model, timeout)
        return row
    except Exception as exc:
        row.update(verdict="INCONCLUSIVE", card_used_mib=None, size_total=None,
                   size_vram=None, evidence=f"{type(exc).__name__}: {exc}")
        unload(host, model, timeout)
        return row

    entry = ps_entry(host, model, timeout) or {}
    size_total = entry.get("size")
    size_vram = entry.get("size_vram")
    state, evidence = classify(size_total, size_vram)
    row.update(verdict=state, card_used_mib=nvidia_smi_used_mib(),
               size_total=size_total, size_vram=size_vram, evidence=evidence)
    unload(host, model, timeout)
    return row


def run_survey(args) -> int:
    baseline = nvidia_smi_used_mib()
    ok, evidence = baseline_verdict(baseline)
    print(f"baseline: {evidence}")
    if not ok:
        return 1

    rows: list[dict] = []
    for model in args.models:
        for num_ctx in CONTEXTS:
            print(f"  measuring {model} @ {num_ctx} ...", flush=True)
            rows.append(measure(args.host, model, num_ctx, args.timeout, embed=False))
    for model in args.embed_only:
        print(f"  measuring {model} (embed, load-only) ...", flush=True)
        rows.append(measure(args.host, model, None, args.timeout, embed=True))

    payload = {
        "cache_type": args.cache_type,
        "flash_attention": args.flash_attention,
        "baseline_mib": baseline,
        "taken": datetime.date.today().isoformat(),
        "rows": rows,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")

    print()
    for row in rows:
        ctx = row["num_ctx"] if row["num_ctx"] is not None else "n/a"
        print(f"{row['verdict']:<14} {row['model']} @ {ctx}")
        print(f"{'':<14} {row['evidence']}")
    inconclusive = [r for r in rows if r["verdict"] == "INCONCLUSIVE"]
    print(f"\n{len(rows)} measurements written to {args.out}")
    if inconclusive:
        print(f"\n{len(inconclusive)} INCONCLUSIVE - this pass is not complete:",
              file=sys.stderr)
        for row in inconclusive:
            print(f"  {row['model']} @ {row['num_ctx']}: {row['evidence']}",
                  file=sys.stderr)
        return 1
    return 0
```

- [ ] **Step 5: Verify the self-check still passes**

Run: `python scripts/vram_survey.py --self-check`

Expected: `self-check: OK`.

- [ ] **Step 6: Verify it refuses to run without `--out`**

Run: `python scripts/vram_survey.py some-model`

Expected: exit 2, `--out is required`.

- [ ] **Step 7: Commit**

```bash
git add scripts/vram_survey.py
git commit -m "feat: add the GPU capacity survey"
```

---

### Task 3: The report generator and the fallback detector

The survey measures. This merges passes and answers the question the survey
cannot answer alone: *did the cache setting do anything at all?*

**Files:**
- Create: `scripts/vram_report.py`

**Interfaces:**
- Consumes: pass JSON files written by `scripts/vram_survey.py` (Task 2).
- Produces: `scripts/vram_report.py` exposing `detect_fallback(f16_mib: int |
  None, quant_mib: int | None, margin: int = 100) -> tuple[str, str]` and
  `render(passes: list[dict]) -> str`, plus a CLI writing `docs/gpu-capacity.md`.

- [ ] **Step 1: Write the failing self-check**

Create `scripts/vram_report.py`:

```python
#!/usr/bin/env python3
"""Merge capacity survey passes into docs/gpu-capacity.md, and catch the lie.

Upstream documents that OLLAMA_KV_CACHE_TYPE falls back to f16 WITHOUT TELLING
YOU on architectures that do not support a quantized cache. The variable is set,
the service restarts, models load and answer, and nothing changed. No error, no
log line - which is the exact failure mode this repo writes warnings about
everywhere else.

It comes with its own detector, though. If compression took effect, the same
model at the same context MUST use measurably less card memory than the f16
pass. If the number does not move, the setting did nothing, and this reports
FALLBACK rather than a number. That is why the report is a separate script from
the survey: the detection is only possible across passes.

    scripts/vram_report.py --out docs/gpu-capacity.md pass-f16.json pass-q8_0.json
    scripts/vram_report.py --self-check

Exit codes:
    0  report written
    1  a pass could not be read, or the f16 pass was missing
    2  bad arguments
"""

from __future__ import annotations

import argparse
import json
import sys

# Measurement noise on an otherwise idle card is tens of MiB. 100 MiB is
# comfortably above that and far below the GB-scale saving a working q8_0 pass
# produces, so it separates "did nothing" from "did something" without being
# sensitive to jitter.
FALLBACK_MARGIN_MIB = 100


def detect_fallback(f16_mib: int | None, quant_mib: int | None,
                    margin: int = FALLBACK_MARGIN_MIB) -> tuple[str, str]:
    """Did the quantized cache actually take effect? Three states, never two."""
    if f16_mib is None or quant_mib is None:
        return "UNKNOWN", "one of the two passes has no measurement to compare"
    saved = f16_mib - quant_mib
    if saved >= margin:
        return "APPLIED", f"{saved} MiB saved against f16"
    if saved <= -margin:
        return "UNKNOWN", (
            f"the quantized pass used {-saved} MiB MORE than f16, which is not a "
            "thing compression does - re-measure on an idle card")
    return "FALLBACK", (
        f"only {saved} MiB different from f16 - the setting silently did nothing "
        "on this architecture and the cache is still f16")


# Without this table the detector could be inverted and every report would
# cheerfully claim compression worked.
FALLBACK_CASES = (
    ("q8_0 halved the cache", 23465, 21000, "APPLIED"),
    ("saving exactly at the margin", 20100, 20000, "APPLIED"),
    ("saving one MiB short of the margin", 20099, 20000, "FALLBACK"),
    ("identical - the classic silent fallback", 20339, 20339, "FALLBACK"),
    ("noise in the wrong direction", 20339, 20350, "FALLBACK"),
    ("impossibly worse", 20000, 21000, "UNKNOWN"),
    ("no f16 measurement", None, 20000, "UNKNOWN"),
    ("no quantized measurement", 20000, None, "UNKNOWN"),
)


def self_check() -> list[str]:
    problems: list[str] = []
    for description, f16, quant, expected in FALLBACK_CASES:
        got, _ = detect_fallback(f16, quant)
        if got != expected:
            problems.append(
                f"detect_fallback {description!r}: got {got}, expected {expected} "
                "— the detector is broken, so a setting that silently did "
                "nothing would be reported as a working saving")
    return problems
```

- [ ] **Step 2: Add the entrypoint and run the self-check**

Append:

```python
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("passes", nargs="*", help="pass JSON files from vram_survey.py")
    parser.add_argument("--out", help="write the markdown report here")
    parser.add_argument("--self-check", action="store_true",
                        help="prove the fallback detector still works, then exit")
    args = parser.parse_args()

    if args.self_check:
        problems = self_check()
        for problem in problems:
            print(problem, file=sys.stderr)
        print("self-check: OK" if not problems else "self-check: FAILED")
        return 1 if problems else 0

    if not args.passes or not args.out:
        parser.print_usage(sys.stderr)
        print("give at least one pass file and --out", file=sys.stderr)
        return 2

    passes = []
    for path in args.passes:
        with open(path, encoding="utf-8") as handle:
            passes.append(json.load(handle))
    if not any(p["cache_type"] == "f16" for p in passes):
        print("no f16 pass given. Without the baseline nothing can be compared "
              "and the fallback detection cannot run", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(render(passes))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run: `python scripts/vram_report.py --self-check`

Expected: `self-check: OK`.

- [ ] **Step 3: Verify the self-check can fail**

Temporarily change `if saved >= margin:` to `if saved >= -margin:` and re-run.

Run: `python scripts/vram_report.py --self-check`

Expected: FAILS on `'identical - the classic silent fallback'`. **Revert.**

- [ ] **Step 4: Implement `render`**

Insert above `main()`:

```python
def _key(row: dict) -> tuple:
    return (row["model"], row["num_ctx"])


def render(passes: list[dict]) -> str:
    base = next(p for p in passes if p["cache_type"] == "f16")
    base_rows = {_key(r): r for r in base["rows"]}
    others = [p for p in passes if p["cache_type"] != "f16"]

    lines = [
        "# GPU capacity, measured",
        "",
        "**Generated by `scripts/vram_report.py`. Do not hand-edit.**",
        "",
        "This file replaces the hand-maintained tables that used to live in",
        "`docs/gpu-host.md` and `docs/chat-models.md` and had already drifted",
        "apart from each other.",
        "",
        "A model's download size is not its VRAM footprint. Budget from the",
        "resident figure here, never from the tag.",
        "",
    ]
    for entry in passes:
        lines.append(
            f"- **{entry['cache_type']}** pass taken {entry['taken']}, idle "
            f"baseline {entry['baseline_mib']} MiB, flash attention "
            f"{'on' if entry['flash_attention'] else 'off'}")
    lines += [
        "",
        "The idle baseline matters: a survey taken while something else held the",
        "card is invalid on its face, which is how the first version of this",
        "table was produced.",
        "",
        "## Fit, by cache type",
        "",
        "| Model | Context | f16 | " + " | ".join(p["cache_type"] for p in others)
        + " |",
        "|---|---|---|" + "---|" * len(others),
    ]

    for key in sorted(base_rows):
        model, num_ctx = key
        row = base_rows[key]
        ctx = num_ctx if num_ctx is not None else "n/a"
        used = row["card_used_mib"]
        cells = [f"{row['verdict']} ({used} MiB)" if used else row["verdict"]]
        for entry in others:
            match = {_key(r): r for r in entry["rows"]}.get(key)
            if match is None:
                cells.append("not measured")
                continue
            state, _ = detect_fallback(used, match["card_used_mib"])
            cell = f"{match['verdict']}"
            if match["card_used_mib"]:
                cell += f" ({match['card_used_mib']} MiB)"
            if state == "FALLBACK":
                cell += " ⚠️ **FALLBACK**"
            cells.append(cell)
        lines.append(f"| `{model}` | {ctx} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "`FALLBACK` means the quantized cache setting silently did nothing on",
        "this architecture — the memory did not move. It is not an error state",
        "you can see any other way.",
        "",
        "## What this cannot tell you",
        "",
        "It measures whether a model **fits**. It says nothing about whether a",
        "compressed cache made the model **worse**. Memory is easy to measure and",
        "quality is not. Do not quote this file as evidence about output quality.",
        "",
    ]
    return "\n".join(lines)
```

- [ ] **Step 5: Verify against synthetic passes**

Create two throwaway pass files in the scratch directory and render them, to
prove the renderer works before a real survey depends on it.

```bash
mkdir -p /tmp/vram && cat > /tmp/vram/f16.json <<'EOF'
{"cache_type":"f16","flash_attention":false,"baseline_mib":1920,"taken":"2026-08-11",
 "rows":[{"model":"a:1b","num_ctx":32768,"verdict":"SPILLED","card_used_mib":23626,
          "size_total":100,"size_vram":90,"evidence":"x"}]}
EOF
cat > /tmp/vram/q8.json <<'EOF'
{"cache_type":"q8_0","flash_attention":true,"baseline_mib":1920,"taken":"2026-08-11",
 "rows":[{"model":"a:1b","num_ctx":32768,"verdict":"MEASURED","card_used_mib":23626,
          "size_total":100,"size_vram":100,"evidence":"x"}]}
EOF
python scripts/vram_report.py --out /tmp/vram/out.md /tmp/vram/f16.json /tmp/vram/q8.json
cat /tmp/vram/out.md
```

Expected: the q8_0 cell reads `MEASURED (23626 MiB) ⚠️ **FALLBACK**` — identical
memory means the setting did nothing, and the report says so even though the
verdict improved. That is the whole point of the detector.

- [ ] **Step 6: Verify it refuses to run without an f16 pass**

Run: `python scripts/vram_report.py --out /tmp/vram/out.md /tmp/vram/q8.json`

Expected: exit 1, `no f16 pass given`.

- [ ] **Step 7: Commit**

```bash
git add scripts/vram_report.py
git commit -m "feat: merge capacity passes and catch the silent cache fallback"
```

---

### Task 4: Run the survey on TERRA and replace the prose tables

**This task runs on TERRA.** It produces the measurements everything else is
judged against.

**Files:**
- Create: `docs/gpu-capacity.md` (generated)
- Modify: `docs/gpu-host.md` (replace the "What actually fits on the card" table)
- Modify: `docs/chat-models.md` (replace the roster table's measurement columns)
- Modify: `inventory/group_vars/all/models.yml` (refresh `measured_mib` / `measured_on`)

**Interfaces:**
- Consumes: `scripts/vram_survey.py` and `scripts/vram_report.py` (Tasks 2, 3).
- Produces: `docs/gpu-capacity.md`, and measured values in the catalog that Task
  5's pass/fail rules are applied against.

- [ ] **Step 1: Confirm Python and an idle card on TERRA**

`docs/gpu-host.md` says there is no Python on TERRA's `PATH`; the working notes
record 11 validators running there after `pip install --user`. Settle it before
starting, not during.

```powershell
python --version
nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits
```

Expected: a Python 3 version, and a figure at or below ~1920 MiB. If it is
higher, close whatever is holding the card. If Python is missing, install it —
do not attempt the survey by hand.

- [ ] **Step 2: Take the f16 pass**

`OLLAMA_KV_CACHE_TYPE` must be **unset** for this pass. Confirm, then run:

```powershell
python scripts\vram_survey.py --out pass-f16.json --cache-type f16 `
  --embed-only nomic-embed-text `
  "huihui_ai/gemma-4-abliterated:26b" `
  "huihui_ai/Qwen3.6-abliterated:27b" `
  "davidau-fable-fusion:27b-q4km" `
  "huihui_ai/gemma-4-abliterated:31b" `
  "qwen3-coder:30b" `
  "qwen3:30b" `
  "qwen2.5-coder:1.5b-base"
```

Expected: 7 models × 2 contexts + 1 embed = **15 rows**, no `INCONCLUSIVE`.

If every row is `INCONCLUSIVE` naming `size_vram`, this Ollama build does not
report the split via `/api/ps`. Record that, and fall back to reading the
`PROCESSOR` column of `ollama ps` for each measurement — the spec anticipated
this and it is a finding, not a failure.

- [ ] **Step 3: Restart Ollama with q8_0 and take the second pass**

Set both variables as **System** variables (not shell — Ollama runs as a
background service and will not see a shell variable, the same trap
`OLLAMA_HOST` already documents in `docs/gpu-host.md`), then restart Ollama.

```
OLLAMA_FLASH_ATTENTION = 1
OLLAMA_KV_CACHE_TYPE   = q8_0
```

Then re-run the Step 2 command with `--out pass-q8_0.json --cache-type q8_0
--flash-attention`.

- [ ] **Step 4: Repeat for q4_0**

Set `OLLAMA_KV_CACHE_TYPE = q4_0`, restart Ollama, re-run with
`--out pass-q4_0.json --cache-type q4_0 --flash-attention`.

- [ ] **Step 5: Generate the report**

```powershell
python scripts\vram_report.py --out docs\gpu-capacity.md `
  pass-f16.json pass-q8_0.json pass-q4_0.json
```

Read it. **The decision this makes:** if the q8_0 column shows real savings
across the board, quantized cache becomes the host default and the 31b's
`num_ctx` cap can be lifted. If it shows `FALLBACK`, the setting does nothing on
this card and the existing 16384 cap stands — record that, because it closes the
question permanently rather than leaving it to be re-asked.

- [ ] **Step 6: Refresh the catalog measurements**

Update `measured_mib` and `measured_on` in
`inventory/group_vars/all/models.yml` from the f16 column (the host's actual
running configuration unless Step 5 changed it), and fill in the two entries
that had no measurement: `qwen2.5-coder:1.5b-base` and `nomic-embed-text`.

If the survey changed the host default to q8_0, say so in the `why` of the 31b
entry and lift its `num_ctx` to 32768.

Run: `python tests/validate_model_roster.py`

Expected: `Model roster: OK`.

- [ ] **Step 7: Delete the duplicated tables**

In `docs/gpu-host.md`, replace the table under **"What actually fits on the
card"** with a link to `gpu-capacity.md`, keeping the two prose lessons beneath
it ("record the idle baseline"; "a model that spills is not necessarily too
big") — those are reasoning, not data, and are not generated.

In `docs/chat-models.md`, drop the `Resident` and `Fits card?` columns from the
roster table and link to `gpu-capacity.md` for them. Keep the `Role` column.

The point is not to reconcile the two tables. It is to remove the duplication so
there is one place to be wrong.

- [ ] **Step 8: Verify links still resolve**

Run: `python tests/validate_links.py`

Expected: `Local Markdown links: OK`.

- [ ] **Step 9: Commit**

```bash
git add docs/gpu-capacity.md docs/gpu-host.md docs/chat-models.md \
        inventory/group_vars/all/models.yml
git commit -m "feat: measure the card instead of describing it in two places"
```

Do **not** commit the `pass-*.json` files — they are working artifacts, and the
repo root is gitignore-sensitive. Confirm with `git status --porcelain` that
nothing else is staged.

---

### Task 5: The two roster additions

**This task runs on TERRA.** Rules from the spec, applied to measurements from
Task 4.

**Files:**
- Create: `scripts/vision_control.py`
- Create: `tests/fixtures/vision-probe.png`
- Modify: `scripts/abliteration_control.py` (the `ROSTER` tuple, line 54-59)
- Modify: `inventory/group_vars/all/models.yml`

**Interfaces:**
- Consumes: `scripts/vram_survey.py` (Task 2), the catalog schema (Task 1).
- Produces: `scripts/vision_control.py` exposing `verdict(response: str,
  expected: str) -> tuple[str, str]` and a CLI; `ROSTER` in
  `abliteration_control.py` extended to five entries.

**The rules, restated so they are not re-derived:**

| Model | Add it only if |
|---|---|
| `huihui_ai/qwen3-coder-abliterated:30b-a3b-instruct-q4_K_M` | `MEASURED` (100% GPU) at 16384 or better, **and** passes the abliteration control |
| `huihui_ai/qwen3-vl-abliterated:8b` | `MEASURED`, **and** reads known text off a real image |

A rejected model gets its reason written down, the way `aratan` did. A rejection
with a recorded reason is more useful than a silent absence.

- [ ] **Step 1: Pull and measure both candidates**

```powershell
ollama pull huihui_ai/qwen3-coder-abliterated:30b-a3b-instruct-q4_K_M
ollama pull huihui_ai/qwen3-vl-abliterated:8b
python scripts\vram_survey.py --out pass-candidates.json --cache-type f16 `
  "huihui_ai/qwen3-coder-abliterated:30b-a3b-instruct-q4_K_M" `
  "huihui_ai/qwen3-vl-abliterated:8b"
```

Confirm the exact vision tag exists first — `ollama show huihui_ai/qwen3-vl-abliterated:8b`.
If `:8b` is not a published tag, list the repo's tags and pick the 8B build;
**do not substitute a 32B**, which would consume the whole card to do the same
document-reading job.

Expected: the coder `MEASURED` at 16384. If it reads `SPILLED` at both contexts,
it is rejected — skip to Step 8 and record why.

- [ ] **Step 2: Create the vision probe fixture**

The image must contain text whose answer is known in advance. Pillow is a
one-time convenience here, not a repo dependency.

```powershell
pip install --user pillow
python -c "from PIL import Image, ImageDraw; i=Image.new('RGB',(640,200),'white'); ImageDraw.Draw(i).text((40,80),'HOMELAB-VISION-OK-4711',fill='black'); i.save('tests/fixtures/vision-probe.png')"
```

Open it and confirm the string is legible. An unreadable fixture makes a working
model look broken.

- [ ] **Step 3: Write the vision control's failing self-check**

Create `scripts/vision_control.py`:

```python
#!/usr/bin/env python3
"""Positive control for the vision model. It must actually see.

A vision model whose image half did not load answers TEXT questions perfectly.
It looks entirely healthy, `ollama ps` shows it resident, and it fails only when
sent a picture. Ollama is known not to wire up the image sidecar for some Qwen
builds, which is why the roster requires Qwen3-VL rather than Qwen3.6.

So this check sends an image containing a string whose value is known in advance
and requires that string back. A model that loaded is not a model that can see.

    scripts/vision_control.py --model huihui_ai/qwen3-vl-abliterated:8b
    scripts/vision_control.py --self-check

Exit codes:
    0  the model read the fixture correctly
    1  it did not, or could not be asked
    2  bad arguments
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_HOST = "http://localhost:11434"
FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/vision-probe.png"
EXPECTED = "HOMELAB-VISION-OK-4711"
PROMPT = "Read the text in this image. Reply with only that text, nothing else."


def verdict(response: str, expected: str) -> tuple[str, str]:
    """Three states, never two."""
    answer = (response or "").strip()
    if not answer:
        return "INCONCLUSIVE", "empty response - the model was asked but said nothing"
    # Normalised: models add quotes, trailing periods and stray whitespace, and
    # none of those mean it failed to read the image.
    flat = "".join(ch for ch in answer.upper() if ch.isalnum() or ch == "-")
    if expected.upper().replace("-", "") in flat.replace("-", ""):
        return "SEES", answer[:200]
    return "BLIND", (
        f"expected {expected!r} somewhere in the reply, got {answer[:200]!r}. "
        "Either the image sidecar did not load - the failure this control exists "
        "for - or the fixture is not legible")


# Without this the matcher could be inverted and a blind model would pass.
VERDICT_CASES = (
    ("exact", "HOMELAB-VISION-OK-4711", "SEES"),
    ("quoted and padded", '  "HOMELAB-VISION-OK-4711."  ', "SEES"),
    ("lowercase", "homelab-vision-ok-4711", "SEES"),
    ("wrapped in a sentence", "The text reads HOMELAB-VISION-OK-4711 clearly.", "SEES"),
    ("hyphens dropped", "HOMELABVISIONOK4711", "SEES"),
    ("plausible but wrong", "HOMELAB-VISION-OK-4712", "BLIND"),
    ("describes instead of reading", "An image containing white background text.", "BLIND"),
    ("empty", "", "INCONCLUSIVE"),
    ("whitespace only", "   \n ", "INCONCLUSIVE"),
)


def self_check() -> list[str]:
    problems: list[str] = []
    for description, response, expected in VERDICT_CASES:
        got, _ = verdict(response, EXPECTED)
        if got != expected:
            problems.append(
                f"verdict {description!r}: got {got}, expected {expected} — the "
                "matcher is wrong, so a model that cannot see would pass this "
                "control or a working one would fail it")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="the vision model to test")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Ollama base URL (default {DEFAULT_HOST})")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    if args.self_check:
        problems = self_check()
        for problem in problems:
            print(problem, file=sys.stderr)
        print("self-check: OK" if not problems else "self-check: FAILED")
        return 1 if problems else 0

    if not args.model:
        parser.print_usage(sys.stderr)
        print("--model is required", file=sys.stderr)
        return 2
    if not FIXTURE.exists():
        print(f"{FIXTURE} is missing. Without the fixture this control cannot "
              "look at anything and must not report success", file=sys.stderr)
        return 1

    payload = {
        "model": args.model,
        "prompt": PROMPT,
        "images": [base64.b64encode(FIXTURE.read_bytes()).decode()],
        "stream": False,
        "options": {"num_predict": 64, "temperature": 0},
    }
    req = urllib.request.Request(
        f"{args.host.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        print(f"INCONCLUSIVE  HTTP {exc.code}: {exc.read()[:200]!r}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"INCONCLUSIVE  {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    state, evidence = verdict(data.get("response", ""), EXPECTED)
    print(f"{state:<14} {args.model}")
    print(f"{'':<14} {evidence}")
    return 0 if state == "SEES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the self-check**

Run: `python scripts/vision_control.py --self-check`

Expected: `self-check: OK`.

- [ ] **Step 5: Verify the self-check can fail**

Temporarily change `if expected.upper().replace("-", "") in flat.replace("-", ""):`
to `if True:` and re-run.

Expected: FAILS on `'plausible but wrong'` and `'describes instead of reading'`.
**Revert.**

- [ ] **Step 6: Run the real control against the vision model**

```powershell
python scripts\vision_control.py --model huihui_ai/qwen3-vl-abliterated:8b
```

Expected: `SEES`. If `BLIND`, first re-open the fixture and confirm a human can
read it; if it is legible, the image sidecar did not load and the model is
rejected.

- [ ] **Step 7: Run the abliteration control against the coder**

Add the coder to `ROSTER` in `scripts/abliteration_control.py`:

```python
ROSTER = (
    "huihui_ai/gemma-4-abliterated:26b",
    "huihui_ai/Qwen3.6-abliterated:27b",
    "huihui_ai/gemma-4-abliterated:31b",
    "davidau-fable-fusion:27b-q4km",
    "huihui_ai/qwen3-coder-abliterated:30b-a3b-instruct-q4_K_M",
)
```

Run: `python scripts/abliteration_control.py --roster --host http://localhost:11434`

Expected: `5/5 answered the control prompt`, exit 0. Anything else and the coder
is not installed — an abliterated model that refuses is the wrong model.

- [ ] **Step 8: Record both outcomes in the catalog**

Add the accepted models. The vision entry (adjust `measured_mib` to the survey's
figure):

```yaml
  - name: huihui_ai/qwen3-vl-abliterated:8b
    tier: terra
    role: vision
    abliterated: true
    measured_mib: 0  # replace with the survey figure
    measured_on: "2026-08-11"
    num_ctx: 32768
    why: >-
      Reads documents, receipts and screenshots. The 8B is chosen over the 32B
      deliberately: it scores 96.1 on DocVQA - ahead of Gemma 3 at every size -
      and reading documents is the use case. Must be Qwen3-VL, not Qwen3.6:
      Ollama runs 3.6 as text only and does not wire up its vision sidecar, and
      a model with no vision answers text questions perfectly. Verified with
      scripts/vision_control.py.
```

And the coder:

```yaml
  - name: huihui_ai/qwen3-coder-abliterated:30b-a3b-instruct-q4_K_M
    tier: terra
    role: code
    abliterated: true
    measured_mib: 0  # replace with the survey figure
    measured_on: "2026-08-11"
    num_ctx: 16384
    why: >-
      Fills the uncensored-coding gap left when aratan/qwen3.6-claude-coder-35b
      was removed. That one failed because its weights exceeded the card at any
      context; this is a 30B-A3B MoE at 19 GB, under the measured ceiling.
```

**Note the roster now has two `role: code` entries.** That is intended — one
abliterated, one stock. `validate_model_roster.py` does not forbid it.

If a candidate was **rejected**, do not add it. Record the rejection in
`docs/chat-models.md` beside the `aratan` paragraph, with the measured numbers.

Run: `python tests/validate_model_roster.py`

Expected: `Model roster: OK`. It will fail if the coder is in the catalog as
`abliterated: true` but missing from `ROSTER`, which is the cross-check working.

- [ ] **Step 9: Add both to Open WebUI**

Neither model appears in the chat dropdown until Open WebUI knows about it, and
because `ENABLE_PERSISTENT_CONFIG` is `"true"` this is a UI action, not a deploy.
Confirm both are selectable at `https://chat.fortwow.dev` and that each returns a
real reply — a green container proves nothing.

- [ ] **Step 10: Commit**

```bash
git add scripts/vision_control.py tests/fixtures/vision-probe.png \
        scripts/abliteration_control.py inventory/group_vars/all/models.yml \
        docs/chat-models.md
git commit -m "feat: add an uncensored coder and a vision model that must prove it sees"
```

---

### Task 6: Three-way reconciliation against the live estate

The check that earns the catalog. Runs against live services, so it is not part
of `make validate`.

**Files:**
- Create: `scripts/roster_reconcile.py`
- Modify: `Makefile` (add a `roster-check` target)

**Interfaces:**
- Consumes: `inventory/group_vars/all/models.yml` (Task 1).
- Produces: `scripts/roster_reconcile.py` exposing `reconcile(catalog: set[str],
  ollama: set[str], webui: set[str]) -> list[tuple[str, str, str]]` returning
  `(severity, name, explanation)` triples.

- [ ] **Step 1: Write the failing self-check**

Create `scripts/roster_reconcile.py`:

```python
#!/usr/bin/env python3
"""Compare the roster in git against Ollama and against Open WebUI.

Open WebUI's model list is its OWN TABLE, not a view over Ollama. So removing a
model upstream leaves the entry behind, and a user who picks it gets a failure
at generation time rather than an absence in the dropdown.

That is not hypothetical. aratan/qwen3.6-claude-coder-35b was deleted from
Ollama on 2026-08-10 and its row is still live in Open WebUI with is_active = 1.
Nothing in the estate would ever have reported it. This is that report.

    scripts/roster_reconcile.py --webui-db /opt/homelab/appdata/open-webui/webui.db
    scripts/roster_reconcile.py --self-check

Exit codes:
    0  the three agree
    1  a mismatch, or a source could not be read
    2  bad arguments
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROSTER_PATH = ROOT / "inventory/group_vars/all/models.yml"
DEFAULT_OLLAMA = "http://192.168.1.40:11434"


def reconcile(catalog: set[str], ollama: set[str],
              webui: set[str]) -> list[tuple[str, str, str]]:
    """Every disagreement between the three, most serious first."""
    findings: list[tuple[str, str, str]] = []
    for name in sorted(webui - ollama):
        findings.append((
            "BROKEN", name,
            "selectable in Open WebUI but not installed in Ollama - a user who "
            "picks it gets a failure at generation time, not an absence"))
    for name in sorted(ollama - catalog):
        findings.append((
            "UNDECLARED", name,
            "installed in Ollama but not in models.yml - undeclared drift"))
    for name in sorted(catalog - ollama):
        findings.append((
            "MISSING", name,
            "declared in models.yml but not installed in Ollama"))
    return findings


# Ordering matters as much as detection: BROKEN is the one with a live user
# impact and must not be buried under a list of MISSING entries.
RECONCILE_CASES = (
    ("all three agree", {"a"}, {"a"}, {"a"}, []),
    ("the aratan case - stale in Open WebUI",
     {"a"}, {"a"}, {"a", "ghost"}, [("BROKEN", "ghost")]),
    ("installed but undeclared",
     {"a"}, {"a", "extra"}, {"a"}, [("UNDECLARED", "extra")]),
    ("declared but not installed",
     {"a", "planned"}, {"a"}, {"a"}, [("MISSING", "planned")]),
    ("BROKEN sorts above MISSING",
     {"a", "planned"}, {"a"}, {"a", "ghost"},
     [("BROKEN", "ghost"), ("MISSING", "planned")]),
    ("empty everywhere is agreement, not an error", set(), set(), set(), []),
)


def self_check() -> list[str]:
    problems: list[str] = []
    for description, catalog, ollama, webui, expected in RECONCILE_CASES:
        got = [(sev, name) for sev, name, _ in reconcile(catalog, ollama, webui)]
        if got != expected:
            problems.append(
                f"reconcile {description!r}: got {got}, expected {expected} — the "
                "comparison is wrong, so a model that is broken for users would "
                "not be reported")
    return problems
```

- [ ] **Step 2: Add the entrypoint and run the self-check**

Append:

```python
def catalog_names() -> set[str]:
    data = yaml.safe_load(ROSTER_PATH.read_text(encoding="utf-8")) or {}
    roster = data.get("model_roster") or []
    return {e["name"] for e in roster if e.get("tier") == "terra"}


def ollama_names(base_url: str, timeout: int) -> set[str]:
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags",
                                timeout=timeout) as resp:
        tags = json.load(resp)
    names = {m["name"] for m in tags.get("models", [])}
    if not names:
        raise SystemExit(
            "Ollama reported zero models. That is almost certainly a broken "
            "query rather than an empty host, and reporting every catalogued "
            "model as MISSING would be worse than not running")
    # /api/tags suffixes bare names with :latest; the catalog writes them bare.
    return {n[: -len(":latest")] if n.endswith(":latest") else n for n in names}


def webui_names(db_path: str) -> set[str]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # `base_model_id IS NULL` is load-bearing, not a tidiness filter.
        # Personas (Workspace -> Models) are rows in this SAME table, carrying
        # the base model they wrap in base_model_id. Their ids are things like
        # `thera`, which no Ollama tag will ever match — so without this clause
        # every persona reports as BROKEN and the one finding that matters is
        # buried under false ones on the very first run.
        rows = con.execute(
            "SELECT id FROM model WHERE is_active = 1 AND base_model_id IS NULL"
        ).fetchall()
    finally:
        con.close()
    return {r[0][: -len(":latest")] if r[0].endswith(":latest") else r[0]
            for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA)
    parser.add_argument("--webui-db",
                        help="path to Open WebUI's webui.db (read-only)")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    if args.self_check:
        problems = self_check()
        for problem in problems:
            print(problem, file=sys.stderr)
        print("self-check: OK" if not problems else "self-check: FAILED")
        return 1 if problems else 0

    if not args.webui_db:
        parser.print_usage(sys.stderr)
        print("--webui-db is required. Without it the check cannot see the one "
              "mismatch it exists to find", file=sys.stderr)
        return 2

    findings = reconcile(catalog_names(), ollama_names(args.ollama_url, args.timeout),
                         webui_names(args.webui_db))
    if not findings:
        print("Roster reconciliation: OK - catalog, Ollama and Open WebUI agree")
        return 0
    for severity, name, explanation in findings:
        print(f"{severity:<12} {name}", file=sys.stderr)
        print(f"{'':<12} {explanation}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

Run: `python scripts/roster_reconcile.py --self-check`

Expected: `self-check: OK`.

- [ ] **Step 3: Verify the self-check can fail**

Temporarily swap the `webui - ollama` loop to run *after* the `catalog - ollama`
loop and re-run.

Expected: FAILS on `'BROKEN sorts above MISSING'`. **Revert.**

- [ ] **Step 4: Add the Makefile target**

Add near the other operational targets (not under `validate`, because this one
talks to live services):

```makefile
roster-check: ## Compare models.yml against Ollama and Open WebUI (needs both up)
	$(PYTHON) scripts/roster_reconcile.py \
	  --webui-db /opt/homelab/appdata/open-webui/webui.db
```

- [ ] **Step 5: Run it against the live estate**

Run it on svc-infra, where `webui.db` lives, with the GPU host reachable.

Expected: at least one `BROKEN` finding naming
`aratan/qwen3.6-claude-coder-35b-A3b-mlx-Q4KM-abliterated`. **That is the
success condition for this task** — the check finding a known-real defect on its
first run is the positive control. If it reports OK, the query is wrong.

- [ ] **Step 6: Clear the stale row**

Deactivate the `aratan` entry in Open WebUI's admin UI, then re-run.

Expected: `Roster reconciliation: OK`.

Update `docs/chat-models.md` — the paragraph stating the row is still live is now
false, and it must say when and how it was cleared instead.

- [ ] **Step 7: Commit**

```bash
git add scripts/roster_reconcile.py Makefile docs/chat-models.md
git commit -m "fix: notice when Open WebUI offers a model Ollama does not have"
```

---

### Task 7: Close out the branch

**Files:**
- Modify: `docs/superpowers/specs/2026-08-11-inference-capacity-and-roster-design.md`

- [ ] **Step 1: Run the full offline gate**

Run: `make validate`

Expected: every gate passes. If run on Windows, it will fail at `validate-tools`
for missing ansible/shellcheck/gitleaks — run the Python validators individually
and say explicitly that the full gate was not run and why. Do not claim a clean
`make validate` you did not see.

- [ ] **Step 2: Confirm a clean tree**

Run: `git status --porcelain`

Expected: no output. Untracked files count. `pass-*.json` from Task 4 must be
deleted or moved out of the repo.

- [ ] **Step 3: Deploy and verify**

```bash
make infra
```

Expected: `changed=3` on the first run after a commit (the `git archive` sync
block), then `changed=0` on a second `make infra`. Check *which* three tasks
changed — do not accept the second number without reading the first.

Nothing in this branch changes a Quadlet, so anything beyond those three is a
genuine diff and must be explained before merging.

- [ ] **Step 4: Mark the spec implemented**

Add a line under the spec's title recording the implementation date, and note
any rule the measurements overturned — particularly whether cache quantization
applied or fell back, since that closes an open question for good.

- [ ] **Step 5: Merge, push, delete the branch**

```bash
git switch main
git merge --ff-only docs/inference-capacity-roster
git push
git branch -d docs/inference-capacity-roster
git push origin --delete docs/inference-capacity-roster
```

Step 8 of the repo workflow is deleting the branch. It was skipped for the
repo's first 75 commits and 22 stale branches accumulated.

---

## Follow-on work this unblocks

- **Provisioning the M1 Pro MBP** as the always-on tier — its own spec. The
  catalog already has a `mbp` tier value with no entries.
- **The RAG pipeline**, which now has a decided embedding tier and a decided
  reranker placement (`RAG_RERANKING_MODEL: BAAI/bge-reranker-v2-m3`, CPU inside
  the Open WebUI container on svc-infra, *not* on the GPU host).
- **Home Assistant integration**, which needs the MBP first, and should use
  `prefer_local_intents` so deterministic commands never reach an LLM.
- **Wiring `roster-check` into `make verify`**, once it has run cleanly a few
  times by hand.
