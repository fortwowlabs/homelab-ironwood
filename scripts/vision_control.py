#!/usr/bin/env python3
"""Positive control for the vision models. They must actually see.

A vision model whose image half did not load answers TEXT questions perfectly.
It looks entirely healthy, `ollama ps` shows it resident, `/api/tags` lists it,
and it fails only when sent a picture. That failure mode is not hypothetical
here: muse-glimmer:30b reports `families=['muse-glimmer']` with no separate
projector entry, which is exactly the shape a model with no working perception
encoder would have - it needed an image to tell the two apart.

So this sends an image containing a string whose value is known in advance and
requires that string back. A model that loaded is not a model that can see.

    scripts/vision_control.py --roster
    scripts/vision_control.py --model huihui_ai/qwen3-vl-abliterated:8b
    scripts/vision_control.py --self-check

Exit codes:
    0  every model asked read the fixture correctly
    1  at least one did not, or could not be asked
    2  bad arguments

Two traps this encodes, both hit by hand before it existed:

  * Reasoning models return their reasoning in a separate `thinking` field and
    leave `response` empty when the token budget runs out first. muse-glimmer
    spent an entire 32-token budget thinking and returned ''. An empty answer
    is not a blind model - it is an unanswered question. Hence the 1200 default
    and the INCONCLUSIVE verdict for `done_reason == 'length'` with no text.
  * The fixture has to be legible to a human before a model failing on it means
    anything. If this control starts failing everywhere, open the PNG first.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_HOST = "http://192.168.1.40:11434"
FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/vision-probe.png"

# Deliberately not a word or a number a model could guess from context: it has
# to be READ. Letters and digits only, so normalisation never has to reason
# about punctuation the model may or may not echo.
EXPECTED = "HOMELAB-VISION-OK-4711"
PROMPT = "Read the text in this image. Reply with only that text, nothing else."

# Every model in the roster that claims to see. Kept in step with
# inventory/group_vars/all/models.yml's `role: vision` entries.
# tests/validate_model_roster.py fails if this drifts from the catalog in
# either direction, so a candidate is added here when it is accepted into
# models.yml, not while it is still being evaluated. Test a candidate with
# --model instead.
ROSTER = (
    "muse-glimmer:30b",
)


def _flatten(text: str) -> str:
    """Upper-case alphanumerics only.

    Models wrap the answer in quotes, add a trailing period, split the hyphens
    differently or spell it across a sentence. None of that is a failure to
    read the image, so none of it should fail the control.
    """
    return "".join(ch for ch in text.upper() if ch.isalnum())


def verdict(response: str, thinking: str, done_reason: str | None,
            expected: str = EXPECTED) -> tuple[str, str]:
    """Three states, never two."""
    answer = (response or "").strip()
    if not answer and done_reason == "length":
        return "INCONCLUSIVE", (
            "no answer; the whole token budget went to `thinking`. Raise "
            f"--num-predict. Thinking began: {(thinking or '')[:100]!r}")
    if not answer:
        return "INCONCLUSIVE", f"empty response, done_reason={done_reason!r}"
    if _flatten(expected) in _flatten(answer):
        return "SEES", answer[:200].replace("\n", " ")
    return "BLIND", (
        f"expected {expected!r} in the reply, got {answer[:200]!r}. Either the "
        "image sidecar did not load - the failure this control exists for - or "
        "the fixture is no longer legible. Open the PNG before blaming the "
        "model")


# Each case is (description, response, thinking, done_reason, expected verdict).
# Without this table the matcher could be inverted and a blind model would pass
# the control that exists to catch exactly that.
VERDICT_CASES = (
    ("exact", "HOMELAB-VISION-OK-4711", "", "stop", "SEES"),
    ("quoted and padded", '  "HOMELAB-VISION-OK-4711."  ', "", "stop", "SEES"),
    ("lower case", "homelab-vision-ok-4711", "", "stop", "SEES"),
    ("wrapped in a sentence", "The text reads HOMELAB-VISION-OK-4711 clearly.",
     "", "stop", "SEES"),
    ("hyphens dropped", "HOMELABVISIONOK4711", "", "stop", "SEES"),
    ("spaced out", "HOMELAB VISION OK 4711", "", "stop", "SEES"),
    ("one digit wrong is not a read", "HOMELAB-VISION-OK-4712", "", "stop", "BLIND"),
    ("describes instead of reading",
     "An image containing black text on a white background.", "", "stop", "BLIND"),
    ("refuses", "I'm sorry, I can't help with images.", "", "stop", "BLIND"),
    # The trap: a reasoning model that never got to its answer.
    ("budget spent thinking", "", "We need to read the text...", "length",
     "INCONCLUSIVE"),
    ("empty for any other reason", "", "", "stop", "INCONCLUSIVE"),
    ("whitespace only", "   \n ", "", "stop", "INCONCLUSIVE"),
)


def self_check() -> list[str]:
    problems: list[str] = []
    for description, response, thinking, reason, expected in VERDICT_CASES:
        got, _ = verdict(response, thinking, reason)
        if got != expected:
            problems.append(
                f"verdict {description!r}: got {got}, expected {expected} — the "
                "matcher is wrong, so either a model that cannot see would pass "
                "this control or a working one would fail it")
    return problems


def ask(host: str, model: str, image_b64: str, num_predict: int,
        timeout: int) -> tuple[str, str]:
    payload = {
        "model": model,
        "prompt": PROMPT,
        "images": [image_b64],
        "stream": False,
        "options": {"num_predict": num_predict, "temperature": 0},
    }
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        return "INCONCLUSIVE", f"HTTP {exc.code}: {exc.read()[:200]!r}"
    except Exception as exc:
        return "INCONCLUSIVE", f"{type(exc).__name__}: {exc}"
    return verdict(data.get("response", ""), data.get("thinking", ""),
                   data.get("done_reason"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("models", nargs="*", help="models to test")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Ollama base URL (default {DEFAULT_HOST})")
    parser.add_argument("--roster", action="store_true",
                        help="test every vision model in the roster")
    parser.add_argument("--num-predict", type=int, default=1200,
                        help="token budget; must clear the thinking block "
                             "(default 1200)")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--self-check", action="store_true",
                        help="prove the matcher still works, then exit")
    args = parser.parse_args()

    if args.self_check:
        problems = self_check()
        for problem in problems:
            print(problem, file=sys.stderr)
        print("self-check: OK" if not problems else "self-check: FAILED")
        return 1 if problems else 0

    models = list(args.models)
    if args.roster:
        models.extend(ROSTER)
    if not models:
        parser.print_usage(sys.stderr)
        print("give at least one model, or --roster", file=sys.stderr)
        return 2

    if not FIXTURE.exists():
        print(f"{FIXTURE} is missing. Without the fixture this control cannot "
              "look at anything, and it must not report success for models it "
              "never asked", file=sys.stderr)
        return 1
    image_b64 = base64.b64encode(FIXTURE.read_bytes()).decode()

    results = [(m, *ask(args.host, m, image_b64, args.num_predict, args.timeout))
               for m in models]

    print()
    for model, state, evidence in results:
        print(f"{state:<14} {model}")
        print(f"{'':<14} {evidence}")
        print()

    seeing = [m for m, s, _ in results if s == "SEES"]
    print(f"{len(seeing)}/{len(results)} read the fixture")
    bad = [(m, s) for m, s, _ in results if s != "SEES"]
    if bad:
        print("\nNOT a clean result - a model that cannot see is worse than no "
              "vision model, because it answers anyway:", file=sys.stderr)
        for model, state in bad:
            print(f"  {state}: {model}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
