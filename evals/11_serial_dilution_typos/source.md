# Source — Case 04 with typos injected in the instruction

**Base case:** `evals/04_lotterhos_qpcr_serial_dilution/`
**Modification:** Instruction modified with realistic typos (reagent name misspellings, common confusable characters in volumes, "qPRC" for "qPCR" etc.). Config identical.
**Error type:** Typos in instruction.

## What this tests
Whether the pipeline:
- Treats slight reagent-name misspellings as the same reagent (does fuzzy matching)
- Recognizes "qPRC" as a typo for "qPCR" without changing meaning
- Handles "1O µl" (capital O instead of zero) — common OCR / fat-finger typo
- Doesn't get derailed by surface-level corruption that doesn't change semantics

This is the lower-severity error mode — typos shouldn't be a hard refusal cause, but they may surface as gentle clarifying questions ("did you mean...?").

## Typos injected
- "Standrads" for "Standards"
- "Dremo" for "Dermo"
- "1O µl" for "10 µl" (capital O as zero in two places)
- "qPRC" for "qPCR"
- "Buffr" for "Buffer" (in one place)
- "diluton" for "dilution" (in one place)
- "9O µl" for "90 µl"
