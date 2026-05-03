# ADR-0004: Remove the Stage 8 LLM intent verifier

**Status:** Accepted
**Date:** 2026-05-03

## Context

The pipeline had eight stages. Stages 1–7 covered input validation, semantic extraction (LLM), labware resolution (LLM), hardware constraint checking (deterministic), spec→schema conversion (deterministic), Python script generation (deterministic), and Opentrons simulation (deterministic).

**Stage 8** was an additional LLM call: take the original natural-language instruction plus the generated Python script, and ask the model "does this script do what the user asked?" It returned a structured verdict — `matches: bool`, `confidence: float`, `issues: list[str]`, `summary: str` — and blocked release of the protocol if `matches=False` or `confidence < 0.7`.

The architectural intent was *defense in depth*: a final semantic safety net catching bugs the deterministic stages couldn't see. The motivating examples were things like "you asked for mix 10 times, the script mixes once" — semantic mismatches that would compile fine, simulate fine, and then do the wrong thing on real hardware.

## What we observed

In production use, the Stage 8 verifier produced **roughly a 95% false-positive rate**. It flagged scripts as mismatched or low-confidence when they were in fact correct. Specific failure modes:

- Hallucinated issues that didn't exist in the script.
- Low-confidence verdicts on simple, obviously-correct protocols.
- Inconsistent verdicts on the same input across reruns.

The deeper problem isn't tunability — it's the *kind* of question. "Does this script match the user's intent?" is exactly the broad, fuzzy semantic-matching question that LLM-as-judge handles badly when the stakes are concrete. There is no narrow rubric, no labeled training signal, and no obvious way to validate the judge itself without a separate eval set that doesn't exist.

The behavioral consequence was worse than the noise. Users learned (correctly) that the verifier's output wasn't worth reading. Once they were trained to dismiss it, the rare true positive — the very class of bug it was built for — would also be dismissed. In safety terms, **the verifier was net-negative signal-to-noise**.

## Decision

Remove Stage 8 entirely. Delete `nl2protocol/validation/validator.py`. Drop the invocation from `pipeline.py:run_pipeline`. Update README and `docs/TESTING.md` to reflect the new 7-stage architecture.

The pipeline's load-bearing safety guarantees now come from:

- **Stage 4 (constraint checker, deterministic).** Catches volumes outside pipette ranges, invalid wells, missing labware/modules, well-capacity overflow, tip insufficiency. Zero false-positive rate by construction.
- **Stage 7 (Opentrons simulator, deterministic).** Catches API-level errors the constraint checker can't see — labware-definition mismatches, deck-coordinate conflicts, deprecated API patterns, runtime tip exhaustion.
- **Stage 3 (user confirmation queue, interactive).** Cases where the LLM had to make a judgment call (which labware, which substance, which volume to assume) are surfaced to the user explicitly rather than judged by another LLM.

Together, those three layers cover the bug classes Stage 8 was nominally responsible for — but with deterministic checks at the right boundaries instead of fuzzy LLM judgment after the fact.

## Consequences

**Easier:**

- One fewer LLM call per protocol (faster, cheaper).
- One fewer subsystem to maintain, document, prompt-engineer, and explain.
- The "neuro-symbolic" story tightens: the LLM is allowed to *interpret* natural language and *route uncertainty to the user*, but it never *judges* the final output.
- Users see fewer warnings; the warnings they do see (from Stage 4) are deterministic and actionable.

**Harder:**

- Genuine semantic-mismatch bugs that pass Stage 4 + Stage 7 are now invisible to the pipeline. The argument for accepting this: in observed use, the verifier was wrong 95% of the time, so its remaining 5% catches were dwarfed by the false-positive cost. Real-world telemetry from `examples/` runs supports the conclusion that the deterministic stages cover the bug classes that actually appear.
- We lose the ability to publish a "verified by LLM" claim about each generated protocol. This was always honest framing-of-low-value; removing it forces clearer language about what *is* verified.

**Now committed to:** any safety net we add to the pipeline must (a) have a measurable false-positive rate before shipping, and (b) be removable when the FP rate degrades user trust. *Don't ship signal you can't trust* is the principle. If a future safety check needs LLM-as-judge, it must come with its own eval set as a precondition for landing.

## Related

- [ADR-0001](0001-tip-exhaustion-is-an-error.md) — Stage 4's design philosophy (fail-loud rather than warn-and-allow).
- [ADR-0002](0002-provenanced-protocol-spec.md) — provenance tracking is the mechanism by which Stage 3 routes uncertainty to the user instead of to an LLM judge.
