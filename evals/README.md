# Evals

The eval layer for nl2protocol. The only test layer that runs the **real** Anthropic API — every other layer (contract, property, boundary, integration) uses mocks. Evals catch the class of bug no mock can detect: **model-behavior regressions**.

## What an eval is

Each `evals/<name>/expected.json` is the *oracle* for one scenario. It declares loose invariants that must hold regardless of how the LLM phrases its response — e.g., "extracted volumes include 50.0", not "summary string equals 'Transfer 50uL'".

Each eval references a canonical `(instruction.txt, config.json)` pair in `test_cases/` rather than copying it. Single source of truth; evals describe assertions, not inputs.

```
evals/
  README.md
  run.py
  01_simple_transfer/        happy: baseline multi-well transfer
  02_serial_dilution/        happy: 2-fold dilution chain
  03_pcr_mastermix/          happy: 10.5uL preservation (motivated the spec/schema split)
  04_pipette_insufficient/   failure: PIPETTE_CAPACITY violation
  05_labware_missing/        failure: LABWARE_NOT_FOUND violation
  06_module_missing/         failure: MODULE_NOT_FOUND violation
  07_combined_config_gaps/   failure: multiple violation types in one spec
  08_equivalent_names/       happy/resolver: user-language → config labels
  09_compact_instruction/    happy: terse-syntax robustness
  10_misspelled_instruction/ happy: typo robustness
  11_distribute/             happy: distribute action type
  13_magnetic_bead_cleanup/  happy: magnetic module + multi-step (NOT YET VERIFIED — pending API credits)
  14_bradford_assay/         happy: mixed provenance / domain_default routing (NOT YET VERIFIED — pending API credits)
```

13 evals shipped. Numbering has a gap at 12 — `12_qpcr_standard_curve` was dropped during initial verification because the labware resolver couldn't match its informal labware names ("tube rack" → `reagent_tube_rack`) to the config's snake_case labels. That class of complex-config eval can come back when task #4 (orchestration refactor) lets the eval runner drive the user-confirmation queue with scripted answers — the resolver weakness is recovered by user confirmation in production, but the eval runner currently bypasses that stage. See [Deferred assertions](#deferred-assertions-intentionally-not-yet-in-expectedjson) below.

## Why this layer exists (vs. manual runs of `examples/`)

Manual runs of the canonical examples catch "I broke something obvious" at development time. They don't catch:

- **Model upgrade silent drift.** Claude 4.6 → 4.7 changes JSON formatting in subtle ways: properties + integration tests pass; nothing flags it.
- **Prompt drift.** A prompt edit makes the model hedge ("around 100uL") instead of extracting verbatim → user volumes get marked `exact=False` → downstream science silently breaks.
- **Hallucinated structure.** The model emits an extra step or wrong action type that happens to validate against Pydantic.
- **Per-instruction behavior shifts.** A new instruction phrasing fails extraction even though similar instructions still work.

Manual runs are exploratory testing (per CS5150 lec15 #11) — not reproducible, no oracle, can't measure coverage. Evals are regression testing with an explicit oracle. Both have value; evals are the layer manual runs are bad at.

## Invariants schema

`expected.json` supports three blocks. Each block is optional; absent means "don't check."

```json
{
  "source": "test_cases/<path>",                      // required
  "description": "human-readable purpose of this eval",

  "extract": {
    "should_succeed": true                             // false = expect None
  },

  "spec": {
    "step_count_at_least": 1,
    "step_count_at_most": 5,
    "explicit_volumes_include": [50.0, 100.0],         // floats from instruction text
    "actions_present": ["transfer", "mix"]             // ActionType values
  },

  "constraints": {
    "expect_errors": false,                            // any ERROR-severity violation
    "violation_types_required": ["PIPETTE_CAPACITY"]   // ViolationType.name values
  }
}
```

The invariants are deliberately **loose**:
- `explicit_volumes_include` checks containment, not equality (LLM may extract additional incidental volumes).
- `actions_present` checks subset, not equality.
- `step_count_at_least`/`at_most` is a range, not an exact match.

This makes evals robust to LLM phrasing variation while still catching "the LLM forgot the user's stated volume" or "the LLM dropped a step entirely."

## When to run

| When | Action |
|---|---|
| Per commit (CI) | **NOT run** — too slow, costs API tokens, non-deterministic |
| Before a release | Run all evals; require all to pass before tagging |
| When bumping Claude model version | Run on old + new model; compare scores. **The reason this layer exists.** |
| When changing the system prompt | Same — prompt edits are model-behavior changes |
| When adding a new instruction shape (e.g., centrifugation) | Add an eval for it; track score over time |

## Running

```bash
ANTHROPIC_API_KEY=... python evals/run.py                       # all evals
ANTHROPIC_API_KEY=... python evals/run.py --eval 04_pipette_insufficient
```

Exit code is 0 iff every eval passes. Each eval makes one real Claude API call to the extraction stage and one to the labware resolver — refinement, simulation, and the (now-removed) intent verifier are not exercised. Stage 1 input classification is also bypassed (different code path).

## What evals do NOT measure

- **Model output quality in absolute terms** — these are pass/fail invariants, not graded scores. For real model evaluation you'd need LLM-as-judge or human review (out of scope for this set).
- **CLI / interactive UX** — the runner calls the Python API directly (`SemanticExtractor.extract`, `LabwareResolver.resolve`, `ConstraintChecker.check_all`). It does NOT invoke `ProtocolAgent.run_pipeline` or any of the `_prompt_input` confirmation flows. Confirmation-queue behavior, stage banners, and CLI output are all UX concerns tested elsewhere (manually via `examples/`, deterministically via `tests/integration/`).
- **Stages bypassed by design**: Stage 1 (input classification — separate LLM call), Stage 3 (interactive confirmations — UX), Stage 5b (deterministic script generation — covered by `tests/integration/`), Stage 6 (Opentrons simulation — slow, covered by `examples/`), Stage 7 ~~intent verification~~ (removed in [ADR-0004](../docs/adr/0004-remove-intent-verifier.md)).
- **Generated Python script execution.** That's the Opentrons simulator's job; evals here cover semantic extraction + labware resolution + hardware constraints, not script runtime behavior.

## Deferred assertions (intentionally not yet in `expected.json`)

The runner only asserts on three things today: did extraction succeed, basic spec shape (step count, action types, explicit volumes), and constraint check expectations. Several other useful invariants are accessible from the same data we already produce, and would meaningfully tighten the eval signal — but require extending the schema and runner. Documented here so a later pass can pick them up without re-discovering the design space:

| Deferred assertion | What it would catch | Why valuable |
|---|---|---|
| **`spec.provenance_for_volumes`** — assert specific volumes carry the expected provenance source (`"instruction"` vs `"domain_default"` vs `"inferred"`) | LLM hedging: e.g., `bradford_assay`'s 5-min incubation should be `domain_default`, not `instruction`. User-stated `100uL` should be `instruction`, never `inferred`. | Tests the load-bearing invariant of [ADR-0002](../docs/adr/0002-provenanced-protocol-spec.md) — provenance routing decides what gets confirmed by the user vs. accepted automatically. |
| **`spec.resolved_labels_include`** — assert specific user-language labware descriptions resolved to specific config labels | E.g., for `08_equivalent_names`: `"Eppendorf tubes" → "tube_rack"`. Tests the resolver succeeded, not just that constraints didn't fire. | Stronger signal than "no constraint errors" — currently the resolver could resolve to the wrong label and as long as the wrong label is *also* in the config, no error fires. |
| **`constraints.expect_warnings`** (boolean) | Tests that warnings (vs errors) fire when expected. E.g., a spec with assumed-volume initial contents should produce specific warning strings. | Cheap; warnings are second-class citizens of constraint-check output and currently invisible to evals. |
| **Stage 1 classification** — extend the runner to call `InputValidator.classify` first | Tests that `nonsensical_instruction` cases are rejected at Stage 1, not extraction. | Different LLM call (Haiku); needs runner extension. Worth adding when there's a clear regression pattern to catch. |

These additions are all **additive** — existing `expected.json` files would keep passing as written. None are blocking the current eval set's value; deferred until a clear regression makes them necessary.

## Blocked on task #4: full-pipeline evals with scripted confirmations

The biggest expansion of the eval layer — covering the user-confirmation queue (Stage 3), the deterministic schema/script generation (Stage 5), and the Opentrons simulator (Stage 6) — is **blocked on the pipeline.py orchestration-vs-UX refactor (task #4)**.

Today the runner calls `SemanticExtractor.extract` → `LabwareResolver.resolve` → `ConstraintChecker.check_all` directly via the Python API. It deliberately bypasses `ProtocolAgent.run_pipeline()` because that orchestrator is coupled to interactive `_prompt_input(...)` calls scattered through 1230 lines — there's no clean injection point for canned answers.

Task #4 introduces a `ConfirmationManager` interface (`InteractiveCM` for the CLI, `ScriptedCM` for tests/evals) that separates orchestration from interactive UX. Once it lands, the eval runner gains a second mode:

```python
# Today (direct-API mode, unchanged)
spec = extractor.extract(instruction, config)
spec = resolver.resolve(spec)
result = checker.check_all(spec)

# After task #4 (full-pipeline mode, new):
agent = ProtocolAgent(config_path, confirmation_manager=ScriptedCM(answers))
pipeline_result = agent.run_pipeline(instruction)
# Assert on pipeline_result.script, .simulation_log, etc.
```

`expected.json` gains an optional `confirmations` block declaring canned answers per scenario — labware resolution mappings, initial-volume assumptions, default-confirm-all behavior. The runner reads it and constructs a `ScriptedCM` per eval. Existing 13 evals stay in direct-API mode (no `confirmations` block); new evals that need the full pipeline opt in.

What this unlocks (none possible today):

- **The complex resolver evals dropped earlier** (e.g. `12_qpcr_standard_curve`) come back as full-pipeline tests with scripted resolution answers — exercising the production path where Stage 3 confirmation patches up resolver weaknesses with the user.
- **Gap-resolution orchestrator end-to-end** (`InitialContentsVolumeDetector`, `ConstraintViolationDetector`, `LabwareAmbiguityDetector`, plus the suggesters and reviewer pass) gets exercised against real LLM extraction output, not just unit-tested with hand-crafted specs. Replaces the pre-PR3b `_build_initial_volume_queue` grouping affordance — currently regresses to per-Gap prompting; future `BatchedInitialContentsVolumeDetector` would re-add the grouping signal.
- **Stage 1 input classification** becomes testable in the eval layer — the runner enters at the top of `run_pipeline()`, including the `InputValidator.classify` LLM call.
- **Generated script + simulation log** become assertable — `PipelineResult.script` and `result.simulation_log` are already in the dataclass; we just don't run that far today.
- **Stage 5 deterministic transforms** get a real-LLM upstream signal — currently their inputs are hand-crafted Pydantic instances in `tests/integration/`; with full-pipeline evals, the inputs come from actual LLM extraction.

Order of operations after task #4:
1. Extend `evals/run.py` to support both modes (direct-API and full-pipeline-with-scripted-confirmations). Two-mode coexistence keeps existing 13 evals fast and unchanged.
2. Re-add `12_qpcr_standard_curve` (and possibly `15_bacterial_transformation`, others) with `confirmations` blocks.
3. Promote the relevant items from "Deferred assertions" above into first-class `expected.json` schema fields.

## Adding a new eval

1. Create `evals/<NN>_<name>/expected.json`.
2. Set `source` to a path under `test_cases/` (or add a new `test_cases/` entry first).
3. Declare invariants in the `extract` / `spec` / `constraints` blocks.
4. Run `python evals/run.py --eval <NN>_<name>` to verify it passes on the current model.
5. Commit. The next time someone bumps the model or edits the prompt, this eval will be one of the things that says "yes, that change kept this scenario working."
