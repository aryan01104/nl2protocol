# ADR-0016: Volumes may be derived from tracked well contents

**Status:** Accepted
**Date:** 2026-06-18

## Context

Some volumes are not stated as numbers — the instruction defines them by the well:
"resuspend the beads in **an appropriate volume**", "transfer **the clear supernatant**",
"remove **the ethanol**". Extraction correctly leaves these null (no number to copy). Gap
resolution then fills them, but each missing-volume gap is filled **independently** by
`LLMSpotSuggester` (`gap_resolution/suggesters.py`), which sees only "the gap field and neighboring
steps — NOT the whole spec" and writes only that one field.

For physically-coupled volumes this produces contradictions. In the case-01 magbead run, the
pipeline poured **30 µL** of elution buffer into `sample_plate A1`, then independently guessed a
**50 µL** resuspend mix and a **32 µL** eluate transfer — you cannot stir or remove more liquid than
you added. The three numbers should all reference the same well, but nothing tied them together.

The deterministic `WellStateTracker` (`validation/constraints.py`) already keeps a running per-well
volume through `spec_to_schema`, and at the point each step's commands are built it already reflects
all prior steps. The arithmetic the inconsistency needs was already available — the gap layer just
wasn't using it.

## Decision

A volume may carry a **basis** that defers its number to build time:

- New `VolumeBasis(kind="well_contents", location: "source"|"destination", fraction: float)` and an
  optional `ProvenancedVolume.basis` (`models/spec.py`). `value` stays required and numeric — a
  fallback — so the ~40 sites that read `.value` and the completeness validator are unaffected.
- `spec_to_schema` resolves a basis to `round(fraction × current_contents_of_well, 1)` from the live
  `WellStateTracker` and uses it for the emitted commands, writing the resolved number back onto the
  volume via `push_revision`. This is the "render the expression to a value" step.
- A deterministic `WellContentsVolumeSuggester`, registered before `LLMSpotSuggester`, attaches a
  basis to missing-volume gaps that are well-defined: a resuspend `mix` (fraction 0.8 on the well it
  stirs) or a removal `transfer` whose substance is supernatant/eluate/ethanol/wash (fraction 1.0 on
  the source). It carries confidence 0.95 — a well reference is deterministic, not a guess, so it
  auto-accepts and is never sent to the per-step LLM.

Coupled volumes are now consistent by construction: the mix and the removal both read the well that
the add filled, so neither can exceed it. The single genuinely-free quantity (the elution **add**)
keeps the existing flow — cited-and-not-fabricated stands; inferred is flagged for confirmation.

## Alternatives rejected

- **LLM maintains a running volume table in-context.** Pushes deterministic bookkeeping into the
  probabilistic model — the thing it is worst at — duplicates `WellStateTracker`, and drifts across
  many steps. Against the "single LLM call + deterministic tail" architecture.
- **Make `value` optional / a discriminated union of literal-or-reference.** Invasive across ~40 read
  sites and the completeness validator for no behavioral gain over the fallback-value approach.
- **Compute the number during gap resolution (stage 3).** Gap-fill ordering is not guaranteed — the
  elution-add gap can resolve after the mix gap, re-introducing the inconsistency. Build-time
  resolution walks steps in order, so the well genuinely holds the right amount when each derived
  volume is computed.
- **Emit the basis from extraction (prompt).** More faithful to the phrase, but changes the
  extraction prompt and the "value required" contract. Chose the deterministic gap-suggester so the
  reader stays unchanged and the fix lives in the existing null → fill flow.

## Consequences

- One new optional model field (`ProvenancedVolume.basis`) and one value type (`VolumeBasis`);
  backward-compatible with saved specs (`basis` defaults `None`).
- Build-time resolution is the single source of truth for derived volumes; the provenance records
  "= fraction × contents of well (N µL)" for legibility.
- Scope is intentionally narrow: only `well_contents × fraction`, covering the observed
  removal/resuspend coupling. No general expression algebra (step-to-step references, arithmetic);
  extensible later via `VolumeBasis.kind`.
- Composes with ADR's trash routing: "discard the supernatant" lowers to aspirate-the-derived-volume
  into the tip then drop to trash.
