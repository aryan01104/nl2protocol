# Expected behavior — 14_normalization_invalid_wells

## Case summary
**Base:** Case 05 (Opentrons CSV-driven normalization).
**Modification:** CSV rows reference wells I1, J3, K3 — invalid on 96-well plates (only A–H rows exist).
**Error type:** Hallucinated wells.

## What this tests
This is the **classic "user's spreadsheet has bad data" failure mode**. Real users typoing in well coordinates is extremely common. Tool should:
- Validate referenced wells against the configured labware
- Surface the invalid rows specifically — not refuse the whole protocol over a few bad rows
- Either skip bad rows with a warning, or refuse with a list of invalid coordinates

## Most likely overall outcome
**Targeted question with the specific invalid rows.** "Rows 4, 5, and 6 of the CSV reference wells I1, J3, K3 which don't exist on a 96-well plate. Skip these rows, or do you mean different wells?"

## Gaps the tool MUST surface

| # | Gap | CSV row | Specific issue |
|---|---|---|---|
| 1 | I1 doesn't exist on 96-well plate | CSV row 4 | Row I not present (max H) |
| 2 | J3 doesn't exist on 96-well plate | CSV row 5 | Row J not present |
| 3 | K3 doesn't exist on LC Dil Plate (also 96-well) | CSV row 6 | Row K not present |

## Acceptable outcomes
- **List invalid rows + ask**: "Rows 4–6 have invalid wells (I1, J3, K3). Skip those rows or correct them?"
- **Refuse with specifics**: "Cannot proceed: 3 CSV rows reference wells beyond H on 96-well plates. List: row 4 (I1), row 5 (J3), row 6 (K3)."
- **Auto-skip with warning**: "Skipped 3 rows with invalid wells (I1, J3, K3). Generated code covers rows 1–3 and 7–9 only."

## What concerning failure looks like
- **Silent invalid pipetting** — generates code with `source_plate['I1']` which would crash at OT-2 runtime.
- **Whole-protocol refusal** — refuses the entire normalization over 3 bad rows when 6 are valid.
- **Hallucinated correction** — silently changes I1 → H1 or I1 → A2 without surfacing.
- **Crashes on extraction** — pipeline can't parse the CSV.
- **Generic error** — says "invalid input" without pointing to specific rows.

## What success looks like
- Tool surfaces the specific invalid wells by row number and well coordinate.
- Either offers to skip and proceed, or asks for corrections.
- Valid rows (1–3, 7–9) would still produce executable code.
- The error message names the labware constraint (96-well, A–H rows) explicitly.

## Open questions
- Does the pipeline have well-existence validation tied to the labware's row/column range (which `enrich_config_with_wells` in `config.py` actually computes)?
- If yes, this should be caught at the labware-resolution stage cleanly.
- If no — relies on LLM knowledge — this case may be inconsistent.

## Comparison vs other error cases
This is the most "structured" error — the bad data is identifiable by row/column. Cases 09 (missing module) and 13 (volume vs tip) are about config gaps; case 14 is about input-data gaps. Together they test three different validation layers.
