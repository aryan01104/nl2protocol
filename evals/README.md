# Evals

The eval layer for nl2protocol. The only test layer that runs the **real** Anthropic API — every other layer (contract, property, boundary, integration) uses mocks. Evals catch the class of bug no mock can detect: **model-behavior regressions**.

## What an eval is

Each `evals/<name>/expected.json` is the *oracle* for one scenario. It declares loose invariants that must hold regardless of how the LLM phrases its response — e.g., "extracted volumes include 50.0", not "summary string equals 'Transfer 50uL'".

Each eval references a canonical `(instruction.txt, config.json)` pair in `test_cases/` rather than copying it. Single source of truth; evals describe assertions, not inputs.

```
evals/
  README.md
  run.py
  01_simple_transfer/
    expected.json     ← {"source": "test_cases/examples/simple_transfer", invariants...}
  02_serial_dilution/...
  03_pcr_mastermix/...
  04_pipette_insufficient/...
  05_labware_missing/...
```

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

Exit code is 0 iff every eval passes. Each eval makes one real Claude API call (the extraction stage); refinement / verifier / intent-verification stages are not exercised by this runner — they're separate concerns.

## What evals do NOT measure

- **Model output quality in absolute terms** — these are pass/fail invariants, not graded scores. For real model evaluation you'd need LLM-as-judge or human review (out of scope for this set).
- **Stages 3 / 6 / 7 / 8 of the pipeline** — confirmation prompts, simulation, intent verification. The runner stops after constraints. Adding more stages would couple the runner to UX that's hard to mock.
- **Generated Python script execution.** That's the Opentrons simulator's job (Stage 6); evals here cover semantic extraction + hardware constraints, not script runtime behavior.

## Adding a new eval

1. Create `evals/<NN>_<name>/expected.json`.
2. Set `source` to a path under `test_cases/` (or add a new `test_cases/` entry first).
3. Declare invariants in the `extract` / `spec` / `constraints` blocks.
4. Run `python evals/run.py --eval <NN>_<name>` to verify it passes on the current model.
5. Commit. The next time someone bumps the model or edits the prompt, this eval will be one of the things that says "yes, that change kept this scenario working."
