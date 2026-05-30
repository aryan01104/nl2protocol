# Source — Case 01 compressed to a user-request form

**Base case:** `evals/01_lotterhos_magbead/`
**Modification:** Instruction reduced from 18 numbered sub-steps (~2200 chars) to a 1-sentence user request (~150 chars). Config identical to case 01.
**Form:** Compressed (the F-vs-C axis, second pass).

## What this tests
Whether the pipeline can produce the same downstream behavior when the user gives a terse "I want this" request instead of a step-by-step protocol document.

The expectation is: **same code, same questions** as case 01 — if compressed form produces different output, the tool's behavior depends too much on input verbosity.

## Token-vs-length data point
This case (instruction ~150 chars) vs. case 01 (~2200 chars) is the natural A/B for the **token usage vs. instruction length** todo. After running both, compare:
- Total LLM call count
- Total input tokens
- Total output tokens
- Wall-clock latency

The hypothesis: input-tokens scale roughly with instruction length, but output tokens and call count should be near-constant (since the downstream code is the same).
