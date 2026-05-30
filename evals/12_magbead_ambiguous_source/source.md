# Source — Case 01 with two candidate sample plates in config

**Base case:** `evals/01_lotterhos_magbead/`
**Modification:**
- Config: added a second 96-well deep plate `input_plate` (slot 5 — moved `elution_plate` to slot 6). Both `sample_plate` and `input_plate` are plausible "the fragmented DNA sample" containers.
- Instruction: identical to case 01 wording — "the 100 µL fragmented DNA sample" is now genuinely ambiguous (matches either plate).

**Error type:** Ambiguous reference.

## What this tests
Whether the pipeline detects that the instruction's singular "the sample" doesn't uniquely identify a labware in the config, and asks for clarification rather than silently picking one.

This is one of the cleanest cases where the tool should beat Claude Code: ambiguity in real lab inputs is the norm (users have multiple plates on deck), and the right answer is "ask," not "guess."
