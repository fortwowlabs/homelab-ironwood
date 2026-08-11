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
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "http://localhost:11434"

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
    problems.extend(_self_check_probe_writable())
    return problems


def _self_check_probe_writable() -> list[str]:
    """_probe_writable cases, kept apart from CLASSIFY_CASES/BASELINE_CASES
    above: those two are pure functions checked against static tuples, but
    _probe_writable does real filesystem I/O, so these cases run against a
    real TemporaryDirectory that is created and torn down right here.
    Nothing this writes may survive the check or land in the repo."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="vram_survey_self_check_") as tmp:
        # Case 1 (the regression test): an existing file's content must
        # survive the probe byte-for-byte. This fails against the old
        # `open(path, "w")` implementation, which truncates the file the
        # instant it opens even though the probe never writes anything.
        existing = os.path.join(tmp, "pass-f16.json")
        original = b'{"rows": ["a completed 45-minute survey pass"]}'
        with open(existing, "wb") as handle:
            handle.write(original)
        result = _probe_writable(existing)
        if result is not None:
            problems.append(
                f"_probe_writable existing writable file: got error {result!r}, "
                "expected None")
        with open(existing, "rb") as handle:
            survived = handle.read()
        if survived != original:
            problems.append(
                "_probe_writable existing writable file: content changed after "
                f"the probe (was {len(original)} bytes, now {len(survived)} "
                "bytes) — the probe truncated a completed survey pass just to "
                "check the path was writable")

        # Case 2: the containing directory does not exist.
        missing_dir = os.path.join(tmp, "no-such-dir", "pass-f16.json")
        result = _probe_writable(missing_dir)
        if result is None:
            problems.append(
                "_probe_writable missing directory: got None, expected an "
                "error — a bad --out directory would only surface after the "
                "~45-minute survey ran")

        # Case 3: the path is itself an existing directory.
        as_dir = os.path.join(tmp, "a-directory")
        os.mkdir(as_dir)
        result = _probe_writable(as_dir)
        if result is None:
            problems.append(
                "_probe_writable path is a directory: got None, expected an "
                "error — writing rows there would fail deep into the survey "
                "instead of before it starts")

        # Case 4: a writable path that does not exist yet - the normal
        # first-run case, which must not be mistaken for a missing directory.
        fresh = os.path.join(tmp, "new-pass.json")
        result = _probe_writable(fresh)
        if result is not None:
            problems.append(
                f"_probe_writable new path in existing directory: got error "
                f"{result!r}, expected None — the first run of a survey "
                "targets a path that does not exist yet and must be allowed")
    return problems


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


def unload(host: str, model: str, timeout: int) -> bool:
    """keep_alive 0 evicts immediately. Without this every measurement inherits
    the previous model's residency and the numbers are cumulative nonsense.

    Returns True only if eviction was actually confirmed via /api/ps. A timeout
    or 500 on the evict call used to be swallowed by a bare `pass` with nothing
    checking whether the model actually left the card - a ~20 GB model could
    stay resident and inflate every later card_used_mib in the pass while the
    per-model verdict kept reading MEASURED. If /api/ps itself cannot be read,
    that is treated the same as "still resident": eviction was not confirmed,
    so the caller cannot trust what comes after it either."""
    try:
        _post(host, "/api/generate", {"model": model, "keep_alive": 0}, timeout)
    except Exception:
        pass
    time.sleep(3)
    try:
        return ps_entry(host, model, timeout) is None
    except Exception:
        return False


def measure(host: str, model: str, num_ctx: int | None, timeout: int,
            embed: bool) -> tuple[dict, bool]:
    """Returns (row, unload_confirmed). unload_confirmed is False whenever the
    model may still be resident afterward - the row dict's schema is frozen
    (scripts/vram_report.py consumes it), so this out-of-band signal is how a
    stuck unload gets reported without changing that contract."""
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
        return row, unload(host, model, timeout)
    except Exception as exc:
        row.update(verdict="INCONCLUSIVE", card_used_mib=None, size_total=None,
                   size_vram=None, evidence=f"{type(exc).__name__}: {exc}")
        return row, unload(host, model, timeout)

    try:
        entry = ps_entry(host, model, timeout) or {}
        size_total = entry.get("size")
        size_vram = entry.get("size_vram")
        state, evidence = classify(size_total, size_vram)
        card_used_mib = nvidia_smi_used_mib()
    except urllib.error.HTTPError as exc:
        row.update(verdict="INCONCLUSIVE", card_used_mib=None, size_total=None,
                   size_vram=None,
                   evidence=f"HTTP {exc.code} reading state after load: {exc.read()[:200]!r}")
        return row, unload(host, model, timeout)
    except Exception as exc:
        row.update(verdict="INCONCLUSIVE", card_used_mib=None, size_total=None,
                   size_vram=None,
                   evidence=f"model loaded but could not read final state: {type(exc).__name__}: {exc}")
        return row, unload(host, model, timeout)

    row.update(verdict=state, card_used_mib=card_used_mib,
               size_total=size_total, size_vram=size_vram, evidence=evidence)
    return row, unload(host, model, timeout)


def _probe_writable(path: str) -> str | None:
    """Confirm the output path can be written before spending any GPU time on
    the survey. A bad directory, a read-only path or a full disk used to
    surface only after the entire ~45-minute grid had run, as a traceback
    with nothing salvaged.

    This must never disturb an existing file at `path`. An earlier version
    opened it with `open(path, "w")`, which truncates the file the instant it
    is called - before a single byte is written, and before the idle-card
    baseline check that runs right after this probe. A re-run against a busy
    card would pass this probe, truncate a previous completed pass sitting at
    that path, and then abort at the baseline check - destroying a finished
    45-minute survey to report that the GPU was busy. That is worse than the
    slow failure this probe exists to prevent, so instead it probes
    writability of the *containing directory* with a throwaway temp file that
    is created and removed, and if `path` itself already exists it only
    stats it - confirming it is a regular, writable file - without ever
    opening it in a truncating mode. Returns None if writable, or an error
    message otherwise."""
    if os.path.isdir(path):
        return f"cannot write to {path}: is a directory"
    if os.path.exists(path):
        if not os.path.isfile(path):
            return f"cannot write to {path}: not a regular file"
        if not os.access(path, os.W_OK):
            return f"cannot write to {path}: not writable"
        return None
    directory = os.path.dirname(path) or "."
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".vram_survey_probe_"):
            pass
    except OSError as exc:
        return f"cannot write to {path}: {exc}"
    return None


def _survey_payload(cache_type: str, flash_attention: bool, baseline: int,
                    taken: str, rows: list[dict]) -> dict:
    """The one place the pass-file shape is assembled, shared by the
    incremental write (after every measurement) and the final write, so they
    cannot drift apart into two different dict literals."""
    return {
        "cache_type": cache_type,
        "flash_attention": flash_attention,
        "baseline_mib": baseline,
        "taken": taken,
        "rows": rows,
    }


def run_survey(args) -> int:
    problem = _probe_writable(args.out)
    if problem:
        print(problem, file=sys.stderr)
        return 1

    baseline = nvidia_smi_used_mib()
    ok, evidence = baseline_verdict(baseline)
    print(f"baseline: {evidence}")
    if not ok:
        return 1

    taken = datetime.date.today().isoformat()
    rows: list[dict] = []
    stuck: list[tuple[str, int | None]] = []

    def flush() -> None:
        # Rewritten after every measurement, not just at the end: the payload
        # is small, so ~48 rewrites cost nothing, and a crash, a Ctrl-C
        # (KeyboardInterrupt is a BaseException and was never caught here) or
        # a power cut preserves every row already taken instead of losing the
        # whole pass.
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(_survey_payload(args.cache_type, args.flash_attention,
                                      baseline, taken, rows), handle, indent=2)
            handle.write("\n")

    def record(model: str, num_ctx: int | None, embed: bool) -> None:
        row, unload_confirmed = measure(args.host, model, num_ctx, args.timeout, embed)
        rows.append(row)
        if not unload_confirmed:
            stuck.append((model, num_ctx))
            ctx_desc = num_ctx if num_ctx is not None else "n/a"
            print(
                f"WARNING: could not confirm {model} was evicted after the @ "
                f"{ctx_desc} measurement (unload call failed, or /api/ps still "
                "shows it resident). It may still be occupying the GPU, which "
                "means every card_used_mib measured after this point in the "
                "pass is potentially inflated by its footprint and should not "
                "be trusted until the card is checked and cleared by hand.",
                file=sys.stderr)
        flush()

    for model in args.models:
        for num_ctx in CONTEXTS:
            print(f"  measuring {model} @ {num_ctx} ...", flush=True)
            record(model, num_ctx, embed=False)
    for model in args.embed_only:
        print(f"  measuring {model} (embed, load-only) ...", flush=True)
        record(model, None, embed=True)

    print()
    for row in rows:
        ctx = row["num_ctx"] if row["num_ctx"] is not None else "n/a"
        print(f"{row['verdict']:<14} {row['model']} @ {ctx}")
        print(f"{'':<14} {row['evidence']}")
    inconclusive = [r for r in rows if r["verdict"] == "INCONCLUSIVE"]
    print(f"\n{len(rows)} measurements written to {args.out}")
    if stuck:
        print(f"\n{len(stuck)} unload(s) not confirmed - later measurements in "
              "this pass may be inflated and untrustworthy:", file=sys.stderr)
        for model, num_ctx in stuck:
            print(f"  {model} @ {num_ctx if num_ctx is not None else 'n/a'}",
                  file=sys.stderr)
    if inconclusive:
        print(f"\n{len(inconclusive)} INCONCLUSIVE - this pass is not complete:",
              file=sys.stderr)
        for row in inconclusive:
            print(f"  {row['model']} @ {row['num_ctx']}: {row['evidence']}",
                  file=sys.stderr)
    if inconclusive or stuck:
        return 1
    return 0


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
