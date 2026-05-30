# Source — Case 01 with magnetic module removed from config

**Base case:** `evals/01_lotterhos_magbead/`
**Modification:** `mag_mod` entry deleted from `config.json`. Instruction identical.
**Error type:** Missing labware / module.

## What this tests
Whether the pipeline detects that the magbead protocol fundamentally needs a magnetic module and either:
(a) Refuses with a clear reason
(b) Asks the user to add a magnetic module
(c) Surfaces the contradiction as a gap

It should NOT silently generate code that attempts magnet engage / disengage on a non-existent module.
