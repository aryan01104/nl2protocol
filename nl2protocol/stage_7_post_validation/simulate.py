"""
simulate.py — Run the generated Opentrons script through the simulator.

The Opentrons simulator dry-runs a protocol script and returns the
synthesized run log (the sequence of pipette/module actions the robot
would execute) — without any hardware. Used as the final automated
check before handing the script back to the user.
"""

import io
import logging
import warnings
from contextlib import redirect_stderr

# Suppress opentrons simulator warnings + deck-calibration banners
logging.getLogger("opentrons").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", module="opentrons")

from opentrons import simulate


def simulate_script(script_code: str) -> tuple[bool, str, list]:
    """Runs the script through the Opentrons simulator.

    Pre:    `script_code` is a complete Opentrons Python protocol — the
            string returned by `generate_python_script`.
    Post:   Returns `(success, formatted_log, raw_runlog)`:
              - `success`: True iff the simulator completed without
                raising. False if it raised any exception.
              - `formatted_log`: human-readable summary. On success,
                "Simulation completed successfully." followed by one
                line per run-log entry. On failure, "Simulation failed:
                <exception>".
              - `raw_runlog`: list of command dicts from the simulator
                on success; empty list on failure.
            Stderr from the simulator is captured and discarded — deck
            calibration warnings are not the user's concern at this
            stage.
    Side effects: Captures + discards simulator stderr. No file I/O.
    """
    protocol_file = io.StringIO(script_code)
    try:
        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            runlog, _ = simulate.simulate(protocol_file)
        log_lines = ["Simulation completed successfully.", "", "Run Log:"]
        for entry in runlog:
            if isinstance(entry, dict):
                msg = entry.get('payload', {}).get('text', str(entry))
                log_lines.append(f"  {msg}")
            else:
                log_lines.append(f"  {entry}")
        return True, "\n".join(log_lines), runlog
    except Exception as e:
        return False, f"Simulation failed: {str(e)}", []
