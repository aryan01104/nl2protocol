# Example 3: Heat Shock Bacterial Transformation

A complex example exercising hardware modules: a 12-step heat shock transformation protocol with temperature module orchestration, multiple pauses, dual pipettes, and tip-strategy variation across reagent types.

## Input

- [`instruction.txt`](./instruction.txt) — the natural-language instruction (28 lines, 12 numbered steps).
- [`lab_config.json`](./lab_config.json) — lab config including a temperature module on slot 3, dual pipettes (p20 + p300), and a tube rack for plasmid + cell + recovery medium tubes.

The instruction describes a standard molecular biology workflow: cold incubation → DNA addition → ice incubation → heat shock at 42°C → return to ice → recovery medium addition → recovery incubation.

## Output

- [`generated_protocol.py`](./generated_protocol.py) — the generated Opentrons script (56 lines, 43 commands).
- [`simulation_log.txt`](./simulation_log.txt) — Opentrons simulator output confirming the script runs.
- [`pipeline_state.json`](./pipeline_state.json) — full per-stage debug log including provenance traces.
- [`run_output.txt`](./run_output.txt) — the CLI's stage-by-stage stdout from this run.

## What this demonstrates

- **Hardware module orchestration** — `set_temperature()` and `await_temperature()` calls map directly from the instruction's *"set temperature module to 4°C and wait for it to stabilize"* and the heat-shock cycle (`4°C → 42°C → 4°C`).
- **Per-sample tip discipline** — *"Use fresh tips between different plasmids to prevent cross-contamination"* extracted into separate `pick_up_tip` / `drop_tip` cycles per plasmid pair, visible in the simulation log as 9 tip pickups for 4 sample pairs (plus shared steps).
- **Pauses and comments preserved** — the instruction's incubation pauses (5 min on ice, 45 sec heat shock, 2 min return-to-cold, 1 hr recovery) and the operator-facing comment about heat-shock timing all survive through extraction → spec → script.
- **Mixed pipette use** — the system assigned the small-volume (2uL plasmid, 20uL mix) operations to the p20 and the large-volume (200uL SOC medium) operations to the p300, matching the constraint that the larger pipette can't handle the small-volume steps.

## What was verified

- All 8 stages passed end-to-end: input validation, extraction, provenance verification, labware resolution, hardware constraints, schema generation, script generation, Opentrons simulation, intent verification.
- 43 commands generated. Both pipettes used. Temperature module engaged with 3 set-points + 1 await call.
