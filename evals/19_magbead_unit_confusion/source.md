# Source — Case 01 with a mL/µL unit confusion injected

**Base case:** `evals/01_lotterhos_magbead/`
**Modification:** Step 2.2 changed from "80 μL of KAPA Pure Beads" to "80 mL of KAPA Pure Beads" — a 1000× over-specification. The neighboring "100 µL fragmented DNA sample" in the same sentence preserves the correct unit, making the contradiction visible within a single line.
**Error type:** Unit confusion (mL/µL).

## What this tests
The most catastrophic class of real lab error. mL vs. µL confusion produces 1000× volume errors. The OT-2 has at most 1000-µL tips — physically can't pipette 80 mL in a single transfer (and even with a multi-aspirate loop it would need 80+ tips).

Tool should:
- Recognize that 80 mL is physically impossible with OT-2 hardware
- Notice the in-step inconsistency (80 mL of beads vs. 100 µL of sample — a 1000× difference for a 0.8X bead-to-sample ratio doesn't make sense)
- Refuse, or ask "Did you mean 80 µL?"

This is also where the tool should **clearly beat Claude Code**, which would attempt the 80 mL transfer literally and crash at runtime.

## What changed vs. case 01
Only one character: a single "µ" replaced with nothing, turning "80 μL" into "80 mL". Everything else identical.
