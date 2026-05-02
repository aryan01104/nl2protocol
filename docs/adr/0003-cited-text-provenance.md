# ADR-0003: Verifiable provenance via cited_text spans

**Status:** Waiting — proposed but not yet implemented. Implementation plan tracked in `docs/plans/cited-text-provenance-plan.md`. Blocked on building an eval set first (see "Open questions").
**Date:** 2026-05-01

## Context

ADR-0002 introduced the `Provenance` object so each value in a `ProtocolSpec` carries a claim about where it came from (`instruction`, `config`, `domain_default`, `inferred`). Verification of those claims is currently regex-based: the verifier `_verify_claimed_instruction_provenance` in `nl2protocol/extraction/extractor.py` re-parses the user's instruction with regex extractors (`_extract_volumes_from_text`, `_extract_wells_from_text`, `_extract_durations_from_text`, `_extract_non_volume_numbers`) to build a "ground truth" set, then checks whether each `source="instruction"` value appears in that set.

This works but has known soundness gaps:

(a) **Coverage is bounded by the regex grammar.** If the user writes "100 μL" (Greek mu), "one hundred microliters", or "100ul" (lowercase), and the regex only matches "100uL", an honest LLM gets flagged for a fabrication that didn't happen. The verifier's recall is the regex's recall.

(b) **Substring matches cause precision losses.** Substance and labware checks use `value.lower() in instruction.lower()` — so "ATP" appears inside "ATPase," "plate" appears inside "template," and a fabricated value can silently pass because of substring collisions.

(c) **Temperature uses an unstructured fallback.** `_extract_non_volume_numbers` returns every number that isn't a volume, including pH values, well counts, RPMs, repetition counts. A claim like "temperature 5°C" passes verification if the user said "mix 5 times," because 5 is in the non-volume bucket — the check confirms the number appears somewhere in the text, not that it appears as a temperature.

(d) **`Provenance.reason` is meant to be a citation but is free-form prose.** The schema instructs the LLM to "cite the phrase from the text," but the field is unstructured natural language and therefore not machine-checkable. The actual grounding work is done by the parallel regex re-parser, not by the provenance object itself.

(e) **The verification logic does not match what the schema claims to enforce.** The provenance object presents itself as a citation; the verifier ignores the prose and grounds claims via a separate, lower-fidelity mechanism. This mismatch makes the system harder to reason about and harder to extend (every new ProvenancedX type would need its own regex extractor).

## Decision

Promote citations from prose to structure. Add a `cited_text: Optional[str]` field to `Provenance`. When `source == "instruction"`, the LLM must emit the verbatim substring from the user's instruction that grounds the value. A model validator enforces presence; the verifier checks the substring is present in the instruction (case-insensitive, whitespace-normalized) and that the value is contained within the substring.

Concretely:
- `Provenance.cited_text` is required when `source == "instruction"`, optional otherwise.
- `_verify_claimed_instruction_provenance` is rewritten as a uniform substring check across all field types.
- The five regex extractors and the `explicit_volumes` field on `ProtocolSpec` are removed — `cited_text` replaces both their roles.
- `CompositionProvenance` is intentionally excluded from this change. A step's existence often grounds in a synthesis of multiple sources ("user said Bradford" + "Bradford requires incubation") and cannot be reduced to a single quote. Composition keeps its `justification` (prose) + `grounding` (list of sources) + `confidence`.

This change applies to all field-level provenanced objects: `ProvenancedVolume`, `ProvenancedDuration`, `ProvenancedTemperature`, `ProvenancedString`, and `LocationRef.provenance`. Because they all share the same `Provenance` class, the schema change covers them automatically.

## Consequences

**Easier:**
- Verification is uniform across every provenanced field type — no more per-category regex.
- Coverage is no longer bounded by what the regex grammar happens to match.
- Substring collisions are eliminated by positional grounding (the value must appear *inside the cited phrase*, not just somewhere in the text).
- Code shrinks: ~115 lines of verifier + 5 regex helpers collapse to ~30 lines and one helper.
- Adding a new ProvenancedX type in the future requires zero new verification code.

**Harder:**
- The LLM must emit accurate verbatim quotes. Reliability depends on the model's extractive copying ability, which is empirically high (~95–98%) for short quotes but not guaranteed.
- A failure mode shifts from "regex misses a real value" (false fabrication) to "LLM emits wrong/missing quote" (false fabrication). The new failure mode is more catchable (substring check is exact) but is a new mode the system must handle.
- Prompt complexity increases — the LLM is told to emit one extra field per provenanced value, with anti-pattern examples to discourage paraphrasing.

**Bounded by:**
- The LLM's reliability at extractive copying. Failures are catchable as schema validation errors at extraction time, not silent.
- Whitespace and unicode normalization in the substring check — innocuous variations ("100 uL" vs "100uL") must be handled by the verifier, not punished as fabrications.

**Now committed to:**
- Maintaining a substring-checking verifier as the canonical grounding mechanism for instruction-sourced claims.
- Treating any `source="instruction"` claim without `cited_text` as a schema error, surfaced loudly at extraction time.
- Building (or borrowing) an eval set that measures provenance verification precision/recall, since this change cannot be safely validated by smoke testing alone.

## Open questions

- Should `source="config"` also use `cited_text` (carrying a config key path)? Out of scope for this ADR but a natural extension if config crosschecking is reintroduced at the ProtocolSpec level.
- Is whitespace normalization in the substring check enough, or do we need fuzzy matching for unicode (uL vs μL, smart quotes, en-dash vs hyphen)?
- What is the LLM's actual quote-emission reliability on the project's instruction style? Cannot be answered without an eval set — building that set is the gating prerequisite for accepting this ADR.
- Does the change degrade other extraction quality (verbosity, paraphrasing, example-following) by adding `cited_text` instructions to the prompt? Same answer: needs an eval set to confirm.
