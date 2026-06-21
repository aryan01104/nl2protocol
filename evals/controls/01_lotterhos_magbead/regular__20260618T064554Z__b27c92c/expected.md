# Expected behavior — 01_lotterhos_magbead

## Case summary
**Source:** Lotterhos Lab Magnetic Bead Wash, Section 2 (Cleanup of Fragmented DNA), augmented with a `Setup:` block following the contract demonstrated in `test_cases/examples/magnetic_bead_cleanup/instruction.txt`.

**Instruction shape:** Single-paragraph Setup block grounding the initial deck state, followed by a numbered procedural body that preserves the source page's wording — including the legitimately fuzzy ranges ("5–15 min", "≥30 sec", "appropriate volume", "until liquid is clear", "3–5 min").

**Config:** OT-2 with P300 single-channel, magnetic module on slot 4, deep-well `sample_plate` (nest_96_wellplate_2ml_deep) on the magnetic module, `elution_plate` (nest_96_wellplate_100ul_pcr_full_skirt) on slot 5, `reagent_rack` (24-tube eppendorf 1.5 mL) on slot 2 for beads + elution buffer, `ethanol_reservoir` (nest_12_reservoir_15ml) on slot 3.

## Setup-block reasoning
- **Vessel choice: plate (`sample_plate A1`).** The source page hedges with "plate/tube(s)" because the protocol is vessel-agnostic — a real bench scientist picks based on sample count. The eval config places `sample_plate` (deep-well 96) on the magnetic module, which is the natural deck for a single-sample run that may scale up; the eppendorf-tube alternative would need a different deck topology (no magnetic module — single tubes can't sit on the OT-2 magnetic module). Plate is the more demonstrable choice and matches the worked example in `test_cases/examples/magnetic_bead_cleanup/`.
- **Initial locations grounded:** sample (sample_plate A1, 100 µL), beads (reagent_rack A1), elution buffer (reagent_rack A2), ethanol (ethanol_reservoir A1), eluate destination (elution_plate A1). These are not gaps the tool should surface; they are user-stated facts.

## Most likely overall outcome
Pipeline generates a working Opentrons script that the simulator accepts, with **3–5 targeted clarifying questions on legitimately fuzzy values:** binding-incubation duration, magnet-capture duration, ethanol-wash duration, bead-dry duration, elution-buffer volume.

## Gaps the tool should surface (legitimate)

| # | Gap | Source step | Why it's legitimate |
|---|---|---|---|
| 1 | Elution-buffer volume | Step 14: "appropriate volume" | Common range 20–50 µL; depends on downstream use; instruction explicitly defers |
| 2 | Binding-incubation duration | Step 3: "5 – 15 min" | Range, not a value — tool must pick a low/mid/high or ask |
| 3 | Magnet-capture duration | Steps 4, 16: "until the liquid is clear" | No timer in instruction; tool must pick a fixed delay |
| 4 | Ethanol-wash incubation | Steps 7, 10: "≥30 sec" | Lower-bound only — tool picks an exact value |
| 5 | Bead-dry duration | Step 12: "3 – 5 min" | Range — pick or ask |

Gaps 4–5 are minor (tight ranges); gap 2 is medium; gaps 1 and 3 are the most consequential.

## Gaps the tool should NOT surface (grounded in Setup)

| Element | Where it's pinned |
|---|---|
| Sample location | Setup: `sample_plate A1` |
| Beads location | Setup: `reagent_rack A1` |
| Elution-buffer location | Setup: `reagent_rack A2` |
| Ethanol location | Setup: `ethanol_reservoir A1` |
| Eluate destination | Setup: `elution_plate A1` |
| Vessel choice (plate vs. tube) | Setup says plate |
| Initial sample volume | Setup: 100 µL |
| Sample-plate-on-module relationship | Setup says sample_plate sits on the magnetic module |

If the tool asks about any of these, that's a Setup-reading regression — the spec contains the info and gap-resolution should defer to it.

## Failure modes to watch in generated code

| Risk | What we'd see |
|---|---|
| No Z-offset on supernatant aspirate | Beads pulled into the tip and lost; "carefully" qualifier ignored |
| Vortex unavailable on OT-2 | Step 2 ("mix thoroughly … pipetting") should map to `mix()`; bad if emitted as `vortex()` or an unsupported op error |
| Skips magnet disengage | Step 13 must emit `mag_mod.disengage()` before elution resuspension |
| "Until clear" hardcoded to 0 sec | Should emit a deliberate `delay()` with a chosen number, not skip |
| Sample-plate-on-module misread | Pipeline must accept `sample_plate` (slot 4) co-located with `mag_mod` (slot 4) as a plate-on-module relationship, not a labware-vs-labware conflict |
| Wrong elution volume | Should land in the 20–50 µL range; below 15 µL or above 60 µL is suspicious |
| Wrong supernatant aspirate volume | Should be 180 µL (= 100 µL sample + 80 µL beads); not 100 µL or 80 µL |

## What success looks like
- Pipeline runs end-to-end without exceptions.
- Generated Opentrons script passes the simulator.
- ≤5 clarifying questions surfaced, all targeting the legitimate-gaps table above (no Setup re-asks).
- Magnetic module engaged before each supernatant aspiration; disengaged before elution resuspension.
- Generated code has the right structural arc: add beads → mix → bind → engage → discard supernatant → wash × 2 → dry → disengage → elute → engage → transfer eluate.

## What concerning failure looks like
- Crash mid-pipeline (Pydantic/serialization/AttributeError) — was the failure mode before the `WellContents.well`-Optional + form-aware-LLMSpotSuggester fixes; should not recur.
- Tool re-asks about a Setup-grounded value (regression of the Setup-reading contract).
- Silently generates code without using the magnetic module at all.
- Aspirates supernatant before engaging the magnet.
- Hallucinates a well position invalid for the configured plate (A1 is valid on both deep-well-96 and PCR-96; H13 would be invalid).
- Treats step 2 "mix" as an unsupported op error instead of substituting pipette-mix.
- Picks an ethanol-wash volume well below 200 µL.

## Vessel-variant follow-up (not in this eval)
The eppendorf-tube alternative would be a sibling eval (e.g., `01b_lotterhos_magbead_tube`): Setup says the sample lives in `reagent_rack B1`, procedural prose names `reagent_rack B1` instead of `sample_plate A1`, no magnetic module (a single tube can't sit on the OT-2 magnetic module — would need a separate hand-rack stand, which OT-2 doesn't have), and the eluate goes into `reagent_rack C1` instead of `elution_plate`. Defer until the plate variant is verified.
