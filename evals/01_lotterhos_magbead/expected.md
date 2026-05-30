# Expected behavior — 01_lotterhos_magbead

## Case summary
**Source:** Lotterhos Lab Magnetic Bead Wash, Section 2 (Cleanup of Fragmented DNA).
**Instruction shape:** instructive, formal numbered (2.1 – 2.18), volumes explicit, single-sample implied.
**Config:** OT-2 with P300, magnetic module on slot 4, deep-well sample plate on the module, separate elution plate, tube rack for beads/elution buffer, reservoir for ethanol.

## Most likely overall outcome
**Tool should surface several clarifying questions before generating code.** The instruction is underspecified in well-positions and a couple of volumes — there's no defensible default.

A secondary acceptable outcome: tool generates code with explicit assumptions surfaced (e.g. "I assumed sample at A1, elution at 30 µL — confirm?").

## Gaps the tool should surface

| # | Gap | Source step | Why no default works |
|---|---|---|---|
| 1 | Sample location in `sample_plate` | "the 100 µL fragmented DNA sample" (singular, no well) | 96-well plate, default to A1 is a guess |
| 2 | Elution volume | 2.15: "appropriate volume of elution buffer" | Common range 20–50 µL; depends on downstream use |
| 3 | Beads location in `reagent_rack` | Not in instruction | Reagent rack has 24 positions |
| 4 | Elution buffer location in `reagent_rack` | Not in instruction | Same as #3 |
| 5 | Ethanol location in `ethanol_reservoir` | Not in instruction | 12-channel reservoir, no column hinted |
| 6 | Binding incubation duration | 2.4: "5 – 15 min" range | Pick low/mid/high or ask |
| 7 | Magnet-capture duration | 2.5, 2.17: "until the liquid is clear" | No timer in instruction; tool must pick a fixed delay |
| 8 | Elution incubation duration | 2.16: "2 min" baseline, optional up-to-10 | Has a default; tool can use 2 |

## Failure modes to watch in generated code (if produced)

| Risk | What we'd see |
|---|---|
| No Z-offset → bead disturbance | `aspirate(180, sample_well)` at default well bottom; "carefully" qualifier ignored |
| Vortex unavailable on OT-2 | Hopefully translated to pipette-mix; concerning if tool emits an unsupported op |
| "Remove from magnet" misread | Should emit `mag_mod.disengage()`; bad if it skips this step |
| "Until clear" → arbitrary delay | Should emit a fixed `delay()` with a chosen number |
| Sample-on-module relationship | Pipeline must understand `sample_plate` (slot 4) sits on `mag_mod` (slot 4) |

## What success looks like
- Pipeline runs end-to-end without exceptions.
- Either ≥3 of gaps #1–#5 surfaced as questions, OR code generated with explicit assumption-surfacing.
- Magnetic module is correctly engaged before supernatant aspirations and disengaged before elution resuspension.
- Generated code (if any) has the right structural arc: bind → engage → discard supernatant → wash × 2 → dry → disengage → elute → engage → transfer eluate.

## What concerning failure looks like
- Crash mid-pipeline.
- Silently generates code without using the magnetic module at all.
- Aspirates supernatant before engaging the magnet.
- Hallucinates a well position invalid for the configured plate.
- Picks an ethanol-wash volume well below 200 µL.
- Treats step 2.3 "vortex" as an unsupported op error instead of substituting pipette-mix.

## Open question (don't know yet)
Whether the pipeline's schema treats `sample_plate` (slot 4) and `mag_mod` (slot 4) as co-located (plate-on-module) or as a conflict. Validator only checks labware-vs-labware slot conflicts, so this should be accepted — but interpretation downstream is unknown.
