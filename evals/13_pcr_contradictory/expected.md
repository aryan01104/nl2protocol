# Expected behavior — 13_pcr_contradictory

## Case summary
**Base:** Case 03 (OpenWetWare Phusion PCR).
**Modification:** Config has only P20 + 20-µL filter tip rack. P300 + 200-µL tips removed.
**Error type:** Contradictory instruction (volume exceeds tip capacity).

## What this tests
The 31.5 µL water step in the Phusion recipe physically cannot fit into a 20-µL tip. The buffer step (10 µL) is also problematic if treated as a single transfer with margins.

Tool should:
- Detect the volume-vs-capacity contradiction
- Either refuse, or propose a multi-step transfer (e.g., 2 × ~16 µL), or ask for a larger-tip rack

## Most likely overall outcome
**Refusal or targeted question.** The contradiction is sharp: a 20-µL tip can't hold 31.5 µL of liquid by physics.

## Gaps the tool MUST surface

| # | Gap | Volume affected | Why this is unmissable |
|---|---|---|---|
| 1 | 31.5 µL water vs. 20-µL tip max | Largest single volume in recipe | Physical impossibility in single transfer |
| 2 | 10 µL buffer | Borderline; fits in 20-µL tip but near the edge | Less critical; tip can hold it |

## Acceptable outcomes
- **Refuse**: "31.5 µL water can't be transferred with the 20-µL tips configured. Add a 200-µL tip rack and a P300 pipette, or reduce the recipe scale."
- **Workaround proposal**: "I'll split the 31.5 µL water transfer into two aspirations (15 µL + 16.5 µL) using the P20. Confirm?"
- **Ask**: "I can't pipette 31.5 µL with 20-µL tips. Should I split into multiple transfers or do you have larger tips?"

## What concerning failure looks like
- **Silent multi-step transfer** that pipettes 2 × 15.75 µL without surfacing — degrades accuracy without telling the user.
- **Tries to pipette 31.5 µL in one shot** — either crashes or overflows the tip (physical risk).
- **Refuses for the wrong reason** ("recipe too complex") instead of identifying the tip-capacity mismatch specifically.
- **Doesn't detect the problem** — generates code that the OT-2 firmware would error on at runtime.

## What success looks like
- Tool detects the contradiction at planning time, NOT at code-execution time.
- Surfaces specifically: "31.5 µL exceeds 20 µL tip capacity."
- Either refuses or proposes a workaround (multi-aspiration or larger tip).
- Does NOT silently degrade behavior.

## Comparison vs cases 09, 10
- Case 09 (missing module): structural absence — no magnetic module at all.
- Case 10 (missing pipette): accuracy concern — P300 can attempt 0.5 µL but it's inaccurate.
- Case 13 (this case): physical impossibility — 20-µL tip cannot hold 31.5 µL no matter how careful.

The pipeline's behavior may differ across these three: case 9 might be caught by config validation, case 10 might need pipette-range knowledge, case 13 might need tip-capacity knowledge. Three different "tool knows what's physical" checks.
