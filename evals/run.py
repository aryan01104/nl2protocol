#!/usr/bin/env python3
"""
evals/run.py — runner for the prose-based eval suite.

Discovers `evals/<case>/{instruction.txt, config.json, expected.md, source.md}`,
runs the nl2protocol pipeline once per case (or N times with -n), and writes
all generated artifacts into `evals/runs/<batch>/<case>/` so the user can
hand-grade by reading them side by side with `expected.md`.

Grading is intentionally NOT automated. LLM outputs vary; ground truth is
prose; the human is the grader. The runner produces artifacts; the user
records verdicts wherever they like (e.g. evals/runs.md).

Usage:
    python evals/run.py --list
    python evals/run.py 01_lotterhos_magbead
    python evals/run.py 01_lotterhos_magbead -n 3
    python evals/run.py --all
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = PROJECT_ROOT / "evals"
RUNS_DIR = EVALS_DIR / "runs"
PIPELINE_OUTPUT_DIR = PROJECT_ROOT / "output"

sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass


def discover_cases():
    """Return sorted case names that have the three required input files."""
    cases = []
    for entry in sorted(EVALS_DIR.iterdir()):
        if not entry.is_dir() or entry.name == "runs":
            continue
        required = ["instruction.txt", "config.json", "expected.md"]
        if all((entry / f).exists() for f in required):
            cases.append(entry.name)
    return cases


class LoggingAutoCM:
    """Always returns '' (accept-default), but logs every question to a file.

    Hand-graders need to know what defaults the runner silently accepted —
    otherwise a "passing" run might just mean every gap was rubber-stamped.
    """

    def __init__(self, log_path: Path):
        self._log = log_path.open("a")
        self._count = 0

    def prompt(self, question: str) -> str:
        self._count += 1
        self._log.write(
            f"[Q{self._count}]\n{question}\n"
            f"[A{self._count}] <empty / accept default>\n\n"
        )
        self._log.flush()
        return ""

    def close(self):
        self._log.close()

    @property
    def prompt_count(self) -> int:
        return self._count


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)

    def flush(self):
        for s in self.streams:
            s.flush()


def _git_sha_short() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT, stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "nogit"


def _snapshot_output_dir() -> set:
    if not PIPELINE_OUTPUT_DIR.exists():
        return set()
    return {p.name for p in PIPELINE_OUTPUT_DIR.iterdir()}


def _move_new_output_files(before: set, dest: Path):
    if not PIPELINE_OUTPUT_DIR.exists():
        return
    for p in PIPELINE_OUTPUT_DIR.iterdir():
        if p.name not in before:
            shutil.move(str(p), str(dest / p.name))


def run_one_case(case_name: str, run_dir: Path, api_key: str) -> dict:
    from nl2protocol.pipeline import ProtocolAgent
    from nl2protocol.reporting import ConsoleReporter

    case_dir = EVALS_DIR / case_name
    instruction = (case_dir / "instruction.txt").read_text()
    config_path = case_dir / "config.json"

    for f in ["instruction.txt", "config.json", "expected.md", "source.md"]:
        src = case_dir / f
        if src.exists():
            shutil.copy(src, run_dir / f)

    console_path = run_dir / "console.txt"
    cm_log_path = run_dir / "prompts.txt"
    result_path = run_dir / "result.json"

    cm = LoggingAutoCM(cm_log_path)
    started = datetime.now(timezone.utc)
    before = _snapshot_output_dir()

    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    console_file = console_path.open("w")
    sys.stdout = Tee(saved_stdout, console_file)
    sys.stderr = Tee(saved_stderr, console_file)

    pipeline_result = None
    error = None
    try:
        agent = ProtocolAgent(
            api_key=api_key,
            config_path=str(config_path),
            confirmation_manager=cm,
            reporter=ConsoleReporter(),
        )
        pipeline_result = agent.run_pipeline(prompt=instruction)
    except Exception as e:
        error = {
            "type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        }
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
        console_file.close()
        cm.close()
        _move_new_output_files(before, run_dir)

    finished = datetime.now(timezone.utc)

    if pipeline_result is not None:
        (run_dir / "script.py").write_text(pipeline_result.script)
        (run_dir / "simulation_log.txt").write_text(pipeline_result.simulation_log)

    result = {
        "case": case_name,
        "started_utc": started.isoformat(),
        "finished_utc": finished.isoformat(),
        "duration_s": (finished - started).total_seconds(),
        "git_sha": _git_sha_short(),
        "pipeline_returned": pipeline_result is not None,
        "prompts_accepted": cm.prompt_count,
        "error": error,
        "run_dir": str(run_dir.relative_to(PROJECT_ROOT)),
        "hand_grade": "TODO",
        "notes": "",
    }
    result_path.write_text(json.dumps(result, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("case", nargs="?",
                        help="Case name (e.g. 01_lotterhos_magbead). Omit with --all or --list.")
    parser.add_argument("--all", action="store_true", help="Run every discovered case.")
    parser.add_argument("--list", action="store_true", help="List available cases and exit.")
    parser.add_argument("-n", "--num-runs", type=int, default=1,
                        help="Run each case N times. Use >1 to sample LLM stochasticity.")
    args = parser.parse_args()

    cases = discover_cases()

    if args.list:
        print(f"Discovered {len(cases)} cases in {EVALS_DIR.relative_to(PROJECT_ROOT)}/:")
        for c in cases:
            print(f"  {c}")
        return 0

    if not args.case and not args.all:
        parser.print_usage()
        print("\nProvide a case name, --all, or --list.", file=sys.stderr)
        return 2

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY not set. Add it to .env or export it.", file=sys.stderr)
        return 1

    if args.all:
        to_run = cases
    else:
        if args.case not in cases:
            print(f"Unknown case: {args.case}", file=sys.stderr)
            print(f"Run `python evals/run.py --list` to see options.", file=sys.stderr)
            return 1
        to_run = [args.case]

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    RUNS_DIR.mkdir(exist_ok=True)
    batch_dir = RUNS_DIR / f"{run_id}_{_git_sha_short()}"
    batch_dir.mkdir()

    summary = []
    for case in to_run:
        for i in range(args.num_runs):
            suffix = f"__run{i+1}" if args.num_runs > 1 else ""
            run_dir = batch_dir / f"{case}{suffix}"
            run_dir.mkdir()
            print(f"\n========== {case} (run {i+1}/{args.num_runs}) ==========\n",
                  file=sys.stderr)
            result = run_one_case(case, run_dir, api_key)
            summary.append(result)
            print(f"\n  -> artifacts: {run_dir.relative_to(PROJECT_ROOT)}", file=sys.stderr)
            print(f"  -> pipeline_returned={result['pipeline_returned']} "
                  f"prompts={result['prompts_accepted']} "
                  f"duration={result['duration_s']:.1f}s", file=sys.stderr)
            if result["error"]:
                print(f"  -> error: {result['error']['type']}: "
                      f"{result['error']['message']}", file=sys.stderr)

    (batch_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"\nBatch complete. {len(summary)} runs.", file=sys.stderr)
    print(f"Artifacts: {batch_dir.relative_to(PROJECT_ROOT)}", file=sys.stderr)
    print("\nNext: open each run dir's expected.md alongside console.txt + script.py",
          file=sys.stderr)
    print("      and record your verdicts by hand.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
