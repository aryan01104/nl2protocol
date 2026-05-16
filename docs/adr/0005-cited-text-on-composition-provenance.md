# ADR-0005: Structured cite-and-reasoning for both atomic and composite provenance

**Status:** Accepted
**Date:** 2026-05-03
**Supersedes (in scope):** ADR-0003 (which proposed `cited_text` on atomic `Provenance` only and excluded `CompositionProvenance` "because composite cannot be reduced to a single quote")

## Context

ADR-0002 introduced provenance objects: a `Provenance` per atomic value (volume, well, substance), and a `CompositionProvenance` per `ExtractedStep`. Both currently mix the *citation* (where the claim grounds in instruction text) and the *reasoning* (how the claim follows from that text) into a single free-text `reason` / `justification` string. ADR-0003 proposed promoting the citation to a structured `cited_text` field — but only on atomic `Provenance`, deliberately excluding composite on the grounds that "a step's existence often grounds in a synthesis of multiple sources and cannot be reduced to a single quote."

ADR-0004 removed Stage 8 (the LLM intent verifier) and committed to "the LLM is allowed to interpret natural language but never to inject things the user did not ask for." A subsequent schema change (in `tighten-composition-grounding`) tightened `CompositionProvenance.grounding` to require `"instruction"` — so every step now provably has at least one instruction phrase that triggered it.

That tightening makes ADR-0003's exclusion of composite no longer the right call. Every composite now *does* have a guaranteed instruction phrase to cite. And the visualization work (the HTML provenance report — Phase 3 will draw arrows from instruction spans to spec values and step blocks) needs structured spans on **both** atomic and composite provenance to render reliably. Substring-search recovery on free-text `reason` is unreliable; structured citations are exact.

There's a deeper modeling insight that becomes visible once we sit with composite. A step's provenance answers TWO distinct questions, not one:

- **Q1 — Step existence:** *Why does a step of this kind exist at all?* Answered by the instruction phrase that triggered it (e.g., user said *"add 2uL plasmid DNA"* → there's a `transfer` step). Optionally augmented by a domain expansion (*"do a Bradford assay"* → multiple steps; the domain reasoning explains why each step exists).
- **Q2 — Parameter cohesion:** *Why do these specific parameter values belong to this same step?* Answered by one or more instruction phrases that ground the values together (volume + source + destination + substance) plus a paragraph explaining how those phrases combine into one operation.

The current `CompositionProvenance.justification` collapses both questions into a single sentence ("User explicitly described this transfer"). That's prose-readable but architecturally weak — it can't drive structured visualization, can't be machine-verified per-question, and elides the very split that makes provenance debuggable.

## Decision

Redesign both provenance types around the same principle: **structured cite + reasoning, separated by which question is being answered.** Citations point to the verbatim instruction substring; reasoning explains how the cited text justifies the claim. The two are stored in separate fields, never mixed.

### Atomic `Provenance` (per-value)

```python
class Provenance(BaseModel):
    source: Literal["instruction", "domain_default", "inferred"]
    cited_text: Optional[str] = None     # required when source == "instruction"
    reasoning: Optional[str] = None      # required when source in {"domain_default", "inferred"}
    confidence: float
```

Conditional invariant via `model_validator`:
- `source == "instruction"` → `cited_text` is required, `reasoning` is None.
- `source in {"domain_default", "inferred"}` → `reasoning` is required, `cited_text` is None.

`"config"` is removed from the source `Literal`. The extractor LLM does not have access to the lab config; no extractor-emitted provenance can legitimately be config-grounded. Layers that DO have config visibility (LabwareResolver, constraint checker) update other fields (`LocationRef.resolved_label`, etc.) but do not construct `Provenance` objects.

### Composite `CompositionProvenance` (per-step)

```python
class CompositionProvenance(BaseModel):
    # Q1: why this step exists
    step_cited_text: str                          # verbatim phrase that triggered this step kind
    step_reasoning: Optional[str] = None          # only when grounding includes "domain_default";
                                                  # explains how the cited phrase expanded to this step
    # Q2: why these specific values cohere as one step
    parameters_cited_texts: List[str]             # one or more verbatim phrases that ground the values
    parameters_reasoning: str                     # one paragraph linking the cites to these specific parameters

    grounding: List[Literal["instruction", "domain_default"]]  # MUST include "instruction"
    confidence: float
```

The two question-pairs are independent — Q1's citation is the high-level trigger; Q2's citations ground the specific parameter values. Often (for simple instructions) they reference the same instruction phrase; for compound steps (named protocols expanded by domain knowledge) they may differ.

The old `justification` field is removed; it conflated the two questions.

### Why the split matters in practice

Consider a Bradford-assay incubation step expanded from `"do a Bradford assay"`:
- Q1 (existence): `step_cited_text="do a Bradford assay"`, `step_reasoning="the standard Bradford workflow includes a 5-min incubation between dye addition and absorbance read."` Grounding = `["instruction", "domain_default"]`.
- Q2 (parameters): `parameters_cited_texts=["do a Bradford assay"]`, `parameters_reasoning="the 5-min duration is the canonical Bradford incubation time; no parameter values were user-stated for this step."`

Compare to a literal-transfer step: `"Add 2uL of each plasmid DNA to corresponding competent cell tubes (Plasmid A1 to cells B1, A2 to B2, A3 to B3, A4 to B4)."`
- Q1: `step_cited_text="Add 2uL of each plasmid DNA"`. `step_reasoning=None`. Grounding = `["instruction"]`.
- Q2: `parameters_cited_texts=["Add 2uL of each plasmid DNA to corresponding competent cell tubes", "Plasmid A1 to cells B1, A2 to B2, A3 to B3, A4 to B4"]`. `parameters_reasoning="the first phrase establishes volume + substance + source/destination labware; the second specifies the per-well A1→B1, A2→B2... mapping. Together they fully ground the transfer with all parameters."`

In both cases, the visualization layer can:
1. Draw a **thick composite arrow** from `step_cited_text` to the step block.
2. Draw **finer arrows** from each `parameters_cited_texts` phrase to relevant value rows in the step body.
3. Use `step_reasoning` and `parameters_reasoning` as hover-tooltip explanations.

Without this split, the visualization can render at most one arrow per step ("this step came from somewhere over there"), losing the architectural distinction between *why does the step exist* and *why do these values fit together*.

## Consequences

**Easier:**

- Both provenance types follow the same shape (cite + reasoning + confidence + source/grounding). Mental model is uniform; the LLM prompt is more learnable.
- Visualization (HTML report Phase 3) can ground arrows in structured fields. No substring-search-on-prose, no fuzzy match.
- Verification of citations is uniform: substring-check `cited_text` / `cited_texts` against the instruction text. Same code path for atomic and composite.
- Hallucinations surface at parse time: a step where the LLM can't produce a `step_cited_text` substring that actually appears in the instruction is a hallucination, caught immediately (paired with the existing `"instruction" in grounding` invariant from `tighten-composition-grounding`).
- ADR-0003's plan is partially subsumed (`cited_text` on atomic) and partially extended (now also on composite, in a deeper way).

**Harder:**

- Schema is wider: 4 new fields across the two types (`cited_text`, `reasoning` on atomic; `step_cited_text`, `step_reasoning`, `parameters_cited_texts`, `parameters_reasoning` on composite). Two new conditional invariants. Old fields (`reason`, `justification`) are removed — breaking change.
- LLM prompt complexity increases — the LLM must emit verbatim quotes per value AND per step, with the two-question discipline for composites. Empirically high reliability for short quotes (~95-98% per ADR-0003), but some failures are expected during prompt iteration.
- Test fixtures and Hypothesis strategies that construct provenances by hand all need updating. ~6 files affected.
- Cached `pipeline_state.json` files in `examples/` will no longer Pydantic-validate against the new schema if anything tries to deserialize them. They're documentation artifacts (not loaded by tests), so this is cosmetic — but worth flagging.

**Bounded by:**

- The LLM's reliability at extractive copying. ADR-0003 estimated ~95-98% for short atomic quotes; composite multi-phrase citations are likely similar. Failures are catchable as schema validation errors at extraction time, not silent wrong outputs.
- The runner doesn't yet exercise the verifier end-to-end — that comes when the visualization layer (HTML report) lands and arrow-rendering relies on the cited_text being substring-present.

## Migration plan

This is a breaking schema change. Single-PR migration:

1. Update `models/spec.py` with new schema and conditional validators. Remove `reason`/`justification` cleanly (no deprecation period — no external consumers exist).
2. Update `extraction/prompts.py` to instruct the LLM to emit the new fields, with calibration examples for the two-question split.
3. Update test helpers (`_prov`, `_comp`) and Hypothesis strategies to construct the new shape.
4. Update integration-test canned LLM responses (`tests/integration/test_pipeline_with_mocks.py`) to use the new format.
5. Run all tests + verify on at least one real-LLM eval that the new prompt produces correctly-shaped outputs.
6. (Acknowledged) Cached `examples/*/pipeline_state.json` files become stale documentation; not regenerated in this PR. Future runs will produce the new format.

## Related

- [ADR-0002](0002-provenanced-protocol-spec.md) — original provenance design.
- [ADR-0003](0003-cited-text-provenance.md) — proposed cited_text on atomic only; this ADR supersedes its scope by extending to composite and refining the structure.
- [ADR-0004](0004-remove-intent-verifier.md) — removed Stage 8; the rationale ("don't ship signal you can't trust" + "the LLM may interpret but not inject") is the same principle that motivates the parse-time hallucination guard here.
- `tighten-composition-grounding` PR — the tightening that made composite citation viable (every composite now provably has an instruction grounding to cite).
