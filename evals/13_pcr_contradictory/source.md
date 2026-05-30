# Source — Case 03 with config that can't accommodate the largest instruction volume

**Base case:** `evals/03_openwetware_phusion_pcr/`
**Modification:**
- Config: dropped P300 and the 200-µL tip rack. Only P20 with 20-µL filter tips remains.
- Instruction: identical to case 03 (includes the 31.5 µL water step).

**Error type:** Contradictory instruction (volume vs. tip).

## What this tests
The Phusion recipe's 31.5 µL water transfer requires more than the configured 20-µL filter tip can hold in a single aspirate. A 20-µL tip caps at 20 µL physical capacity. The tool must:
- Detect the volume-vs-tip contradiction
- Refuse or surface the gap with a clear reason
- Offer a resolution (multi-step transfer, or add a larger tip rack, or use a different recipe scale)

This is the inverse of case 10. Case 10 removed P20 (small volumes broken); case 13 removes P300 + larger tips (large volumes broken).

## Why this is distinct from case 10
- Case 10: pipette range issue — accuracy concern (P300 can pipette 0.5 µL but inaccurately)
- Case 13: tip capacity issue — physical impossibility (20 µL tip can't hold 31.5 µL in one shot)
