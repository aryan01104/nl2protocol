# Source — Case 05 with CSV referencing rows that don't exist on 96-well plates

**Base case:** `evals/05_opentrons_csv_normalization/`
**Modification:** CSV rows modified to reference wells I1, J3, K3 — rows that don't exist on standard 96-well plates (which only go A–H). Original valid rows preserved alongside the invalid ones.
**Error type:** Hallucinated wells / config mismatch.

## What this tests
Whether the pipeline validates that referenced wells actually exist on the configured labware before generating transfer code. Real users with sloppy spreadsheets do this all the time — a forgotten copy-paste expanded the rows beyond H.

Tool should:
- Detect the invalid well references at extraction or labware-resolution time
- Surface them by row / by well coordinate
- Not silently skip them, generate code with NaN wells, or crash

## Invalid wells introduced
| Row | Plate | Well | Why invalid |
|---|---|---|---|
| (CSV row 4) | LC 96-well plate | I1 | Row I doesn't exist (96-well = A–H) |
| (CSV row 5) | HC 96-well plate | J3 | Row J doesn't exist |
| (CSV row 6) | LC Dil Plate | K3 | Row K doesn't exist |
