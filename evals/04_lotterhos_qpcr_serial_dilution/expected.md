# Expected behavior — 04_lotterhos_qpcr_serial_dilution

## Case summary
**Source:** Lotterhos Lab qPCR Oyster Disease Detection page, standards prep portion (steps 1 + 2).
**Instruction shape:** instructive, semi-structured — short. Step 1 is a one-time combine (3 reagents → 1 well). Step 2 is iterative dilution defined by a concentration range, not a fixed point count.
**Config:** OT-2 with P20 + P300, reagent rack on slot 3, 96-well standards plate on slot 4, no modules.

## What this case tests that earlier cases didn't
- **Iteration count inferred from range** — instruction says "0.1 ng/µl to 0.0001 pg/µl" but doesn't say how many dilution points; tool must compute or ask
- **Cross-contamination prevention** — serial dilution is the canonical case for fresh-tip-per-transfer; sloppy tool would reuse tips
- **Mixing between transfers** — instruction doesn't explicitly say "mix" between dilution steps; real lab assumes mix-by-pipetting; tool may forget
- **Reagent disambiguation** — two distinct gBlock standards (Dermo + MSX) plus TE buffer — three reagents in one rack
- **Short / sparse instruction** — instruction is much shorter than cases 01–03 (~600 chars vs 2000+); good data point for the token-vs-length observation in todos

## Most likely overall outcome
Tool should **ask about the dilution series layout** (where in the plate, how many points), **reagent locations** in the rack, and either compute the dilution count from the range or ask. Otherwise should generate a fresh-tip-per-transfer serial dilution with mixing.

## Gaps the tool should surface

| # | Gap | Source step | Why no default works |
|---|---|---|---|
| 1 | Number of dilution points | Step 2: "range from 0.1 ng/µl to 0.0001 pg/µl" | Math gives 7 points at 10-fold, but tool should confirm |
| 2 | Layout in standards_plate | Not specified | Common conventions: row A across columns, or column 1 down rows; tool must pick or ask |
| 3 | Direction of dilution (left→right vs top→bottom) | Not specified | Multichannel convenience drives this in reality |
| 4 | Where the 0.1 ng/µL "top point" goes | Step 1 makes 100 µL of combined standard — into what destination? | Could go into a reagent_rack tube, or directly into the first well of standards_plate |
| 5 | Reagent positions | Dermo gBlock, MSX gBlock, TE buffer — which tubes? | 24-position rack |
| 6 | Mixing strategy | Not in instruction | Real serial dilution requires pipetting up-and-down between steps |
| 7 | Whether step 1's 100 µL is the source for step 2 | Implied but not stated | Affects whether tool uses the same vessel or transfers |

## Failure modes to watch in generated code

| Risk | What we'd see |
|---|---|
| Same tip across all dilutions | Single `tip_pickup` at the start, no `drop_tip` until end → cross-contamination |
| No mixing between transfers | `aspirate(10, source); dispense(10, dest)` with no mix or only mix at first dilution |
| Hallucinates a specific count | Hardcoded "7 dilutions" without surfacing the math |
| Picks 80 µL TE buffer for serial dilution | Reuses step 1's 80 µL value instead of step 2's 90 µL |
| Pipette choice | 10 µL with P300 (less accurate); 80/90 µL with P20 (impossible, P20 max is 20 µL) |
| Reagent confusion | Tool treats Dermo and MSX as interchangeable instead of separate gBlock stocks |
| Destination location | All 7 dilution points end up in same well (overwrite), or 6 dilutions but missing the top point |

## What success looks like
- Pipeline runs end-to-end without exceptions.
- Either asks about gaps #1–#5 or surfaces explicit assumptions (e.g., "I assumed 7 dilution points at 10-fold, starting in A1 of standards_plate, ending in A7 — confirm?").
- Each dilution uses a fresh tip (or surfaces a contamination-prevention strategy).
- Mixing is included after each dilution transfer (or explicitly surfaced as a decision).
- P20 used for 10 µL transfers, P300 used for 80 µL (step 1) and 90 µL (step 2) transfers.
- Step 1 produces a clearly identified top-point standard before step 2 dilutes from it.

## What concerning failure looks like
- Reuses tips across all 7 dilutions silently.
- Doesn't include any mix calls.
- Hardcodes a number of dilutions without surfacing.
- Treats step 2 as a one-time dilution (only produces 0.01 ng/µL, not the full range).
- Generates code that pipettes 90 µL with P20 (impossible).
- Treats the two gBlock standards as the same reagent.

## Open questions
- How does the pipeline handle a concentration-range specification ("0.1 ng/µl to 0.0001 pg/µl") in instruction parsing? Does it understand the math, ask the user to expand, or silently emit an arbitrary count?
- For short instructions like this (~600 chars vs cases 01–03 at 2000+), does the pipeline ask MORE questions (because less is specified) or FEWER (because less is parsed)? This is the natural A/B for the token-vs-length todo.
