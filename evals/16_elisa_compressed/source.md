# Source — Case 06 compressed to a one-line user request

**Base case:** `evals/06_openwetware_elisa_wash/`
**Modification:** Instruction reduced from ~400 chars to ~85 chars. Config identical.
**Form:** Compressed.

## What this tests
The case-06 instruction was already short (~400 chars) — this case takes it down to the absolute minimum: a single one-liner. Pairs with case 06 as a register-tolerance test specifically on a workflow that's already terse to begin with.

## Token-vs-length data point
The delta from case 06 to case 16 is much smaller in absolute chars than the 01→15 delta. But both pairs let us measure:
- Whether token cost scales similarly across protocol kinds
- Whether the question-asking burden grows linearly with terseness

## Information removed vs. case 06
- The "By using excess volumes" explanation (rationale paragraph)
- The "tap against absorbent paper" finishing line
- The "immediately proceed" warning
