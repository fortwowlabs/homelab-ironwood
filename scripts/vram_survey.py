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
        unload(host, model, timeout)
        return row
    except Exception as exc:
        row.update(verdict="INCONCLUSIVE", card_used_mib=None, size_total=None,
                   size_vram=None,
                   evidence=f"model loaded but could not read final state: {type(exc).__name__}: {exc}")
        unload(host, model, timeout)
        return row

    row.update(verdict=state, card_used_mib=card_used_mib,
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
