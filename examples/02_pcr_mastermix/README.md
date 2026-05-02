# Example 2: PCR Master Mix Setup

A mid-complexity example showing the system handling multi-pipette assignment, distribute operations across 24 wells, and per-sample mixing.

## Input

- [`instruction.txt`](./instruction.txt) — the natural-language instruction.
- [`lab_config.json`](./lab_config.json) — the lab config: dual pipettes (p300 + p20), 5 labware items including a tube rack and two PCR plates.

The instruction describes setting up a PCR plate for 24 samples in three sub-steps: distribute master mix, transfer DNA template per sample, mix after each transfer.

## Output

- [`generated_protocol.py`](./generated_protocol.py) — the generated Opentrons script (37 lines).
- [`simulation_log.txt`](./simulation_log.txt) — Opentrons simulator output confirming the script runs.
- [`pipeline_state.json`](./pipeline_state.json) — full per-stage debug log (the equivalent of `run_output.txt` but in structured JSON; this example was reused from a prior successful run that did not capture stdout).

## What this demonstrates

- **Multi-pipette routing** — instruction says *"Use the p20 pipette for the DNA template transfers"*; the system honored that and assigned the master-mix distribute to the p300.
- **Operation type selection** — for the 24-well master-mix step the system chose `distribute()` (one-source-many-destinations) rather than 24 separate `transfer()` calls, matching the instruction's framing of *"distribute"*.
- **Per-sample mixing** — `mix_after=(3, 10.0)` reflects *"Mix each well 3 times at 10uL after adding template"* extracted as a `PostAction` on the transfer step.
- **Tip strategy** — *"new tip for each sample"* extracted as `new_tip_each` for the template transfers.

## What was verified

- Stages 1–7 passed: input validation, extraction, provenance verification, hardware constraints, schema generation, script generation, Opentrons simulation.
- Stage 8 (LLM intent verification) passed.
