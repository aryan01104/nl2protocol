# ADR-0006: Tighten `CompleteProtocolSpec` to enforce per-action completeness

**Status:** Accepted
**Date:** 2026-05-04

## Context

`CompleteProtocolSpec` is a Pydantic subclass of `ProtocolSpec` that adds a single validator, `validate_completeness`. Its docstring says: *"all fields required for code generation are present."* That promise is the entire reason the type exists — it lets downstream stages assume their input is filled in.

The current validator (`models/spec.py:458–511`) is narrower than the docstring:

```python
liquid_actions   = {"transfer","distribute","consolidate","aspirate","dispense","mix","serial_dilution"}
transfer_actions = {"transfer","distribute","serial_dilution","consolidate"}
# liquid action → require volume
# transfer action → also require source + destination
# Other actions (e.g. delay, set_temperature) impose no completeness requirements.
```

The "other actions" gap quietly admits: `set_temperature`, `wait_for_temperature`, `pause`, `delay`, `comment`, `engage_magnets`, `disengage_magnets`, `deactivate`. Every one of these has a parameter that codegen needs (a temperature, a duration, a note); none of them are checked.

The gap is masked by `extraction/schema_builder.py`. At command-emission time, `wait_for_temperature` runs a fallback chain — `step.temperature` → regex-parse `step.note` for `\d+°C` → walk back to the most recent `set_temperature` command → hardcoded 4.0°C. So a sparse spec can produce a working Opentrons program. Simulation passes. End-to-end tests had nothing to fail on, because the schema builder silently filled in what the spec had left out.

The provenance report surfaced the gap: `wait_for_temperature` blocks rendered parameter-less even in the "Complete Spec" column, while the generated Python carried the inferred celsius. A reader saw a bare step and asked: *"how is this complete?"* It wasn't.

The test suite (`tests/test_spec_validators.py:208`, `TestValidateCompleteness`) had 7 tests, every one matching exactly what the validator covered, including `test_delay_step_without_volume_ok` which **explicitly asserts** the gap is allowed. The tests followed the implementation, not the contract implied by the name. A future contributor reading the docstring would have a different mental model than the validator enforces.

## Decision

Tighten `validate_completeness` so the contract matches the name. Every action listed in `ActionType` gets a per-action completeness rule based on what codegen actually consumes. The schema builder's silent fallbacks for module commands become redundant; they stay as belt-and-suspenders for now but lose their structural role.

Action → required fields (post-validation invariant):

| Action                 | Required for codegen                          |
|------------------------|-----------------------------------------------|
| transfer / distribute / serial_dilution / consolidate | volume, source, destination |
| aspirate / dispense / mix | volume                                      |
| set_temperature        | temperature                                   |
| wait_for_temperature   | temperature                                   |
| delay                  | duration                                      |
| pause                  | duration **OR** note (one or the other)       |
| comment                | note                                          |
| engage_magnets / disengage_magnets / deactivate | (no per-action requirement) |

`pause` is the one polymorphic case — a timed pause needs duration; a user-driven pause needs the note that explains what the user is doing. Either satisfies the contract; both is fine.

Why per-action rules instead of a global "every field non-null" rule: many actions intentionally leave fields null (a `set_temperature` step has no source/destination by design). The contract should reflect what each action *needs*, not impose a uniform shape that would force fake fillers.

## Consequences

**Positive:**

- The name and the validator agree. `CompleteProtocolSpec` actually means "complete for codegen."
- Provenance report no longer renders parameter-less module-command steps in the Complete Spec column; gaps either get filled before promotion (with `source="inferred"` provenance, surfacing in the visualization with the ▴ marker) or fail validation loudly.
- Future contributors can rely on the contract instead of re-discovering schema-builder fallbacks.

**Negative / costs:**

- Specs the LLM produced sparsely (e.g. `wait_for_temperature` without temperature) now fail promotion. We must either (a) backfill before promotion using the inheritance chain the schema builder already encodes, or (b) tighten the extractor prompt to require the field, or (c) both. We do (a) immediately and (b) opportunistically — the prompt already asks for full provenance, and the inheritance backfill is the right fallback when the LLM forgets.
- Existing pipeline_state.json examples that pass today's narrow validator may fail tomorrow's. Acceptable: those are debug artifacts, not load-bearing fixtures.

## Scope of the inheritance backfill (deliberately narrow)

The backfill referenced above is **scoped to exactly one action: `wait_for_temperature`**. It is NOT a general-purpose gap-filler. The justification is purely semantic, not heuristic:

`wait_for_temperature` literally means *"wait until the module reaches its target."* The target can only refer to the most recent `set_temperature.temperature` in the same protocol — there is no other coherent referent. So when the LLM omits this field, the value isn't *guessed*; it's *recovered from the action's semantics*. This is why the backfill is permissible without harming the contract.

Other actions deliberately get NO backfill, even when a value seems "obvious":

| Action                  | Why no backfill                                                                 |
|-------------------------|---------------------------------------------------------------------------------|
| `delay`                 | No contextual referent for "how long" — must come from instruction or fail.    |
| `pause` (no duration)   | User-driven pauses are intentional; the `note` is the meaningful field.        |
| `comment` (no note)     | Cannot be inferred from context.                                               |
| Liquid actions (no volume) | Already routed through user confirmation as "missing volume" — the right place to fill these is at the confirmation step, not silently. |

The hardcoded character of the `wait_for_temperature` rule is intentional. If we ever find another action whose parameter is similarly *forced by semantics* (not just plausible-given-context), it gets a sibling rule with its own ADR justification — not a generalized "fill any null" pass.

Implementation lives in `nl2protocol/extraction/extractor.py` as `fill_module_gaps` (sibling to the existing `fill_gaps`), called from stage 3 alongside the labware-content gap-fill. Pipeline.py stays an orchestrator — it does not decide what to fill, only when to call the filler. The inferred value is attached with `Provenance(source="inferred", reasoning="Inherited from prior set_temperature step (X°C). The instruction did not re-state the target temperature for this wait.", confidence=0.95)`. Confidence 0.95 places it above the default 0.7 confirmation threshold so it auto-accepts; the report renders it with the ▴ marker and surfaces the reasoning on hover, so the user can always see *why* the value is there even though they didn't have to confirm it.

**Test coverage:**

- One contract test per row in the table above: positive (passes when required fields are present) + negative (fails with a specific error message when required fields are missing).
- Existing `test_delay_step_without_volume_ok` becomes `test_delay_without_duration_raises` — the assertion flips because the contract changed. The old test documented the gap; the new one documents the fix.

## Alternatives considered

**(A) Rename to `LiquidHandlingCompleteSpec`, keep contract narrow.** Cheapest. Makes the design honest at the type level and pushes module-command completeness to the schema builder where it lives today. Rejected because the schema builder's fallbacks are heuristic (regex on note text, hardcoded 4.0°C default) — not the kind of logic that should be the only line of defense.

**(C) Add a runtime "spec completion" pass between stage 4 and stage 5.** Fills inferable values in place with `source="inferred"` provenance, then validates with the existing narrow validator. This is what we now do AS PART OF (B) — the backfill is necessary either way. The difference is whether the validator catches gaps the backfill missed; under (B) it does, under (C) alone it doesn't.

## Deferred / future work

- **Surface fill counts in the report.** The `fill_lookup_and_carryover_gaps` function returns a `fills: List[str]` describing every value it filled, and pipeline logs them to stderr. We do NOT currently surface this count or list in the HTML provenance report. A future revision should:
  - Add a header stat counting how many atomic values were filled by deterministic gap-filling (separate from instruction / domain_default / inferred-by-LLM).
  - Visually distinguish gap-filled values from LLM-produced inferred values — they share `source="inferred"` today but the *origin* differs (deterministic context vs LLM judgment), and a careful reader benefits from telling them apart at a glance. Possible mechanism: a sub-marker (e.g. ▴⊕ for "filled by lookup/carryover" vs plain ▴ for LLM inference), or a distinct CSS class wired off a sentinel string in the reasoning field.
- **Confirmation queue routing.** Today a 0.95-confidence carryover fill auto-accepts under the default 0.7 threshold. If user feedback shows the silent fills surprise readers, lower the carryover confidence or carve out a "deterministic-fill-always-confirms" channel.

## References

- `nl2protocol/models/spec.py:447–511` — `CompleteProtocolSpec.validate_completeness`
- `nl2protocol/extraction/schema_builder.py:743–759` — `wait_for_temperature` fallback chain (origin of the silent compensation)
- `nl2protocol/pipeline.py:1146–1153` — promotion site
- `tests/test_spec_validators.py:208` — existing completeness contract tests
- ADR-0002 — establishes ProtocolSpec / CompleteProtocolSpec separation
- ADR-0005 — cited_text on CompositionProvenance (related, narrows what "provenanced" means)
