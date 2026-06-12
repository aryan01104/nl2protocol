#!/usr/bin/env python3
"""
evals/generate_control.py — single-call baselines for the eval suite.

Runs the same `(instruction.txt, config.json)` pair through a plain Anthropic
API call (no pipeline, no gap resolution, no resolver) so you have a control
to compare the architected output against.

Two modes:
  * `regular` — naive zero-shot prompt, the kind of message a non-expert
                might paste into a chat: "make me an OT-2 protocol for this".
                Tests the bar of "what a casual user gets for free."
  * `proper`  — engineered single-call prompt: system role, XML-tagged inputs,
                code-only output, pitfall checklist. Tests the bar of "what a
                skilled prompt engineer would get without architecting a pipeline."

Both modes use the same model as the extractor (read from
`SemanticExtractor.__init__` default) so model choice is held constant — any
delta is attributable to the pipeline, not the model.

Thinking is off in both modes to match the production extractor config.

Usage:
    python evals/generate_control.py list
    python evals/generate_control.py generate --case 1 --mode proper
    python evals/generate_control.py generate --case 01_lotterhos_magbead --mode both -n 3
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = PROJECT_ROOT / "evals"
CONTROLS_DIR = EVALS_DIR / "controls"

sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass


# ----------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------

REGULAR_USER_PROMPT = """Can you create a Python protocol for the Opentrons OT-2 based on this instruction and config?

Instruction:
{instruction}

Config:
{config_json}"""


PROPER_SYSTEM_PROMPT = """You are a senior lab automation engineer fluent in the Opentrons Python Protocol API (v2). You translate natural-language wet-lab procedures into runnable OT-2 protocol scripts.

OUTPUT FORMAT — strict:
- Respond with exactly one fenced code block: ```python ... ``` and nothing else.
- No prose before the block. No prose after the block.
- The script must be a complete, runnable Opentrons v2 protocol with `metadata` and a `run(protocol)` function.
- Use `metadata = {{'protocolName': '...', 'author': 'control-baseline', 'apiLevel': '2.15'}}`.

INPUT FORMAT:
- The user message contains an `<instruction>` block (natural language) and a `<config>` block (JSON deck/labware/pipettes/modules).
- Use the labware `load_name` and `slot` exactly as given in `<config>`. Do not invent slots or substitute labware.
- Use the pipette `model` and `mount` exactly as given. The tipracks listed for each pipette are the only racks that pipette can pick up from.
- If `modules` is present in the config, load it on the slot specified.

COMMON PITFALLS — check before emitting:
- Tip lifecycle: pick up a tip before each new reagent, drop tips between reagents, don't reuse tips across contaminating steps.
- Pipette range: every aspirate/dispense volume must be within the pipette's working range. If a step exceeds it, split into multiple transfers.
- Magnetic module: `magnetic_module.engage()` and `magnetic_module.disengage()` are explicit calls. Wait times are `protocol.delay(minutes=..., seconds=...)`.
- Temperature module: `temp_module.set_temperature(C)` blocks until reached.
- Wells: refer to wells by `labware['A1']` style addressing matching what the instruction states.
- Fuzzy values: if the instruction says "5–15 min" or "appropriate volume", pick a reasonable single value (you cannot ask). Note the choice with a brief inline comment.
- Do not fabricate labware or pipettes that aren't in `<config>`.

Now produce the script."""


PROPER_USER_PROMPT = """<instruction>
{instruction}
</instruction>

<config>
{config_json}
</config>

Generate the OT-2 protocol script now. Code block only."""


# ----------------------------------------------------------------------------
# Case resolution
# ----------------------------------------------------------------------------

def discover_cases() -> list[str]:
    """Return sorted case names that have the four required input files."""
    cases = []
    for entry in sorted(EVALS_DIR.iterdir()):
        if not entry.is_dir() or entry.name in ("runs", "controls"):
            continue
        required = ["instruction.txt", "config.json", "expected.md"]
        if all((entry / f).exists() for f in required):
            cases.append(entry.name)
    return cases


def resolve_case(ident: str, cases: list[str]) -> Optional[str]:
    """Map a user-provided identifier to a discovered case directory name.

    Accepts: `1`, `01`, `01_lotterhos_magbead`. Returns None if no unique match.
    """
    if ident in cases:
        return ident
    padded = ident.zfill(2)
    prefix_matches = [c for c in cases if c.startswith(f"{padded}_")]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    return None


# ----------------------------------------------------------------------------
# Model resolution
# ----------------------------------------------------------------------------

def extractor_default_model() -> str:
    """Read the extractor's default model from its __init__ signature.

    Single source of truth: if the extractor's default model changes, the
    control's default tracks it automatically.
    """
    from nl2protocol.extraction.extractor import SemanticExtractor
    sig = inspect.signature(SemanticExtractor.__init__)
    return sig.parameters["model_name"].default


# ----------------------------------------------------------------------------
# Response handling
# ----------------------------------------------------------------------------

_CODE_BLOCK_RE = re.compile(
    r"```(?:python|py)?\s*\n(.*?)```",
    re.DOTALL,
)


def extract_python_code(response_text: str) -> str:
    """Pull a Python script out of a model response.

    Prefers a fenced ```python block. Falls back to any fenced block. Falls
    back to the raw text. The raw response is always saved separately, so
    this extraction is lossy-but-recoverable.
    """
    match = _CODE_BLOCK_RE.search(response_text)
    if match:
        return match.group(1).strip() + "\n"
    return response_text.strip() + "\n"


# ----------------------------------------------------------------------------
# Generation
# ----------------------------------------------------------------------------

def build_messages(mode: str, instruction: str, config_json: str) -> dict:
    """Return {system, user} message payloads for the given mode."""
    if mode == "regular":
        return {
            "system": None,
            "user": REGULAR_USER_PROMPT.format(
                instruction=instruction,
                config_json=config_json,
            ),
        }
    if mode == "proper":
        return {
            "system": PROPER_SYSTEM_PROMPT,
            "user": PROPER_USER_PROMPT.format(
                instruction=instruction,
                config_json=config_json,
            ),
        }
    raise ValueError(f"Unknown mode: {mode}")


def call_model(client, model_name: str, system: Optional[str], user: str) -> dict:
    """Single Anthropic call. Returns {response_text, stop_reason, usage}."""
    kwargs = dict(
        model=model_name,
        max_tokens=8192,
        messages=[{"role": "user", "content": user}],
    )
    if system is not None:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    return {
        "response_text": response.content[0].text,
        "stop_reason": response.stop_reason,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }


def generate_one(
    case_name: str,
    mode: str,
    client,
    model_name: str,
    run_id: str,
    git_sha: str,
) -> dict:
    """Generate one control output for (case, mode). Returns a result dict."""
    case_dir = EVALS_DIR / case_name
    instruction = (case_dir / "instruction.txt").read_text()
    config_json = (case_dir / "config.json").read_text()

    out_dir = CONTROLS_DIR / case_name / f"{mode}__{run_id}__{git_sha}"
    out_dir.mkdir(parents=True, exist_ok=True)

    for f in ["instruction.txt", "config.json", "expected.md", "source.md"]:
        src = case_dir / f
        if src.exists():
            (out_dir / f).write_text(src.read_text())

    msgs = build_messages(mode, instruction, config_json)
    prompt_dump = ""
    if msgs["system"]:
        prompt_dump += f"=== SYSTEM ===\n{msgs['system']}\n\n"
    prompt_dump += f"=== USER ===\n{msgs['user']}\n"
    (out_dir / "prompt.txt").write_text(prompt_dump)

    started = datetime.now(timezone.utc)
    error = None
    api_result = None
    try:
        api_result = call_model(client, model_name, msgs["system"], msgs["user"])
    except Exception as e:
        error = {
            "type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        }
    finished = datetime.now(timezone.utc)

    meta = {
        "case": case_name,
        "mode": mode,
        "model": model_name,
        "thinking": False,
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "duration_s": (finished - started).total_seconds(),
        "git_sha": git_sha,
        "out_dir": str(out_dir.relative_to(PROJECT_ROOT)),
        "error": error,
    }

    if api_result is not None:
        (out_dir / "response_raw.txt").write_text(api_result["response_text"])
        script = extract_python_code(api_result["response_text"])
        (out_dir / "script.py").write_text(script)
        meta["stop_reason"] = api_result["stop_reason"]
        meta["usage"] = api_result["usage"]
        meta["script_chars"] = len(script)

    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _git_sha_short() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT, stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "nogit"


def cmd_list(args, cases):
    print(f"Discovered {len(cases)} cases in {EVALS_DIR.relative_to(PROJECT_ROOT)}/:")
    for i, c in enumerate(cases, 1):
        print(f"  {i:>2}. {c}")
    return 0


def cmd_generate(args, cases):
    case = resolve_case(args.case, cases)
    if case is None:
        print(f"Unknown case: {args.case}", file=sys.stderr)
        print(f"Run `python evals/generate_control.py list` to see options.",
              file=sys.stderr)
        return 1

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY not set. Add it to .env or export it.",
              file=sys.stderr)
        return 1

    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)

    model_name = args.model or extractor_default_model()
    modes = ["regular", "proper"] if args.mode == "both" else [args.mode]
    git_sha = _git_sha_short()

    print(f"Case:  {case}", file=sys.stderr)
    print(f"Model: {model_name}", file=sys.stderr)
    print(f"Modes: {', '.join(modes)}  (x{args.num_runs})", file=sys.stderr)

    results = []
    for mode in modes:
        for i in range(args.num_runs):
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            label = f"{mode}" + (f" run{i+1}/{args.num_runs}" if args.num_runs > 1 else "")
            print(f"\n--- {label} ---", file=sys.stderr)
            meta = generate_one(case, mode, client, model_name, run_id, git_sha)
            results.append(meta)
            if meta.get("error"):
                print(f"  error: {meta['error']['type']}: {meta['error']['message']}",
                      file=sys.stderr)
            else:
                print(f"  -> {meta['out_dir']}", file=sys.stderr)
                print(f"     stop_reason={meta.get('stop_reason')} "
                      f"out_tokens={meta.get('usage', {}).get('output_tokens')} "
                      f"duration={meta['duration_s']:.1f}s",
                      file=sys.stderr)

    failures = [r for r in results if r.get("error")]
    print(f"\nDone. {len(results) - len(failures)} ok, {len(failures)} failed.",
          file=sys.stderr)
    return 0 if not failures else 1


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List discovered eval cases and exit.")

    gen = sub.add_parser(
        "generate",
        help="Generate one or more control outputs for a case.",
    )
    gen.add_argument("--case", required=True,
                     help="Case identifier: number (1, 01) or full name (01_lotterhos_magbead).")
    gen.add_argument("--mode", required=True,
                     choices=["regular", "proper", "both"],
                     help="Which control(s) to generate.")
    gen.add_argument("-n", "--num-runs", type=int, default=1,
                     help="Sample stochasticity by running each mode N times.")
    gen.add_argument("--model", default=None,
                     help="Override model. Default: read from SemanticExtractor.")

    args = parser.parse_args()
    cases = discover_cases()

    if args.cmd == "list":
        return cmd_list(args, cases)
    if args.cmd == "generate":
        return cmd_generate(args, cases)
    parser.print_usage()
    return 2


if __name__ == "__main__":
    sys.exit(main())
