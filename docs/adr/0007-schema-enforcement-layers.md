# ADR-0007: Two layers of schema enforcement — Pydantic fields vs model validators

**Status:** Accepted (post-hoc documentation of an established pattern)
**Date:** 2026-05-04

## Context

The protocol spec carries many invariants. Some are simple presence rules ("a volume must have provenance"); others are cross-field rules ("a transfer step must have source AND destination"); others are graded quality rules ("inferred values with confidence < 0.7 need user confirmation"). Pydantic offers two enforcement points for these:

- **Field-level types** — `field: T` (required) or `field: Optional[T] = None` (optional) on the model. Enforced at parse time, before any custom logic runs.
- **Model validators** — `@model_validator(mode='after')` methods that run after parsing. They see the whole model and can collect, compare, raise, or return warnings.

Until now, the codebase has used both layers without an explicit rule for which goes where. The result: most invariants ended up in the right place by instinct, but a few (notably `LocationRef.provenance: Optional[Provenance]`) drifted into the wrong layer and stayed there until ADR-0006 surfaced them.

This ADR makes the rule explicit. It's a documentation ADR, not a code change — the pattern already exists; we're naming it.

## Decision

The two layers serve different jobs. Pick the layer that matches the invariant's *shape*:

| Layer | Use for invariants that are… | Examples in this codebase |
|---|---|---|
| **Field-level** (Pydantic types) | Unconditional, atomic, single-object | `ProvenancedVolume.provenance: Provenance` (every populated volume MUST carry provenance, period) |
| **Model validator** | Conditional, cross-field, graded, or cross-system | `validate_completeness` (transfer requires source AND destination — conditional on `action`); fabrication detection (cited_text must appear in instruction — cross-system); confidence thresholds (graded) |

The deciding question for each invariant: *can Pydantic express it in one line on the type, with no reference to other fields or external state?*

- **Yes** → field-level. Cheapest possible enforcement; runs at parse time; downstream code can rely on the type without null checks.
- **No** → validator-level. Has full model in scope; can collect issues across all fields/steps; can choose to raise (hard fail) or return warnings (soft, route to confirmation queue).

### Why field-level is preferred when it fits

When an invariant is field-level-expressible, putting it in a validator instead is strictly worse:

1. **Errors land later.** Validator runs after parsing succeeds — by then, downstream code that traverses the model has to defensively null-check fields that "should" be set. Field-level enforcement lets that code use `step.volume.provenance.source` without `if .provenance` guards.
2. **Errors are less specific.** Pydantic's field-level errors point exactly at the offending field. Validator errors are aggregated messages.
3. **Refactor safety drops.** A field-level requirement is part of the type; renaming or deleting the field is impossible without the type system catching every reference. Validator requirements are just strings buried in method bodies.

### Why validator-level is preferred when it must be

Three reasons an invariant CAN'T be field-level:

1. **Cross-field conditionals.** "If `action` is `transfer`, then `source` must be present" — Pydantic's type system cannot express "required-given-other-field". This is `validate_completeness`'s domain (ADR-0006).
2. **Cross-system checks.** "Cited_text must actually appear in the instruction string" — needs both the spec and the original instruction, which Pydantic doesn't have at parse time. This is `_verify_claimed_instruction_provenance`.
3. **Graded / soft outcomes.** "Confidence below 0.7 → flag for user confirmation, don't reject." Pydantic types are binary: present-and-valid or rejected. Anything that needs to *report* rather than *raise* must live in a validator. This is `_flag_uncertain_claims`.

## How this maps onto provenance specifically

Provenance is the load-bearing case study for the pattern, so worth tracing through:

| Invariant | Layer | Where it lives |
|---|---|---|
| Every `ProvencedVolume/Duration/Temperature/Substance` has provenance | Field-level (required) | `models/spec.py:223,230,236,242` |
| Every populated `LocationRef` has provenance | Field-level (required, post-ADR-0007) | `models/spec.py:274` |
| Every `ExtractedStep` has `composition_provenance` | Field-level (required) | `models/spec.py:305` |
| Provenance has either `cited_text` OR `reasoning` (mutually exclusive by source) | Validator | `models/spec.py:Provenance.require_one_of` |
| `composition_provenance.grounding` must include `"instruction"` | Validator | `models/spec.py:CompositionProvenance.require_instruction_grounding` |
| `domain_default` grounding requires `step_reasoning` | Validator | `models/spec.py:CompositionProvenance.require_step_reasoning_for_domain_expansion` |
| Cited substring actually appears in the instruction | Validator (extractor pass) | `extraction/extractor.py:_verify_claimed_instruction_provenance` |
| `inferred` provenances flagged for user confirmation | Validator (extractor pass) | `extraction/extractor.py:_flag_uncertain_claims` |
| Action-required fields present (per-action completeness) | Validator | `models/spec.py:CompleteProtocolSpec.validate_completeness` (ADR-0006) |

The pattern: presence of provenance is field-level; everything about *what* the provenance says is validator-level. After closing the LocationRef gap (also today), the field-level layer is structurally complete: `if a value exists, it has provenance` is now a parse-time guarantee with no exceptions.

## Consequences

**Positive:**

- New contributors have an explicit rule: field-level for unconditional atomic invariants, validator-level for everything else. No need to re-derive the choice from scratch on every PR.
- Schema gaps like the `LocationRef.provenance: Optional` mistake become easy to spot — they're invariants that *could* be field-level but aren't.
- Downstream code (renderer, schema builder, constraint checker) can rely on field-level guarantees without defensive null checks. Reduces noise and removes a class of latent bugs.

**Negative / costs:**

- Field-level required means parse failures are HARD failures. Recoverable only at the LLM layer (re-prompt with error). For this project that's acceptable: the prompt explicitly demands provenance, the LLM rarely violates it, and silent acceptance of invalid input is worse than a clean retry. But this trade-off must stay visible to future contributors who might want to add soft-recovery paths.

- Validators that report (rather than raise) need their own routing infrastructure. Today that's the confirmation queue + fabrication-block path through pipeline.py. Adding a new soft-validator means wiring it into one of those queues, not just appending to a list. This isn't a downside of the pattern, just a fact future contributors should know.

## Related — retry-on-validation-failure

Field-level enforcement makes Pydantic the first failure point for malformed LLM output. Today, an extractor parse failure propagates up to the user as an error. A future ADR should consider whether to introduce a retry loop at the extractor layer: catch `pydantic.ValidationError`, surface the error message back to the LLM as feedback, re-run the call, max N attempts. This pattern is well-established in tool-using-LLM systems and would let us tighten more invariants to field-level without increasing user-facing failure rates.

This is deferred — orthogonal to where invariants *live*, which is what this ADR settles. But it's the natural follow-up: the harder our schema gets, the more the extractor needs a graceful retry path.

## References

- ADR-0002 — provenanced ProtocolSpec (introduces the provenance fields this ADR governs)
- ADR-0005 — cited_text on CompositionProvenance (refines what provenance content means)
- ADR-0006 — CompleteProtocolSpec completeness contract (uses validator-level for conditional presence)
- `nl2protocol/models/spec.py` — all field-level types referenced above
- `nl2protocol/extraction/extractor.py:441-676` — validator-level provenance checks (`_verify_claimed_instruction_provenance`, `_verify_config_claims`, `_flag_uncertain_claims`)
