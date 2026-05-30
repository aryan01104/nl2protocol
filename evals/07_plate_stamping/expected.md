# Expected behavior — 07_plate_stamping

## Case summary
**Source:** Generic plate stamping convention, defined via Avidien blog quotes.
**Instruction shape:** instructive, very short (~280 chars) — one sentence + one constraint sentence.
**Config:** OT-2 with P300 multi-channel, source 96-well plate, destination 96-well plate, no modules, no waste.

## What this case tests
- **1:1 well mapping** across plates — tests the simplest non-trivial OT-2 pattern
- **Multi-channel native operation** — the canonical case for column-by-column traversal
- **Cross-contamination constraint** — instruction explicitly says "do not cross-contaminate" — tool should change tips between columns (or per well)
- **Short instruction** — comparable to case 06 ELISA wash (~400 chars), shorter than cases 01–03

## Most likely overall outcome
Tool should generate code that traverses 12 columns with multi-channel, changing tips between columns, transferring 10 µL each. Should ask about how many wells if not all 96 (but instruction says "each well of the source plate" so all 96 implied).

## Gaps the tool should surface

| # | Gap | Source step | Why no default works |
|---|---|---|---|
| 1 | Which subset of wells | Instruction: "each well" implies all 96 | Should confirm if user actually wants subset |
| 2 | Tip strategy detail | "do not cross-contaminate" — tool must operationalize | Per-column? Per well? |
| 3 | Source well contents — is source pre-filled? | Implied | Tool needs to know there's volume to aspirate |
| 4 | Order of traversal | Not specified | Column-by-column standard; tool should pick or surface |

## Failure modes to watch in generated code

| Risk | What we'd see |
|---|---|
| Single-channel mode | 96 transfers instead of 12 multi-channel column traversals |
| Same tip across all 96 wells | Direct violation of "no cross-contamination" constraint |
| Order mistake | Goes row-by-row (rotates plate 90°) — would work but unusual |
| Hallucinates source content | Tool emits warning about "empty source" even though instruction implies pre-filled |
| Wrong destination mapping | A1 → B1 or A1 → A12 instead of A1 → A1 |
| Tip drop strategy | Drops tip into source plate or destination plate instead of trash |

## What success looks like
- Pipeline runs end-to-end.
- Generated code traverses 12 columns (with multi-channel) or 96 wells (with single-channel; less efficient but valid).
- Fresh tip per column (or per well) — cross-contamination prevention is explicit.
- 1:1 mapping by well position (A1 → A1, B1 → B1, ..., H12 → H12).
- 10 µL transfer volume.

## What concerning failure looks like
- Single tip across all wells (violates explicit constraint).
- Wrong mapping (offset, transpose, or scrambled).
- Tool decides to do a subset without surfacing why.
- Pipettes 10 µL with P20 mounted (wrong pipette).
- Asks excessive questions for a well-defined trivial operation — would signal the pipeline is over-cautious for simple cases.

## Open questions
- For very short instructions (~280 chars here), how does the pipeline weigh "ask for confirmation" vs "just do the obvious thing"? Over-asking on trivial cases is its own failure mode.
- Does the tool understand "do not cross-contaminate" as a tip-change directive, or treat it as user-facing meta-text?
