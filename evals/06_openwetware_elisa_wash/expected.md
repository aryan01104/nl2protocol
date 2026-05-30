# Expected behavior — 06_openwetware_elisa_wash

## Case summary
**Source:** Mabtech ELISA guide, wash step only.
**Instruction shape:** instructive, very short (~400 chars) — one wash recipe + one manual finishing instruction.
**Config:** OT-2 with P300 8-channel multi (efficient for 96-well operations), bulk PBS-T reservoir, ELISA plate, waste reservoir.

## What this case tests
- **Repeat-N-times pattern** — 5 identical wash cycles; tool should emit a loop, not 5 inlined copies
- **96-well uniform operation** — perfect multi-channel use case; single-channel would be inefficient
- **Non-automatable finishing step** — "tap plate against absorbent paper" is manual; tool should surface as pause or skip with note
- **Waste destination** — aspirated wash buffer needs to go somewhere; config has `waste_reservoir`

## Most likely overall outcome
Tool should generate code for 5 iterations of (aspirate 300 µL from plate → dispense to waste → aspirate 300 µL PBS-T from reservoir → dispense to plate) — or close equivalent — and surface the tapping step as manual.

## Gaps the tool should surface

| # | Gap | Source step | Why no default works |
|---|---|---|---|
| 1 | Subset of wells to wash | Instruction says "the plate" — all 96 implied | Real ELISAs sometimes wash only used columns; tool should confirm |
| 2 | Per-wash incubation time | Not specified in source | Some protocols soak for 30 sec; some skip; tool should pick or ask |
| 3 | What was in the wells before the wash | Implied: antibody/sample residue | Tool needs to know there's volume to aspirate before dispensing fresh wash |
| 4 | Tip strategy across 5 washes | Not specified | Same tip across 5 washes? Fresh per wash? Per column? |
| 5 | "Tap against absorbent paper" — automate, pause, or skip? | Closing instruction | Cannot be done by OT-2 |
| 6 | Whether to start with aspirate or dispense (order matters per well state) | Not stated | If wells start full, aspirate first; if empty, dispense first |

## Failure modes to watch in generated code

| Risk | What we'd see |
|---|---|
| Inlined 5 copies instead of a loop | Code has 5 repeated blocks of identical operations |
| Single-channel mode | 96 × 5 = 480 transfers (slow); should use multi-channel = 12 × 5 = 60 |
| Waste handling | Wash aspirate goes to fixed trash (splash risk) instead of `waste_reservoir` |
| "Tap" step | Tool emits an unsupported `tap()` op or tries to "move plate" |
| Tip reuse silently | No `pick_up_tip` between washes → cross-well contamination |
| Subset hallucination | Tool decides to wash only columns 1–3 without surfacing why |
| Volume mismatch | Dispense 200 µL or 100 µL despite instruction specifying 300 µL |

## What success looks like
- Pipeline runs end-to-end.
- Either confirms "all 96 wells" or asks.
- Generates a loop or repetition pattern for the 5 washes (not 5 inlined copies — but inlined copies are acceptable if minor).
- Uses P300 multi-channel — column-by-column traversal.
- Aspirate-before-dispense per well (to remove old liquid first).
- Aspirated wash goes to `waste_reservoir`, not fixed trash.
- "Tap against absorbent paper" is surfaced as a manual step or pause command, NOT auto-translated.

## What concerning failure looks like
- Generates a single wash instead of 5.
- Uses single-channel mode (480 ops).
- Dispenses 300 µL of PBS-T without aspirating the existing well contents first (overflow risk — wells are 360 µL max).
- Sends aspirated wash to fixed trash.
- Tries to literally "tap" the plate via robot motion (nonsense).
- Hallucinates wells beyond A1–H12.

## Open questions
- Does the pipeline's repeat-N semantics emit a Python for-loop or inlined operations? Either is acceptable but they look different.
- How short can an instruction be (~400 chars here) before the pipeline starts asking *more* questions because the underspecification is dense?
- This is the matched compressed-baseline-pair: case 16 will be a further-compressed version of this same wash, ideal for the token-vs-length comparison.
