# Expected behavior — 11_serial_dilution_typos

## Case summary
**Base:** Case 04 (Lotterhos qPCR serial dilution standards prep).
**Modification:** Instruction modified with 7 realistic typos (reagent names, capital-O for zero, common misspellings). Config identical.
**Error type:** Typos in instruction.

## What this tests
Typo tolerance is a lower-severity error mode than missing labware. The bar is **softer**: the tool shouldn't crash or refuse, but it MAY:
- Silently auto-correct (preferred for unambiguous typos)
- Ask "did you mean...?" for ambiguous ones (acceptable but adds friction)
- Crash or refuse (bad)

The typos here are all unambiguous to a human reader (Dremo → Dermo from context; 1O → 10 from volume range).

## Typos injected (with their intended meaning)

| Typo | Intended |
|---|---|
| qPRC | qPCR |
| Standrads | Standards |
| Dremo | Dermo |
| 1O µl (at top of step 1) | 10 µl |
| diluton | dilution |
| Buffr | Buffer |
| 9O µl | 90 µl |

## Most likely overall outcome
**Tool generates code as if typos didn't exist** — auto-correction is the right behavior here. Some clarifying questions on the volumes (1O / 9O) are acceptable since misreading a number is higher-risk than misreading a word.

## Acceptable outcomes
- **Silent correction** — tool maps Dremo → Dermo gBlock from rack, 1O → 10, etc. Generates code matching case 04.
- **Volume confirmation** — "I read '1O µl' as 10 µl — confirm?" Asking on the numbers is sensible since digit/letter confusion is high-risk.
- **Mixed correction + question** — auto-correct word typos, ask on volume typos.

## What concerning failure looks like
- **Crash on parse** — tool can't tokenize "qPRC" or "Standrads" → pipeline failure.
- **Treats Dremo and MSX as new unknown reagents** — labware-resolution step doesn't find a "Dremo" reagent, fails or hallucinates.
- **Pipettes 1 µL instead of 10 µL** because "1O" parsed as "1".
- **Treats "qPRC" as a different protocol type** — extraction confused.
- **Excessive questions** — asks about every typo individually; 5+ clarifying questions for what should be near-automatic correction.

## What success looks like
- Pipeline runs end-to-end with ≤ 2 clarifying questions.
- Generated code is structurally identical to what case 04 would produce.
- Any questions asked are about the numeric typos (1O, 9O), not the word typos.
- Pipeline output (if any) shows it understood "Dremo" = Dermo gBlock = same reagent as in case 04.

## Open questions
- Does the pipeline use fuzzy matching at the labware/reagent-resolution stage, or strict matching?
- If strict, this case will likely fail on the reagent resolution.
- Does the extraction LLM auto-correct typos in its output, or pass them through?

## Comparison vs Claude Code
This is a case where typo tolerance is a UX feature. Claude Code (without configuration) will likely handle typos OK because it's a general-purpose LLM. The interesting question is whether nl2protocol matches that level (because typos are common) or whether the constrained pipeline structure makes it brittler.
