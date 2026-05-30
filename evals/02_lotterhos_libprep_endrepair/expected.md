# Expected behavior — 02_lotterhos_libprep_endrepair

## Case summary
**Source:** Lotterhos Lab Illumina Library Prep, sub-step 1 (End Repair & A-tailing) only — first stage of KAPA Hyper Prep Kit at half volumes.
**Instruction shape:** instructive, formal numbered (1.1 – 1.4), three sub-steps + thermocycler profile.
**Config:** OT-2 with P20 + P300, thermocycler module on slot 7 (with reaction plate inside), reagent rack on slot 3 for DNA + buffer + enzyme.

## What this case tests that case 01 didn't
- **Reaction assembly** — combining multiple reagents into one well at varied volumes (25 / 3.5 / 1.5 µL)
- **Thermocycler module** — translating an NL temperature/time profile into module commands
- **Mastermix optional** — instruction explicitly allows mastermix OR individual addition; tool's behavior on this choice point is interesting
- **Manual-only steps** — 1.2 vortex/spin/return-to-ice has no automated equivalent on OT-2

## Most likely overall outcome
Tool should **ask several clarifying questions** and **flag step 1.2 as not automatable** before generating code.

## Gaps the tool should surface

| # | Gap | Source step | Why no default works |
|---|---|---|---|
| 1 | Sample count + DNA location | "fragmented DNA (25 µL)" singular, config has 96-well plate | Default to A1 single-well is a guess |
| 2 | DNA source — reagent_rack tube or already in reaction_plate? | Not specified | Realistic placements differ |
| 3 | Buffer location in reagent_rack | Not specified | 24-position rack, no hint |
| 4 | Enzyme location in reagent_rack | Not specified | Same as #3 |
| 5 | Mastermix or individual addition? | 1.1: "Master mixes acceptable with 10% overage" | Tool must choose; sample count drives it |
| 6 | What to do with step 1.2 (vortex / spin / return to ice) | 1.2 verbatim | No OT-2 op for vortex or centrifuge; tool should either skip with note or emit a `pause` for manual intervention |
| 7 | Thermocycler lid temperature | 1.3 specifies block temps only | Standard ~105°C lid is default; tool may need to choose or ask |
| 8 | "4°C hold" duration | 1.3 says "hold" | Tool must pick a duration or emit indefinite hold |

## Failure modes to watch in generated code

| Risk | What we'd see |
|---|---|
| Skips thermocycler entirely | Generated code has no `thermocycler.execute_profile` calls |
| Emits unsupported `vortex` op | Pipeline crashes mid-execution, or generates ill-formed Python |
| Treats 1.2 as automatable | Pipettes do "spin" or similar nonsense |
| Mastermix decision opaque | Code combines reagents but no comment / log on whether overage was applied |
| Step 1.4 leaks into generated code | "Proceed immediately to adapter ligation" gets translated as a next-step action |
| Reaction-plate-in-thermocycler relationship | Pipeline must understand `reaction_plate` (slot 7) sits inside `thermocycler` (slot 7) |
| P20 vs P300 picking | 25 µL needs P300; 3.5 µL needs P20; 1.5 µL needs P20. Tool should switch pipettes mid-assembly. |

## What success looks like
- Pipeline runs end-to-end without exceptions.
- Either ≥3 of gaps #1–#5 surfaced as questions, OR code generated with explicit assumption-surfacing.
- Step 1.2 is surfaced (flagged manual / pause / skipped with note) — NOT silently auto-translated.
- Generated thermocycler code (if any) has the right block sequence: 20°C 30min → 65°C 30min → 4°C hold.
- P20 used for 3.5 µL and 1.5 µL, P300 used for 25 µL.

## What concerning failure looks like
- Crashes on the thermocycler profile parsing.
- Silently drops the thermocycler step and just does the reaction assembly.
- Pipettes 1.5 µL with P300 (out of accurate range).
- Treats step 1.4 ("proceed immediately to adapter ligation") as an action and tries to find adapter reagents.
- Hallucinates that this is a multi-sample protocol when "fragmented DNA (25 µL)" is singular.

## Open question (don't know yet)
Whether the pipeline understands that `reaction_plate` (slot 7) sits inside the `thermocycler` (slot 7). Same shared-slot convention as case 01's plate-on-magnetic-module — if it doesn't work for magnetic, it probably doesn't work here either, and we'll see the same gap.
