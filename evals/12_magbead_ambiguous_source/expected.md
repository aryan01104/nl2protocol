# Expected behavior — 12_magbead_ambiguous_source

## Case summary
**Base:** Case 01 (Lotterhos magbead fragmented DNA).
**Modification:** Config now has two 96-well deep plates — `sample_plate` (slot 4, on mag_mod) and `input_plate` (slot 5). Both are plausible holders for "the fragmented DNA sample" referenced in the instruction.
**Error type:** Ambiguous reference.

## What this tests
The instruction says "the 100 µL fragmented DNA sample" — a singular reference, no plate name. With two candidate plates in the config, this is genuinely ambiguous.

Right behavior: tool detects the ambiguity and asks. Wrong behavior: silently picks one (most likely `sample_plate` because of the name match, but `input_plate` is also reasonable since "input" implies starting material).

## What this case tests beyond Case 01
- Case 01 already had implicit ambiguity (no well specified). This case adds a second plate so there's also ambiguity at the labware level.
- Tests whether the labware-resolution stage detects multi-candidate matches.

## Most likely overall outcome
**Targeted disambiguation question.** Tool should say something like: "The instruction refers to 'the fragmented DNA sample' — which plate holds it: `sample_plate` (on the magnetic module) or `input_plate`?"

## Gaps the tool should surface

| # | Gap | Source step | Why ambiguous |
|---|---|---|---|
| 1 | Which plate is the source | 2.2: "the 100 µL fragmented DNA sample" | Both plates fit the description |
| 2 | Whether the source moves to the magnet | If the source is `input_plate` (not on mag_mod), the tool needs to know if it's a transfer step or whether the user intends `input_plate` to be the working plate (which would conflict with the magnet workflow) |

## Acceptable outcomes
- **Ask which plate**: clean disambiguation question, then proceeds with chosen one.
- **Surface preference with reasoning**: "I'm picking `sample_plate` because it's on the magnetic module, which the protocol requires. Confirm?" — acceptable because the reasoning is sound.
- **Refuse to proceed without confirmation**: stricter than necessary but defensible.

## What concerning failure looks like
- **Silent pick** without surfacing — generates code for `sample_plate` (or `input_plate`) without telling the user which it chose.
- **Generates code that touches both plates** — tool gets confused and uses one for one step and the other for another.
- **Crashes on ambiguity** — labware resolution stage errors instead of asking.
- **Picks `input_plate`** silently (worse than picking `sample_plate` because `input_plate` isn't on the mag module, so the magnet steps would target the wrong slot).

## What success looks like
- Tool surfaces the ambiguity within the first 2 stages.
- The question is specific and offers both candidates by name.
- If the tool picks one with reasoning, the reasoning is grounded (e.g., "on the magnet" vs. random choice).

## Open question
Does the pipeline's labware-assignment stage handle 2-candidate ambiguity, or does it only check for "no candidate found"? This is the load-bearing question for the case.
