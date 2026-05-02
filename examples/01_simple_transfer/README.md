# Example 1: Simple Transfer

A baseline example showing the system extracting and executing a basic multi-well transfer with explicit tip strategy.

## Input

- [`instruction.txt`](./instruction.txt) — the natural-language instruction.
- [`lab_config.json`](./lab_config.json) — the lab config the LLM was given (2 plates + tipracks + p300 pipette).

> *"Transfer 50uL from source plate wells A1, A2, A3 to destination plate wells B1, B2, B3. Use a new tip for each transfer."*

## Output

- [`generated_protocol.py`](./generated_protocol.py) — the generated Opentrons script (13 lines).
- [`simulation_log.txt`](./simulation_log.txt) — Opentrons simulator output confirming the script runs.
- [`pipeline_state.json`](./pipeline_state.json) — full per-stage debug log including provenance traces.
- [`run_output.txt`](./run_output.txt) — the CLI's stage-by-stage stdout from this run.

## What this demonstrates

- **Direct extraction** — every value (volume, well positions) carries `provenance.source = "instruction"` because the user wrote them verbatim.
- **Compression** — three transfers expressed as one logical step with paired well lists, then deterministically expanded into three `transfer()` calls.
- **Tip strategy honored** — *"new tip for each transfer"* was extracted as `tip_strategy: new_tip_each` and reflected in the generated script.

## What was verified

- Stages 1–7 passed: input validation, extraction, provenance verification, hardware constraints, schema generation, script generation, Opentrons simulation.
- Stage 8 (LLM intent verification) passed with high confidence.
