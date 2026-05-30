# Source — Case 03 with P20 pipette removed from config

**Base case:** `evals/03_openwetware_phusion_pcr/`
**Modification:** P20 pipette removed from config; only P300 single-channel remains. Tip rack for P20 also removed. Instruction identical.
**Error type:** Missing / insufficient pipette.

## What this tests
The Phusion PCR recipe needs sub-µL precision in places (0.5 µL polymerase) and small volumes throughout (1 µL dNTPs, 2 µL template, 2.5 µL primers each). P20 is the accurate-range tool for these. With only P300, the small volumes fall well below accurate-pipette range.

Tool should detect the volume-vs-pipette mismatch and either:
- Refuse with a clear reason
- Propose a mastermix-scaling workaround (so individual additions become larger)
- Ask user to add P20
