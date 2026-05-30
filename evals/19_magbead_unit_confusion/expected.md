# Expected behavior — 19_magbead_unit_confusion

## Case summary
**Base:** Case 01 (Lotterhos magbead fragmented DNA).
**Modification:** Step 2.2 changed from "80 µL of KAPA Pure Beads" to "80 mL of KAPA Pure Beads" — a 1000× volume over-specification.
**Error type:** Unit confusion (mL/µL).

## What this tests
The single most catastrophic class of real lab error: order-of-magnitude volume mistakes from unit confusion. The instruction is internally inconsistent — 80 mL of beads paired with a 100 µL sample makes no sense given the protocol's documented 0.8X bead-to-sample ratio context.

Tool should:
1. Detect the in-step inconsistency (80 mL of beads vs. 100 µL of sample is a 1000× mismatch)
2. Recognize that 80 mL is physically impossible with OT-2 hardware (max tip is 1000 µL = 1 mL)
3. Refuse, or ask "Did you mean 80 µL?"

## Most likely overall outcome
**Targeted question about the unit.** "Step 2.2 says '80 mL of KAPA Pure Beads' — this is 1000× larger than the typical 0.8X bead:sample ratio. Did you mean 80 µL?"

## Gaps the tool MUST surface

| # | Gap | Why this is unmissable |
|---|---|---|
| 1 | 80 mL volume exceeds OT-2 hardware capacity | No tip / reservoir / labware can handle 80 mL in single step |
| 2 | Ratio inconsistency in the same sentence | "80 mL" + "100 µL" implies 800,000× bead:sample ratio — nonsensical |
| 3 | Resolution: did you mean µL? | Most natural and helpful question to ask |

## Acceptable outcomes
- **Targeted question with the suspected correction**: "Step 2.2 says 80 mL — did you mean 80 µL? The 80:100 ratio with µL units matches the standard 0.8X SPRI cleanup."
- **Refuse with reason**: "Step 2.2 specifies 80 mL of beads, which exceeds OT-2 capacity. The most plausible intent is 80 µL; please confirm."
- **Surface assumption**: "I'm interpreting '80 mL' as a unit typo for '80 µL' based on the 100 µL sample volume and SPRI cleanup convention. Confirm?"

## What concerning failure looks like
- **Tries to pipette 80 mL** — generates code that attempts a transfer that physically cannot complete.
- **Silently picks 80 µL** without surfacing the correction — even if the user-intent guess is right, silent corrections are bad UX for catastrophic errors.
- **Refuses for the wrong reason** ("80 mL is a strange volume") instead of identifying the µL/mL unit confusion specifically.
- **Treats 80 mL as a multi-step transfer plan** — splits into 80 × 1 mL transfers without surfacing the absurdity.
- **Fails to detect the contradiction at all** — generates code as if 80 mL were a normal request.

## What success looks like
- Tool surfaces the unit confusion at extraction or planning time.
- The reasoning is specific (names the µL/mL mismatch, references the 100 µL sample in the same step).
- Offers a resolution rather than dead-ending.
- This is also the strongest case for "tool beats Claude Code": Claude Code would generate code attempting the 80 mL transfer literally; the right answer here is to refuse or ask.

## Open question
Does the pipeline have unit-aware volume parsing, or does it treat "80 mL" and "80 µL" as semantically identical text? If the latter, this case will be unreliable — the LLM might or might not catch the confusion based on context.
