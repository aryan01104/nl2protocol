# Expected behavior — 05_opentrons_csv_normalization

## Case summary
**Source:** Opentrons Protocols 5654c0 README ("DNA Normalization from .csv"). Over-structured template; CSV format is real.
**Instruction shape:** instructive + tabular — a CSV embedded in the instruction, plus 8 narrative steps.
**Config:** OT-2 with P20 + P300, water reservoir, 5 plates (LC source, HC source, LC dil, HC dil, norm), no modules.

## What this case tests
- **Embedded CSV parsing** — instruction contains a multi-row CSV table; tool must read it or ask
- **Multi-plate references by name** — "LC Dil Plate" / "Norm Plate" / "LC Plate" — must resolve to config labware
- **Sample-identifier text** — "pET4286", "pET4308" — these are plasmid names; tool shouldn't try to look them up as reagents
- **Variable per-well volumes** — water volumes vary per row (81.82, 80.00, etc.) — not uniform
- **8-step orchestration** — most operations so far have been &lt;5 steps; this is the first multi-step workflow

## Most likely overall outcome
Tool should **ask whether the CSV is the literal input data or an example schema** before generating code. Sub-question: whether to expect the CSV from a file or embedded.

## Gaps the tool should surface

| # | Gap | Source step | Why no default works |
|---|---|---|---|
| 1 | CSV as literal data vs schema example | Description: "the volumes described in the example CSV below" — "example" is ambiguous | If literal, the 9 rows are the actual transfers; if schema, the tool needs the real CSV |
| 2 | CSV file location / format | Note: "Save any .xlsx files as a .csv" | Where will the real CSV be? Per-run input? Pre-loaded? |
| 3 | Sample IDs (pET4286 etc.) — what are these? | CSV column | These are plasmid names labeling samples, not reagents to source |
| 4 | "LC Plates" vs "LC Plate" | Step 3 uses plural, step 5 singular | Config has one of each; instruction is inconsistent |
| 5 | Reuse-tip vs fresh-tip strategy | Not specified | Cross-contamination is a real concern across 9 transfers per phase |
| 6 | Water reservoir column | Not specified | 12-channel reservoir, no hint which column has water |
| 7 | Should the example row "pET4308" (3 entries: F3, G3, H3) all go to the same dest well "F3,G3,H3"? Looks like a per-row mapping. | CSV rows | Tool must understand row = single transfer; can't aggregate by ID |

## Failure modes to watch in generated code

| Risk | What we'd see |
|---|---|
| Treats example CSV as literal | Hard-coded 9 transfers with pET4286 etc. as the actual samples |
| Misses CSV entirely | Tool generates code that just walks "step 1" to "step 8" with no volume specificity |
| Pipette choice | 10 µL → P300 (less accurate); 90 µL → P20 (impossible) |
| Tip reuse | Single-tip across all transfers (massive cross-contamination) |
| Plate label resolution | "LC Dil Plate" not matched to config's `lc_dil_plate` |
| Water source | Tool puts water in reagent_rack instead of `water_reservoir` |
| Variable volume | All water transfers default to 90 µL despite CSV showing 80.00 / 81.82 variations |
| Misreads CSV column header `ID ` with trailing space | Parsing failure on trailing whitespace |

## What success looks like
- Pipeline runs end-to-end.
- Either asks about CSV literal-vs-schema OR explicitly assumes one with surfaced reasoning.
- Each plate-name reference in the instruction resolves to a config labware (or asks).
- Tip-reuse strategy is decided (or asked) — at minimum, fresh tip between source plates.
- Pipette selection: P20 for 10 µL transfers, P300 for 80–90 µL transfers.
- 8 narrative steps map to ≥8 distinct OT-2 operations in the generated code (more is fine if tip changes are explicit).

## What concerning failure looks like
- Crashes on the embedded CSV (treats as unparsable).
- Generates 1–2 transfers and stops (misses the multi-row CSV).
- Treats "pET4286" as a reagent to source from somewhere.
- Hallucinates plate positions in the source plates beyond what the CSV specifies.
- Single-tip workflow (cross-contamination).
- Picks P20 for 90 µL transfers.

## Open questions
- Does the pipeline have any CSV-parsing semantics, or does it treat embedded CSVs as opaque text?
- Are plate-name references resolved fuzzily ("LC Plates" → `lc_source_plate`) or strictly?
