# Pipeline and Testing Map

> **Superseded by ADR-0008 (PR3b).** This doc describes the pre-PR3b
> pipeline shape — six ad-hoc detect/fill/refine/confirm stretches in
> `pipeline.py`. PR3b replaced all six with the unified gap-resolution
> orchestrator (`nl2protocol/gap_resolution/`). For the legacy → new
> mapping, see ADR-0008's "What PR3b replaced" appendix. Symbols
> referenced below (`extractor.refine`, `_confirm_provenance_items`,
> `_build_initial_volume_queue`, `_apply_provenance_edits`,
> `format_for_confirmation`) no longer exist; recover via
> `git show <pre-PR3b-commit>:<path>` if needed.

How the pipeline runs, where the LLM lives, and what each deterministic stage owes the next one. Source: `nl2protocol/pipeline.py` (`ProtocolAgent.run_pipeline`).

## Linear flow

Each stage either passes its output forward, branches into a sub-flow, or returns `None` and exits. `(LLM)` = makes a call to Anthropic. `(det)` = pure Python, no network.

### Stage 1 — Validate config + instruction `(LLM for input classification)`

1. **`ConfigLoader.load_config()` (det)** — parse `config.json`, fail fast if malformed.
2. **`InputValidator.classify(prompt)` (LLM)** — classifies prompt as PROTOCOL / QUESTION / AMBIGUOUS / INVALID. Skipped if `--skip-validation`.
   - **Branch:** if not `is_valid_protocol` → print suggestion, `return None`.

### Stage 2 — Extract spec from instruction `(LLM)`

1. **`SemanticExtractor.extract(prompt, config)` (LLM)** — produces a `ProtocolSpec` with steps, provenance, `explicit_volumes`, `initial_contents`. The single load-bearing LLM call.
   - **Branch:** if `spec is None` → save state log, `return None`.

### Stage 3 — Validate + complete the spec `(LLM + det)`

1. **`extractor.verify_provenance_claims(spec, prompt, config)` (LLM)** — checks whether each "from instruction" claim actually appears in the prompt.
   - **Branch:** if any `severity == "fabrication"` → abort.
2. **`extractor.missing_fields(spec)` (det)** — finds gaps (missing volumes, durations, etc.).
   - **Branch:** if gaps exist:
     - **`extractor.fill_gaps(spec, config)` (det)** — pull defaults from config.
     - If gaps remain: **`extractor.refine(spec, gaps, prompt, config)` (LLM)** — re-reason with feedback.
     - If gaps still remain after refine → abort.
3. **`_infer_source_containers(spec)` (det)** — wells aspirated-from but never dispensed-into are pre-filled by the user. Prompt user for confirmation; on accept, append `WellContents` to `spec.initial_contents`.

### Stage 3.5 — Resolve labware references `(LLM + interactive)`

1. **`LabwareResolver.resolve(spec)` (LLM)** — maps each free-text `LocationRef.description` to a config label.
2. **`_confirm_labware_assignments(spec)` (interactive)** — TTY only. Shows assignments, lets user edit.
   - **Branch:** if any `LocationRef.resolved_label` still missing → abort with "add labware to config" message.

### Stage 4 — Hardware constraints `(det)`

1. **`ConstraintChecker(config).check_all(spec)` (det)** — pipette capacity, labware existence, module availability, tip sufficiency.
   - **Branch:** if `has_errors`:
     - TTY → ask user to proceed anyway (`y/N`); on N abort.
     - Non-TTY → abort.
2. **Build confirmation queue:** non-fabrication provenance warnings + initial-contents wells with `volume_ul is None`.
3. **`_confirm_provenance_items(spec, confirmable)` (interactive)** — per-item accept/edit/skip/quit. Edits are applied via `_apply_provenance_edits`.

### Stage 5 — Spec → ProtocolSchema `(det)`

1. **`CompleteProtocolSpec.model_validate(spec.model_dump())` (det)** — pydantic re-validation, narrower type.
2. **`SemanticExtractor.spec_to_schema(complete_spec, config)` (det)** — expands steps into per-command `ProtocolSchema`: `Transfer`, `Distribute`, `PickUpTip`, etc. Runs the deterministic tip-strategy algorithm. Returns schema + well-state warnings + step summaries.
   - **Branch:** any exception → abort, hint that spec has an inconsistency.

### Stage 6 — Schema → Python `(det)`

1. **`generate_python_script(protocol_schema)` (det)** — string-builds an Opentrons `protocol_api` Python file. One big `if cmd.command_type == ...` over every command type.
   - **Branch:** `ValueError` (unknown command, missing labware) → abort.
2. Always save script to `output/debug_script_*.py`.

### Stage 7 — Opentrons simulation `(det, external simulator)`

1. **`simulate_script(script)` (det)** — runs the generated `.py` through `opentrons.simulate`. Returns `(success, log, runlog)`.
   - **Branch:** `not success` → abort; user can inspect the saved debug script.

### Stage 8 — Intent verification `(LLM)`

1. **`validate_protocol(intent, spec, schema, config, …)` (LLM)** — final safety net asking the LLM whether the schema actually matches the user's original prompt. Returns confidence + issues.
   - **Branch:** if `not matches` or `confidence < 0.7` → abort.
2. Otherwise return `PipelineResult(script, simulation_log, protocol_schema, runlog, config)`.

## LLM vs deterministic, summarized

| Stage | Component | LLM? |
|---|---|---|
| 1 | `ConfigLoader.load_config` | det |
| 1 | `InputValidator.classify` | **LLM** |
| 2 | `SemanticExtractor.extract` | **LLM** |
| 3 | `verify_provenance_claims` | **LLM** |
| 3 | `missing_fields`, `fill_gaps` | det |
| 3 | `refine` (only if gaps remain) | **LLM** |
| 3 | `_infer_source_containers` | det |
| 3.5 | `LabwareResolver.resolve` | **LLM** |
| 3.5 | `_confirm_labware_assignments` | interactive (det) |
| 4 | `ConstraintChecker.check_all` | det |
| 4 | `_confirm_provenance_items` | interactive (det) |
| 5 | `spec_to_schema` | det |
| 6 | `generate_python_script` | det |
| 7 | `simulate_script` (Opentrons) | det |
| 8 | `validate_protocol` | **LLM** |

Six LLM call sites; the rest is straight Python.

## Contracts for the deterministic stages

A "contract" = inputs the function trusts, outputs it promises, invariants it preserves. Tests should pin these.

### `ConfigLoader.load_config`

- **Pre:** path exists, file is JSON.
- **Post:** `self.config` is a dict containing the keys downstream code reads (`labware`, `pipettes`, `modules?`, `tipracks?`).
- **Tests (unit, black-box):** valid config loads; missing required keys raise; unknown extra keys ignored; malformed JSON raises with file path in message.

### `extractor.missing_fields(spec)`

- **Pre:** `spec` is a well-formed `ProtocolSpec` (pydantic-validated).
- **Post:** returns a list of human-readable gap strings; empty iff every step has the fields needed downstream.
- **Invariant:** `missing_fields(fill_gaps(spec, config))` is a (non-strict) subset of `missing_fields(spec)`. Filling never introduces new gaps.
- **Tests:** equivalence classes — no gaps; missing volume; missing well; missing duration. Boundary: a step with one filled volume and one missing should report only the missing one.

### `extractor.fill_gaps(spec, config)`

- **Pre:** spec + valid config.
- **Post:** returns `(spec, fills)`. `spec.explicit_volumes` is unchanged (user-stated values are immutable). Filled values get `provenance.source != "instruction"`.
- **Tests:** filled values come from config defaults; explicit volumes never overwritten; `fills` list is a complete record of what changed.

### `_infer_source_containers(spec)`

- **Pre:** spec with at least one liquid-handling step.
- **Post:** list of `(labware, well, substance)` for wells that are aspirated-from but never dispensed-into and not already in `initial_contents`.
- **Invariant:** if a labware is ever a destination anywhere, none of its wells should appear in the result. (See line 451 — guards serial dilution where one plate is both src and dst.)
- **Tests:** simple transfer surfaces source; serial dilution does not surface its plate; already-tracked wells skipped; replicates and well_ranges handled.

### `ConstraintChecker.check_all(spec)`

- **Pre:** spec with `resolved_label` set on every `LocationRef`, valid config.
- **Post:** `ConstraintCheckResult` with `errors` and `warnings` lists. `has_errors == bool(errors)`.
- **Invariant:** physical constraints (pipette capacity) ignore provenance — an inferred 1500uL must still error.
- **Tests (mostly already exist):** equivalence classes per `ViolationType`; **boundary values** (20.0 µL on p20: pass; 20.01 µL: fail; well exactly at capacity: pass; capacity + 0.01: warn).

### `WellStateTracker.{aspirate,dispense}`

- **Pre:** initialized with config + spec; labware label exists in config.
- **Post:** `get_volume(labware, well)` reflects cumulative aspirate/dispense. Warnings appended for: aspirate from empty, aspirate more than available, overflow.
- **Invariant:** dispense then aspirate of equal volume returns to original volume; substance set is monotonically growing per well.
- **Tests:** already covered for the main cases; add a boundary test for capacity exactly hit (no warning) vs +0.01 (warn).

### `SemanticExtractor.spec_to_schema(spec, config)`

- **Pre:** `CompleteProtocolSpec` (stricter than `ProtocolSpec`), enriched config.
- **Post:** `(ProtocolSchema, well_state_warnings, step_summaries)`. Schema commands reference only labware/modules in config. Tip-strategy: a `PickUpTip` precedes every transfer that needs a fresh tip; redundant `DropTip`/`PickUpTip` pairs are eliminated across step boundaries.
- **Invariant:** number of `Transfer` commands equals total fan-out across (sources × destinations × replicates); every transfer's `volume` ∈ pipette range for its assigned pipette.
- **Tests (mostly already exist):** tip grouping by source well; `mix_after` forces new-tip-each; `tip_strategy` field is ignored (deterministic algorithm is authoritative); cross-step tip optimization; serial dilution generates the chain `A1→A1, A1→A2, …` and no spurious `distribute`.

### `generate_python_script(protocol)`

- **Pre:** `ProtocolSchema` validated, every `cmd.labware`/`cmd.module` reference resolves in the schema.
- **Post:** a string that is valid Python 3 and importable as an Opentrons protocol. Module load lines precede labware load lines that depend on them; pipette load lines precede commands.
- **Invariant:** unknown `command_type` raises `ValueError` (no silent drop). Single quotes in user-supplied messages are escaped.
- **Tests:** snapshot test per command type (small fixture schema → expected line); unknown command raises; `pause`/`comment` with apostrophe escapes correctly; module/labware/pipette ordering preserved.

### `simulate_script(script)`

- **Pre:** any string; will not raise on bad input.
- **Post:** `(success, log, runlog)`. On success, `runlog` is a list of dicts; on failure, an empty list and the exception message in `log`.
- **Tests:** mainly used as part of integration tests (below); a unit test that bad Python returns `(False, "Simulation failed: …", [])` is enough.

## What kind of tests for each contract

| Contract | Test style | Why |
|---|---|---|
| `load_config` | unit, black-box, equivalence + error cases | Pure function, easy oracle. |
| `missing_fields` / `fill_gaps` | unit, equivalence + invariant property | Multiple input shapes, single output shape. |
| `_infer_source_containers` | unit, equivalence + a regression case (serial dilution) | One subtle invariant; pin it. |
| `ConstraintChecker` | unit, equivalence + boundary value | Numeric thresholds — exactly where boundary analysis pays off. |
| `WellStateTracker` | unit, equivalence + boundary value | Same — capacity is a numeric threshold. |
| `spec_to_schema` | unit, specification-based; one regression per past bug | Behavior is fully specified by inputs; large state space, so equivalence classes matter more than coverage. |
| `generate_python_script` | unit, snapshot per command type | Output is a string with stable structure; snapshot is the cheapest oracle. |
| `simulate_script` | unit, smoke + error path | Wrapper around an external library. |

For the LLM-using stages, unit tests aren't the right tool. They produce non-deterministic output, so a single-input/single-output assertion is brittle. Three options instead:

1. **Mock the client.** Inject a fake `Anthropic` that returns a canned response. Test the calling code's plumbing, not the model.
2. **Recorded responses (cassettes).** Use `vcrpy` or hand-rolled fixtures. Tests are deterministic; replay breaks if upstream call shape changes (a feature).
3. **Eval, not test.** Track pass-rate across a fixed prompt suite; CI enforces a floor (e.g. ≥80% extracts correctly), not a single-instance equality.

Currently we do option 0 (gate the test behind `--run-llm` and let it be flaky). Option 1 for unit-style coverage of the orchestration is the highest-leverage upgrade.

## Lossy projections

A second class of deterministic function — easy to miss, easy to break — sits between two stages and turns a structured object into text or a loose dict for a different consumer. They look like trivial helpers; the bug surface is *which fields they silently drop*. Whoever owns the producer model usually doesn't know the projection exists, so adding a field to the model leaves the projection blind to it.

### Heuristic: when a function deserves its own contract

Flag any function meeting **two or more** of:

- branches on the shape of a structured input (`if step.source.well_range elif wells …`),
- reads ≥ 3 fields off a single structured input,
- output is consumed by a *different* module — especially the LLM/det boundary,
- output is text fed to an LLM, regex, or template,
- return type is loose (`str`, `dict`, `list[str]`, `list[dict]`),
- has a sibling formatter for a different audience (drift between siblings is the failure mode).

### Inventory in this repo

| Function | Consumer | Reads from | What it omits today |
|---|---|---|---|
| `schema_builder._format_step_line` | Stage 8 validator (LLM) via `step_summaries[i].description` | `ExtractedStep` | (fixed) `post_actions`, `tip_strategy` were dropped — caused PCR mastermix Stage 8 false positive on 2026-04-30 |
| `extractor.format_for_confirmation` | TTY user (interactive prompt) | `ProtocolSpec` | sibling of `_format_step_line`; surfaces post-actions correctly. Drift risk against `_format_step_line`. |
| `validator.validate_protocol` (prompt-build block, lines 95–137) | Stage 8 validator (LLM) | `config`, `spec.initial_contents`, `spec.prefilled_labware`, `step_summaries`, `well_state_warnings` | Anything new added to `spec` or `config` is invisible to Stage 8 unless explicitly threaded in. |
| `extractor._extract_wells_from_text` | `_verify_instruction_claims` | instruction text | (fixed) block ranges (`A1-H3`) returned only endpoints — caused PCR mastermix Stage 3 fabrication false positive on 2026-04-30 |
| `extractor._expand_well_range` | `_extract_wells_from_text` | (start, end) | same as above; fixed |
| `extractor._extract_volumes_from_text` | `_verify_instruction_claims` | instruction text | unhandled units (`mL`) silently absent. |
| `extractor._extract_durations_from_text` | `_verify_instruction_claims` | instruction text | unit aliases not in `unit_map` silently absent. |
| `extractor._extract_non_volume_numbers` | `_verify_instruction_claims` | instruction text | numbers attached to non-numeric tokens (e.g. `1.5x`) handled? not tested. |
| `pipeline.generate_python_script` | Opentrons simulator | `ProtocolSchema` | per-command-type branches; missing branch raises, missing kwarg silently dropped. |
| `_infer_source_containers` (`pipeline.py`) | Stage 3 user prompt | `spec.steps`, `spec.initial_contents` | non-`liquid_actions` actions skipped — adding a new action that aspirates would silently miss source detection. |

### Contract: "every populated field reaches the consumer"

The standard test for a lossy projection is parametric: populate one field at a time and assert it appears (or visibly affects) the output.

```python
@pytest.mark.parametrize("field,value,expected_fragment", [
    ("post_actions", [PostAction(action="mix", repetitions=3, volume=_vol(10))], "mix 3x at 10"),
    ("tip_strategy", "new_tip_each", "new_tip_each"),
    ("temperature", _temp(37), "37"),
    ("duration", _dur(5, "minutes"), "5 minutes"),
])
def test_format_step_line_surfaces_field(field, value, expected_fragment):
    step = ExtractedStep(order=1, action="transfer", **{field: value}, ...)
    line = _format_step_line(step)
    assert expected_fragment in line
```

Run this against every projection in the inventory above. New field added to `ExtractedStep` → add a row to the parametrize. If the formatter wasn't updated, the new test fails immediately, and the failure points exactly at the formatter, not at the LLM consumer 100ms later.

### Why this is its own section

The earlier "deterministic stages" contracts focus on *behavior*: did the function compute the right thing? Lossy projections are different — the function is computing *something* fine, but it's silently leaving a field out. Tests have to assert on **completeness of the output**, which is a different shape of assertion. Keep them as a separate row in the doc and the test layout.

## Suggested integration tests

Integration tests cross stage boundaries. Each gives the deterministic tail (Stages 4–7) end-to-end coverage without paying for the LLM head.

1. **Spec → simulator (no LLM).** Hand-craft a `ProtocolSpec` (like the unit tests already do), run it through Stages 4 → 5 → 6 → 7. Assert `simulate.simulate(...)` succeeds and `runlog` is non-empty. This is the single most valuable test we don't have — it exercises four deterministic stages plus the external Opentrons API.
   - Fixtures: simple_transfer, qpcr, serial_dilution. Use the existing configs in `test_cases/examples/`.

2. **Failure-mode integration.** For each `test_cases/failure_modes/<case>/`, hand-craft (or load a snapshot of) the spec and run Stages 4–6. Assert the pipeline aborts at the expected stage. Pin the abort reason (e.g. `pipette_insufficient` aborts at Stage 4 with a `PIPETTE_CAPACITY` violation).

3. **Mocked-LLM full pipeline.** Inject a fake `Anthropic` client whose `messages.create` returns canned `ProtocolSpec` JSON for a known prompt. Run `ProtocolAgent.run_pipeline` end-to-end with `skip_validation=True` to bypass the input-classifier LLM call too. Assert a `PipelineResult` is returned and `result.script` contains expected substrings (`load_labware`, `transfer`).
   - This is the test that catches stage-wiring bugs (e.g. a refactor that drops `resolved_label` between Stage 3.5 and 5).

4. **Generated-script smoke.** Take a known-good `ProtocolSchema` fixture, run `generate_python_script`, write to tmp, `import importlib.util` it, assert it parses and the `run` function is callable. Cheap, catches syntax errors that snapshot tests miss.

5. **Round-trip determinism.** Same `ProtocolSpec` through `spec_to_schema` twice → identical schemas. Catches accidental nondeterminism (dict iteration order, set ordering) which is easy to introduce and hard to notice.

Tests 1 and 3 together cover the deterministic backbone. Test 2 turns the curated failure-mode cases from a manual library into an automated regression suite.
