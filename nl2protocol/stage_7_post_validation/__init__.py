"""
stage_7_post_validation — deterministic codegen + simulation tail.

Stage 7 of the pipeline (the "Building & simulating" stage) takes the
resolved + validated `ProtocolSpec`, builds a `ProtocolSchema`, emits a
runnable Opentrons Python script, and runs the script through the
Opentrons simulator. No LLM calls; no user interaction. Two pure
translations + one external-tool invocation.

Public surface:
  - `generate_python_script(protocol, step_summaries=None)` — schema → script
  - `simulate_script(script_code)` — script → (success, log, runlog)
"""

from .codegen import generate_python_script
from .simulate import simulate_script

__all__ = ["generate_python_script", "simulate_script"]
