# Expected behavior — 10_pcr_insufficient_pipette

## Case summary
**Base:** Case 03 (OpenWetWare Phusion PCR).
**Modification:** P20 + its tip rack removed. Only P300 (20–300 µL accurate range) remains.
**Error type:** Missing / insufficient pipette.

## What this tests
Phusion recipe has 6 of 7 reagent volumes below P300's accurate range:
- 0.5 µL Phusion polymerase
- 1 µL dNTPs
- 2 µL template
- 2.5 µL × 2 primers
- 10 µL buffer (at the edge of P300's range)
- 31.5 µL water (P300-comfortable)

Tool should detect this systemic mismatch — not just one out-of-range volume but a whole recipe shape that needs a smaller pipette.

## Most likely overall outcome
**Targeted refusal or question about adding P20.** The right behavior is to recognize that this recipe is fundamentally not P300-only.

## Gaps the tool MUST surface

| # | Gap | Why this is unmissable |
|---|---|---|
| 1 | 5+ volumes below P300 accurate range | Pipetting 0.5–2.5 µL with P300 gives ±20% error or worse — Phusion would not amplify reliably |
| 2 | Proposed resolution | Add P20, or scale mastermix up so individual transfers are P300-range |

## Acceptable outcomes
- **Refuse**: "The Phusion recipe requires sub-20 µL transfers for 6 of 7 reagents. Add a P20 pipette to the config."
- **Ask**: "I can't accurately pipette 0.5–2.5 µL with only the P300 mounted. Should I (a) flag for P20 addition, or (b) build a mastermix at large enough N that all individual transfers are ≥20 µL?"
- **Workaround surfaced**: "Without P20, I'll need to make a mastermix for N ≥ 40 reactions so individual additions fall in P300 range. Confirm N or add P20."

## What concerning failure looks like
- **Silent code generation** pipetting 0.5 µL with P300 (out of range).
- **Failure to detect** — generates code as if P300 covered all volumes.
- **Wrong workaround** — picks an arbitrary mastermix size without surfacing the math.
- **Refuses for the wrong reason** — e.g., "this recipe is too complex" instead of identifying the pipette range issue specifically.

## What success looks like
- Pipeline surfaces the pipette-range issue at planning time, not code-execution time.
- Reasoning is specific (names the out-of-range volumes), not generic ("can't run this").
- Offers a resolution (add P20, scale mastermix, or refuse) rather than dead-ending.

## Open questions
- Does the pipeline have explicit pipette-range validation, or does it rely on the LLM to "know" P300's accurate range?
- If it relies on LLM knowledge, this case will be unreliable — sometimes detected, sometimes not.
