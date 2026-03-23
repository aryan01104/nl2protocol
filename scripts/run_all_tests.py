#!/usr/bin/env python3
"""
Run all test cases with and without config.
Results saved to test_results/{test_name}_{timestamp}/with_config/ and without_config/
"""

import subprocess
import sys
import shutil
import re
from pathlib import Path
from datetime import datetime

TEST_CASES = [
    "simple_transfer",
    "distribute",
    "serial_dilution",
    "pcr_mastermix",
    "elisa_sample_addition",
    "magnetic_bead_cleanup",
    "qpcr_standard_curve",
    "cell_viability_assay",
]


def run_test(test_name: str, with_config: bool, results_dir: Path, base_dir: Path) -> tuple[bool, str]:
    """
    Run a single test case.

    Args:
        test_name: Name of the test case
        with_config: Whether to use config file
        results_dir: Directory to save results (with_config or without_config subdir)
        base_dir: Project base directory

    Returns:
        (success, log_output)
    """
    test_dir = base_dir / "test_cases" / test_name
    instruction_file = test_dir / "instruction.txt"
    config_file = test_dir / "config.json"

    # Check test case exists
    if not test_dir.exists():
        return False, f"Test directory not found: {test_dir}"
    if not instruction_file.exists():
        return False, f"Instruction file not found: {instruction_file}"

    results_dir.mkdir(parents=True, exist_ok=True)

    output_file = results_dir / "protocol.py"

    # Build command
    cmd = [
        sys.executable, "-m", "nl2protocol",
        "-i", str(instruction_file),
        "-o", str(output_file),
        "-r", "4"
    ]

    if with_config:
        if not config_file.exists():
            return False, "No config.json found"
        cmd.extend(["-c", str(config_file)])
    else:
        cmd.append("--generate-config")

    # Run with 'y' input for --generate-config prompt
    # Run from results_dir so llm_failed_output files are created there
    try:
        # Stream output live while also capturing it
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Merge stderr into stdout
            text=True,
            cwd=results_dir
        )

        # Send 'y' input for --generate-config prompt
        if not with_config:
            process.stdin.write("y\n")
            process.stdin.flush()
        process.stdin.close()

        # Stream output live and capture it
        log_lines = []
        for line in process.stdout:
            print(f"    {line}", end="", flush=True)  # Print live with indent, flush immediately
            log_lines.append(line)

        process.wait(timeout=300)
        log_output = "".join(log_lines)
        returncode = process.returncode

        # Save log
        log_file = results_dir / "run.log"
        log_file.write_text(log_output)

        # For --generate-config, extract and save the generated config
        if not with_config:
            # Look for "Using generated config: /path/to/file.json"
            match = re.search(r'Using generated config: (.+\.json)', log_output)
            if match:
                temp_config_path = Path(match.group(1).strip())
                if temp_config_path.exists():
                    # Copy to results directory
                    saved_config = results_dir / "generated_config.json"
                    shutil.copy(temp_config_path, saved_config)

        # Check for success: returncode 0 AND output file exists AND simulation passed
        output_files = list(results_dir.glob("protocol*.py"))
        has_output = len(output_files) > 0

        success = (
            returncode == 0 and
            has_output and
            "Reached max attempts" not in log_output and
            "Simulation passed" in log_output
        )

        return success, log_output

    except subprocess.TimeoutExpired:
        process.kill()
        return False, "TIMEOUT after 5 minutes"
    except Exception as e:
        return False, f"Error: {e}"


def main():
    base_dir = Path(__file__).parent.parent

    # Create timestamped test results directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    test_results_base = base_dir / "test_results"
    test_results_base.mkdir(exist_ok=True)

    print("\n" + "=" * 60)
    print("  NL2PROTOCOL TEST SUITE")
    print("=" * 60)
    print(f"  Timestamp: {timestamp}")
    print(f"  Output:    {test_results_base}")
    print("=" * 60 + "\n", flush=True)

    results = {
        "with_config": {"passed": [], "failed": []},
        "without_config": {"passed": [], "failed": []}
    }

    for i, test_name in enumerate(TEST_CASES, 1):
        print(f"\n[{i}/{len(TEST_CASES)}] {test_name.upper()}")
        print("-" * 40, flush=True)

        # Create test-specific results directory with timestamp
        test_results_dir = test_results_base / f"{test_name}_{timestamp}"

        # Run WITH config
        print(f"\n  WITH CONFIG:", flush=True)
        with_config_dir = test_results_dir / "with_config"
        success, log = run_test(test_name, with_config=True, results_dir=with_config_dir, base_dir=base_dir)
        if success:
            print("\n  ✓ PASSED\n", flush=True)
            results["with_config"]["passed"].append(test_name)
        else:
            print("\n  ✗ FAILED\n", flush=True)
            results["with_config"]["failed"].append(test_name)

        # Run WITHOUT config
        print(f"  WITHOUT CONFIG (auto-generate):", flush=True)
        without_config_dir = test_results_dir / "without_config"
        success, log = run_test(test_name, with_config=False, results_dir=without_config_dir, base_dir=base_dir)
        if success:
            print("\n  ✓ PASSED", flush=True)
            results["without_config"]["passed"].append(test_name)
        else:
            print("\n  ✗ FAILED", flush=True)
            results["without_config"]["failed"].append(test_name)

    # Summary
    print(f"\n\n{'=' * 60}")
    print("  SUMMARY")
    print("=" * 60)

    with_passed = len(results['with_config']['passed'])
    with_total = len(TEST_CASES)
    without_passed = len(results['without_config']['passed'])

    print(f"\n  WITH CONFIG:      {with_passed}/{with_total} passed", end="")
    if results['with_config']['failed']:
        print(f"  [Failed: {', '.join(results['with_config']['failed'])}]")
    else:
        print()

    print(f"  WITHOUT CONFIG:   {without_passed}/{with_total} passed", end="")
    if results['without_config']['failed']:
        print(f"  [Failed: {', '.join(results['without_config']['failed'])}]")
    else:
        print()

    print(f"\n  Results: {test_results_base}/")

    # Save summary to file
    summary_file = test_results_base / f"summary_{timestamp}.txt"
    with open(summary_file, 'w') as f:
        f.write(f"Test Run: {timestamp}\n")
        f.write("=" * 40 + "\n\n")
        f.write(f"WITH CONFIG: {len(results['with_config']['passed'])}/{len(TEST_CASES)} passed\n")
        if results['with_config']['failed']:
            f.write(f"  Failed: {', '.join(results['with_config']['failed'])}\n")
        f.write(f"\nWITHOUT CONFIG: {len(results['without_config']['passed'])}/{len(TEST_CASES)} passed\n")
        if results['without_config']['failed']:
            f.write(f"  Failed: {', '.join(results['without_config']['failed'])}\n")

    print(f"Summary saved to: {summary_file}")

    # Return non-zero if any failed
    total_failed = len(results['with_config']['failed']) + len(results['without_config']['failed'])
    return 1 if total_failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
