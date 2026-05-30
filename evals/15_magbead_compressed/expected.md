# Expected behavior — 15_magbead_compressed

## Case summary
**Base:** Case 01 (Lotterhos magbead fragmented DNA, full form).
**Modification:** Instruction compressed to ~150 chars (1 sentence). Config identical.
**Form axis:** Compressed.

## What this tests
Two things:
1. **Register tolerance** — does the pipeline produce the same downstream code from compressed instruction as it does from full instruction?
2. **Token-cost vs. length** — the natural A/B with case 01. Comparing both runs gives us data for the token-usage todo.

## Expected delta from case 01

| Stage | Case 01 (full) | Case 15 (compressed) |
|---|---|---|
| Generated code structure | Same | Same |
| Questions asked | 3+ gap questions | Probably more (less info specified) |
| Input tokens | High (~600 from instruction) | Low (~40 from instruction) |
| Output tokens | Roughly equal (same code being produced) | Roughly equal |
| Total LLM calls | Same | Same |

The hypothesis: the tool asks MORE questions when given compressed input because more is left unspecified — but the generated code (after Q&A loop) should converge to the same place.

## Information present in compressed instruction
- 0.8X SPRI ratio (implies bead:sample volume ratio — same as case 01's 80 µL / 100 µL)
- "fragmented DNA sample" (same as case 01)
- "two 80% ethanol washes" (same as case 01 steps 2.7–2.12)
- "on the magnet" (same as case 01's repeated magnet engages)
- "elute in 30 µL Tris-HCl" (case 01's "appropriate volume of elution buffer" → made specific here)

## Information NOT in compressed instruction (vs case 01)
- Binding incubation time (case 01: "5–15 min")
- Mixing instructions (case 01: "Mix thoroughly by vortexing")
- Drying time (case 01: "3–5 min")
- Step ordering details (case 01 numbered; case 15 implicit)
- Final transfer to new plate (case 01 step 2.18; case 15 just says "elute")

## Most likely overall outcome
Pipeline asks MORE clarifying questions than case 01 because less is specified — but the same gap categories apply (sample location, beads location, etc.). After Q&A, generated code should be near-identical to case 01.

## Acceptable outcomes
- **More questions + same final code** as case 01.
- **Same questions + same final code** as case 01 (would imply tool surfaces gaps even when not in instruction).
- **Fewer questions + sensible defaults** for the omitted bits (e.g. "assuming 10 min binding incubation" surfaced as assumption).

## What concerning failure looks like
- **Fundamentally different code** from case 01 — would indicate compressed input takes a different code path.
- **Crashes on parsing** — the pipeline can't extract anything from the terse instruction.
- **Hallucinates additional steps** not in either case 01 or 15.
- **Misses the magnet** — fails to surface that this needs the magnetic module.

## What success looks like
- Pipeline runs end-to-end.
- Generated code is structurally equivalent to case 01's expected output.
- Token-usage delta vs. case 01 is roughly linear in input length (not super-linear from extra retries).

## Open questions
- Does the compressed form trigger different planning paths inside the pipeline (e.g. fewer extraction passes)?
- Does the token budget actually scale linearly, or does the gap-resolution loop multiply costs?
