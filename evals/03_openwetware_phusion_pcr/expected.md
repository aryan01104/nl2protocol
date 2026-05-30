# Expected behavior — 03_openwetware_phusion_pcr

## Case summary
**Source:** OpenWetWare Silver Lab PCR page, Phusion polymerase condition only.
**Instruction shape:** instructive, semi-structured — a 7-component reagent list with per-reaction volumes plus a thermocycling profile that includes ranges and formulas.
**Config:** OT-2 with P20 + P300, thermocycler module on slot 7 (with reaction plate inside), reagent rack on slot 3.

## What this case tests that earlier cases didn't
- **Multi-component mastermix** (7 reagents combined into one 50 µL well — more than case 02's 3)
- **Sub-P20 volume** — 0.5 µL Phusion polymerase is below P20's accurate range
- **Parameter formulas in the cycling profile** — "Tm minus 5 °C", "(# bp/1000) min", "15-30 sec/kb"
- **Mastermix-vs-individual decision** — case 02 mentioned mastermix as optional; here it's the only realistic path if N > 1

## Most likely overall outcome
Tool should **ask several clarifying questions** before generating code, including at minimum: sample count, anneal temperature, extension time, amplicon size, and polymerase low-volume strategy.

## Gaps the tool should surface

| # | Gap | Source step | Why no default works |
|---|---|---|---|
| 1 | Sample count + reaction-plate wells | Recipe is per-reaction; no count or layout | Drives mastermix sizing and well assignments |
| 2 | Template DNA location & whether it's already at the right concentration | "2 µL Genomic Template" | Real templates are user-provided |
| 3 | Per-reagent locations in reagent_rack | Not specified | 24 positions, 7 reagents |
| 4 | Mastermix vs. individual reagent addition | Not stated; instruction lists per-reaction volumes | Sample count drives the choice |
| 5 | Anneal temperature | "45-72 °C ... 3 degrees above primer Tm" | Tool can't know primer Tm |
| 6 | Extension time | "15-30 sec/kb" | Tool can't know amplicon length |
| 7 | Cycle count | "25-35 cycles" | Range, no default |
| 8 | Final extension time | "5-10 min" | Range |
| 9 | "4 °C indefinitely" duration | End hold | Pipeline must pick a finite duration or emit indefinite hold |

## Failure modes to watch in generated code

| Risk | What we'd see |
|---|---|
| Silently pipettes 0.5 µL with P20 | `p20.aspirate(0.5, polymerase)` — below accurate range |
| Doesn't make mastermix | Code does 7 individual transfers per sample (high tip use, error-prone) |
| Hardcoded anneal temp | `thermocycler.set_block_temperature(55)` without surfacing |
| Hardcoded extension time | `set_block_temperature(72, hold_time_seconds=30)` without asking |
| Doesn't translate the formulas | Pipeline emits literal strings like `"Tm-5"` and crashes |
| Pipette switching | 10 µL → P20 (wrong; should be P300); 0.5 µL → P300 (wrong; should be P20 or flagged) |
| Reagent rack auto-assignment | Tool puts reagents in arbitrary slots without confirming |
| Skips cycling entirely | Generates mastermix prep but no `thermocycler.execute_profile` |
| Generates cycling when user only wanted prep | Tool assumes OT-2 should do the cycling; user may have wanted prep only and an external thermocycler |

## What success looks like
- Pipeline runs end-to-end without exceptions.
- Either ≥4 of gaps #1–#9 surfaced as questions, OR code generated with explicit assumption-surfacing for each.
- 0.5 µL polymerase volume is flagged as out of accurate pipette range — tool either suggests mastermix-only path or surfaces the concern.
- Mastermix decision is surfaced (either asked, or chosen with a comment).
- P20 used for ≤ 20 µL volumes (1, 2, 2.5 µL); P300 used for ≥ 20 µL volumes (31.5 µL water and arguably the 10 µL buffer).
- Parameter ranges in cycling profile are either asked about or chosen with surfaced reasoning.

## What concerning failure looks like
- Crashes on parsing the formula strings ("Tm minus 5", "# bp/1000").
- Silently picks midpoints of all ranges without comment.
- Treats step 1 ("Use NEB Phusion Polymerase Kit") as actionable and tries to "use" something.
- Pipettes 0.5 µL with P300 (way out of range).
- Hallucinates that reagents have specific well positions in `reagent_rack` (e.g. assumes Phusion buffer is always at A1).
- Generates code that combines reagents into the reaction_plate before pre-cooling the block.

## Open questions (don't know yet)
- Whether the pipeline understands `reaction_plate` (slot 7) sits inside the `thermocycler` (slot 7). Same shared-slot concern as cases 01 and 02.
- Whether the pipeline can negotiate "0.5 µL" as below-precision and propose mastermix as the resolution, or whether it treats sub-P20 volumes as a hard error.
- Whether "Use NEB Phusion Polymerase Kit" gets interpreted as a labware-resolution step or correctly recognized as a sentence the user wrote to themselves about which kit they're using.
