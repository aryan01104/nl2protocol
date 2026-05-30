# Expected behavior — 17_lotterhos_dna_extraction

## Case summary
**Source:** Lotterhos Lab Plate DNA Extraction (Qiagen DNeasy 96) — EXTRACTION section only.
**Instruction shape:** instructive, formal numbered (steps 3, 6, 7, 8, 9, 10, 11, 12, 13), ~1900 chars.
**Config:** OT-2 with P1000 single, three large reservoirs for buffers (AL, AW1, AW2), 12-well reservoir for Low TE elution buffer, sample rack, DNeasy plate.

## User-story dimension this case adds
This is the **DNA extraction** user story — distinct from magbead cleanup (case 01). Same lab problem space (purify nucleic acids) but:
- Different vocabulary: "Buffer AL / AW1 / AW2 / Low TE", "lysate", "DNeasy plate", "S-block", "elution microtubes" vs. "KAPA beads", "ethanol", "supernatant"
- Different reagent classes: column-based chemistry, not bead-based
- Manual centrifuge steps embedded mid-workflow (NOT present in magbead cleanup)

## What this case tests beyond cases 01–16
- **Hybrid OT-2 + manual workflow** — explicit `pause` / user-intervention requests required for centrifuge steps (steps 7, 9, 11, 13's final centrifuge)
- **Different DNA-workflow vocabulary** than magbead (Buffer AL vs. SPRI beads etc.)
- **Bulk multichannel pipetting** at 410–600 µL volumes (P1000 territory)
- **Multi-stage with state transitions** at centrifuge boundaries

## Most likely overall outcome
Pipeline should generate code with explicit `pause` or "manual centrifuge" steps between pipetting stages, plus questions about:
- Reagent reservoir column assignments
- Sample count and plate well subset
- Which centrifuge steps are user-handled
- Multichannel vs. single-channel pipetting choice (config has P1000 single; instruction says "multichannel" repeatedly)

## Gaps the tool should surface

| # | Gap | Source step | Issue |
|---|---|---|---|
| 1 | Pipette mismatch | Instruction says "1000μL multichannel pipette" repeatedly; config has P1000 SINGLE | Either ask user to add multi, or use single in a loop |
| 2 | Sample count + well subset | Instruction implies a full plate (96 wells) but no explicit count | |
| 3 | Lysate source location | Step 6: "remove caps from the collection microtubes" — config has `sample_rack` but no per-well content info | |
| 4 | Centrifuge handling | Steps 7, 9, 11, 13 require centrifuge | Tool should emit `pause` with instruction text |
| 5 | Tape application / removal | Steps 7, 9, 11, 13 mention "AirPore tape sheet" | Manual; tool should pause |
| 6 | Buffer reservoir capacity | 55 mL Buffer AW1, 55 mL AW2, 45 mL Buffer AL | Config has 195 mL reservoirs (overcap is fine) |
| 7 | Elution incubation timing | Step 13: "incubate for 10 min at room temperature" | Tool should emit `delay` |

## Failure modes to watch in generated code

| Risk | What we'd see |
|---|---|
| Skips centrifuges entirely | Pipetting steps run without centrifuge `pause` — at runtime, centrifuge-required steps would be skipped silently |
| Hallucinates a centrifuge module | `protocol.load_module('centrifuge')` — OT-2 has no centrifuge module |
| Pipettes 410+ µL with default tips | Need 1000-µL tips; 200-µL would underfill |
| Forces multichannel | Tries `p1000_multi` but config has `p1000_single` — crash or refusal |
| Skips tape steps without noting | No surface to user about manual sealing |
| Generates code for the wrong section | Tool includes the tissue-lysing manual steps (1–15 of TISSUE LYSING section) even though instruction only shows EXTRACTION |

## What success looks like
- Pipeline runs end-to-end without exceptions.
- Either surfaces the multichannel-vs-single pipette mismatch, OR generates code using single-channel in a loop (acceptable workaround).
- Generates explicit `pause` or user-intervention statements for each centrifuge step (4 total).
- Generates `pause` or `delay` for the room-temp elution incubation (step 13, 10 min).
- Vocabulary preserved — "Buffer AL", "AW1", "AW2", "Low TE" used correctly in code comments / variable names.

## What concerning failure looks like
- Generates code as if centrifuges don't exist.
- Crashes on the "multichannel pipette" reference when config has single.
- Hallucinates a centrifuge module to load.
- Treats the extraction as a magbead workflow (mixes up Buffer AL with ethanol).
- Skips the elution buffer step entirely.

## Open questions
- Does the pipeline have a "user manual step" / "external step" mechanism, or only OT-2 ops + pause?
- How does it handle the "multichannel" vocabulary mismatch with single-channel config — error or graceful fallback?
- Does the LLM extraction recognize Buffer AL, AW1, AW2, Low TE as distinct labeled reagents vs. confusing them?
