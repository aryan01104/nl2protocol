"""
evals/run.py — runner for the LLM-extraction eval set.

Discovers every `evals/*/expected.json`, loads the linked instruction +
config from `test_cases/`, runs real-LLM extraction + the constraint
check, and asserts the loose invariants declared in expected.json.

This is the ONLY layer that exercises the real Anthropic API — every
other test layer (contract / property / boundary / integration) uses
mocks. Evals catch model-behavior regressions that no mock can detect.

Usage:
    ANTHROPIC_API_KEY=... python evals/run.py
    ANTHROPIC_API_KEY=... python evals/run.py --eval 04_pipette_insufficient

Exit code is 0 iff every eval passes.
"""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Resolve project root so we can import nl2protocol and resolve `source` paths.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env before checking ANTHROPIC_API_KEY, so users can keep the key in
# a project-root .env file rather than exporting it manually.
try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

from nl2protocol.config import ConfigLoader
from nl2protocol.extraction import LabwareResolver
from nl2protocol.extraction.extractor import SemanticExtractor
from nl2protocol.validation.constraints import ConstraintChecker, ViolationType


# ============================================================================
# Eval result
# ============================================================================

@dataclass
class EvalResult:
    name: str
    passed: bool
    failures: list = field(default_factory=list)
    duration_s: float = 0.0


# ============================================================================
# Invariant checks — each returns a list of failure strings (empty = passed)
# ============================================================================

def _check_extract(extract_block: dict, spec) -> list:
    failures = []
    if extract_block.get("should_succeed", True):
        if spec is None:
            failures.append("extract.should_succeed: extraction returned None")
    else:
        if spec is not None:
            failures.append("extract.should_succeed=false but extraction returned a spec")
    return failures


def _check_spec(spec_block: dict, spec) -> list:
    failures = []
    if spec is None:
        # If extraction failed, spec invariants can't be checked — but they're
        # only meaningful when extraction was supposed to succeed. The
        # extract block's should_succeed check will already have flagged
        # the real failure.
        return failures

    if "step_count_at_least" in spec_block:
        n = spec_block["step_count_at_least"]
        if len(spec.steps) < n:
            failures.append(f"spec.step_count_at_least={n} but got {len(spec.steps)}")

    if "step_count_at_most" in spec_block:
        n = spec_block["step_count_at_most"]
        if len(spec.steps) > n:
            failures.append(f"spec.step_count_at_most={n} but got {len(spec.steps)}")

    if "explicit_volumes_include" in spec_block:
        required = spec_block["explicit_volumes_include"]
        present = set(spec.explicit_volumes or [])
        missing = [v for v in required if v not in present]
        if missing:
            failures.append(
                f"spec.explicit_volumes_include={required} but spec only had {sorted(present)}; "
                f"missing {missing}"
            )

    if "actions_present" in spec_block:
        required = set(spec_block["actions_present"])
        actual = {step.action for step in spec.steps}
        missing = required - actual
        if missing:
            failures.append(
                f"spec.actions_present requires {sorted(required)} but spec had {sorted(actual)}; "
                f"missing {sorted(missing)}"
            )

    return failures


def _check_constraints(constraints_block: dict, result) -> list:
    failures = []

    expect_errors = constraints_block.get("expect_errors", False)
    has_errors = result.has_errors

    if expect_errors and not has_errors:
        failures.append("constraints.expect_errors=true but constraint check produced no errors")
    elif not expect_errors and has_errors:
        error_summary = [f"{v.violation_type.value}: {v.what}" for v in result.errors]
        failures.append(
            f"constraints.expect_errors=false but got {len(result.errors)} error(s): {error_summary}"
        )

    if "violation_types_required" in constraints_block:
        required_names = set(constraints_block["violation_types_required"])
        actual_names = {v.violation_type.name for v in result.violations}
        missing = required_names - actual_names
        if missing:
            failures.append(
                f"constraints.violation_types_required={sorted(required_names)} "
                f"but got {sorted(actual_names)}; missing {sorted(missing)}"
            )

    return failures


# ============================================================================
# Single-eval runner
# ============================================================================

def run_one_eval(eval_dir: Path) -> EvalResult:
    name = eval_dir.name
    start = time.time()
    failures: list = []

    expected_path = eval_dir / "expected.json"
    if not expected_path.exists():
        return EvalResult(name, False, [f"missing expected.json"], 0.0)

    expected = json.loads(expected_path.read_text())

    # Resolve source instruction + config
    source_rel = expected.get("source")
    if not source_rel:
        return EvalResult(name, False, ["expected.json missing 'source' field"], 0.0)
    source_dir = PROJECT_ROOT / source_rel
    instruction_path = source_dir / "instruction.txt"
    config_path = source_dir / "config.json"
    for p in (instruction_path, config_path):
        if not p.exists():
            return EvalResult(name, False, [f"source missing {p}"], 0.0)

    instruction = instruction_path.read_text()

    # Run pipeline (extract → labware-resolve → constraints) with real LLM.
    # Labware resolution sits between extraction and constraints in the real
    # pipeline (Stage 3.5); skipping it would cause LABWARE_NOT_FOUND on every
    # eval whose instruction uses any user-language labware description.
    try:
        loader = ConfigLoader(config_path=str(config_path))
        loader.load_config()
        extractor = SemanticExtractor(client=loader.client, model_name=loader.model_name)
        spec = extractor.extract(instruction, loader.config)
        if spec is not None:
            resolver = LabwareResolver(
                config=loader.config,
                client=loader.client,
                model_name=loader.model_name,
            )
            spec = resolver.resolve(spec)
    except Exception as e:
        return EvalResult(
            name, False,
            [f"pipeline crashed: {type(e).__name__}: {e}"],
            time.time() - start,
        )

    # Check extract invariants
    failures += _check_extract(expected.get("extract", {}), spec)

    # Check spec invariants (only meaningful if extract succeeded)
    if "spec" in expected and spec is not None:
        failures += _check_spec(expected["spec"], spec)

    # Check constraint invariants (only meaningful if extract succeeded)
    if "constraints" in expected and spec is not None:
        try:
            checker = ConstraintChecker(loader.config)
            constraint_result = checker.check_all(spec)
            failures += _check_constraints(expected["constraints"], constraint_result)
        except Exception as e:
            failures.append(f"constraint check crashed: {type(e).__name__}: {e}")

    return EvalResult(name, len(failures) == 0, failures, time.time() - start)


# ============================================================================
# Discovery + main
# ============================================================================

def discover_evals(only: Optional[str] = None) -> list:
    evals_dir = Path(__file__).resolve().parent
    candidates = sorted(d for d in evals_dir.iterdir() if d.is_dir() and (d / "expected.json").exists())
    if only:
        candidates = [d for d in candidates if d.name == only]
    return candidates


def main():
    parser = argparse.ArgumentParser(description="Run the nl2protocol eval set.")
    parser.add_argument("--eval", help="Run only this eval (directory name)", default=None)
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Evals require a real LLM.", file=sys.stderr)
        sys.exit(2)

    eval_dirs = discover_evals(only=args.eval)
    if not eval_dirs:
        print(f"No evals found{' matching --eval ' + args.eval if args.eval else ''}.", file=sys.stderr)
        sys.exit(2)

    print(f"Running {len(eval_dirs)} eval(s)...\n")
    results = []
    for d in eval_dirs:
        print(f"  [{d.name}]", end=" ", flush=True)
        r = run_one_eval(d)
        results.append(r)
        status = "PASS" if r.passed else "FAIL"
        print(f"{status} ({r.duration_s:.1f}s)")
        for f in r.failures:
            print(f"      - {f}")

    n_passed = sum(r.passed for r in results)
    n_total = len(results)
    print(f"\n{n_passed}/{n_total} passed")
    sys.exit(0 if n_passed == n_total else 1)


if __name__ == "__main__":
    main()
