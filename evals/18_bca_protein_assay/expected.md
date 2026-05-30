# Expected behavior — 18_bca_protein_assay

## Case summary
**Source:** Thermo Pierce BCA Protein Assay (microplate procedure).
**Instruction shape:** instructive, formal numbered (steps 1–7), ~720 chars.
**Config:** OT-2 with P20 + P300, reagent rack, BSA standards plate, WR reservoir (195 mL), assay plate on heater-shaker module.

## User-story dimension this case adds
This is the **protein quantification** user story — distinct from the ELISA wash (case 06), even though both involve plate-based immunoassay-adjacent workflows. Different vocabulary:
- "BSA standards", "Working Reagent", "Reagent A + B 50:1", "562 nm absorbance", "µg/mL", "Bradford / BCA"
- vs. ELISA's "PBS-T", "antibody", "wash", "blocking"

Different operations too: BCA combines (a) serial dilution (standard curve prep), (b) sample addition, (c) bulk WR dispense, (d) shake/incubate. ELISA wash is just (a) repeat-N aspirate-dispense.

## What this case tests beyond cases 01–17
- **Composite workflow** — combines patterns from serial dilution + plate stamping + ELISA-like dispense + thermocycler-like incubation
- **Heater-shaker module** — first case using this module
- **Working Reagent prep on the fly** — implies user has separate Reagent A and Reagent B; tool needs to know to mix them
- **Standards curve generation** — 9 dilution points, needs source BSA tube + diluent

## Most likely overall outcome
Pipeline should generate a long multi-stage code with 5+ distinct stages. Lots of questions about reagent locations, sample count, and which wells of the assay plate go where.

## Gaps the tool should surface

| # | Gap | Source step | Issue |
|---|---|---|---|
| 1 | BSA stock location | Step 1: "the 2.0 mg/mL BSA stock" | Not specified in instruction; needs to be in reagent_rack |
| 2 | Sample buffer (diluent) location | Step 1: "in sample buffer" | Not specified |
| 3 | Reagent A and Reagent B locations | Step 2: ratio specified, not locations | Likely in reagent_rack |
| 4 | Sample count + sample locations | Step 3: "each unknown sample replicate" — count unstated | |
| 5 | Assay plate layout — which wells for standards, which for samples? | Steps 3 + 4 | Common: standards in col 1, samples cols 2+ |
| 6 | Whether the standards_plate and assay_plate are different | Config has both | Maybe standards go directly to assay_plate, making standards_plate unused |
| 7 | Heater-shaker shake duration after WR addition | Step 5: 30 seconds | OK to use as-is; tool should emit module command |
| 8 | Incubation temperature/time | Step 6: 37°C, 30 min | Direct mapping to heater_shaker.set_target_temperature |

## Failure modes to watch in generated code

| Risk | What we'd see |
|---|---|
| Skips standard curve prep | Tool treats step 1 as descriptive instead of an actionable serial dilution |
| Skips WR prep | Tool assumes WR is pre-made (it isn't in config) and tries to aspirate from a non-existent source |
| Wrong serial-dilution count | 8 or 10 instead of 9 |
| Pipettes 200 µL with P20 | Wrong pipette selection |
| Misses heater-shaker | Generates code with `time.sleep(1800)` instead of using the module |
| Doesn't separate Reagent A from Reagent B | Treats them as one reagent |
| Wrong WR ratio | Mixes 1:50 instead of 50:1 |

## What success looks like
- Pipeline runs end-to-end.
- Code structure has 4 distinct stages: (a) standard curve serial dilution, (b) WR prep, (c) sample + standard dispense to assay plate, (d) WR addition + incubation.
- Heater-shaker module used for shake + incubation.
- ≥ 4 clarifying questions surfaced.
- P20 used for 25 µL transfers; P300 used for 200 µL.
- Generated code separates Reagent A and Reagent B with the 50:1 mixing math correct.

## What concerning failure looks like
- Crashes on parsing the multi-stage workflow.
- Skips the standard curve entirely (treats as pre-prepared).
- Hallucinates a plate reader op.
- Wrong WR ratio (1:50 instead of 50:1) — quantification would fail.
- Uses single tip across all standard dilutions — cross-contamination in the curve.

## Open questions
- Does the pipeline handle multi-stage composite workflows (serial dilution + dispense + heater-shake) as one extraction, or break it into pipeline-wise stages?
- Heater-shaker is a less-common module than magnetic / thermocycler — is it fully supported, or partially?

## Comparison vs case 04 (serial dilution)
Case 04's serial dilution is the prep portion of a qPCR standards workflow.
Case 18 also contains a serial dilution (BSA standards), but it's PART OF a longer composite.
Pipeline behavior on case 18's standards step should match case 04's standalone serial dilution — if it doesn't, the standalone-vs-embedded distinction is a tool weakness.
