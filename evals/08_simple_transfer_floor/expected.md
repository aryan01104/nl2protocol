# Expected behavior — 08_simple_transfer_floor

## Case summary
**Source:** Hand-authored floor case.
**Instruction shape:** instructive, single sentence (~90 chars).
**Config:** OT-2 with P300 single-channel, source plate, destination plate, no modules.

## What this case tests
- **Pipeline runs at all** — if this fails, everything fails
- **Direct happy path** — no ambiguity in the instruction, no missing info, every entity named
- **Minimal asking** — pipeline should NOT over-ask on a fully-specified trivial case

## Most likely overall outcome
Tool should generate ~3 lines of OT-2 code with zero or minimal clarifying questions: pick up tip, aspirate 50 µL from `source_plate['A1']`, dispense into `destination_plate['A1']`, drop tip.

## Gaps the tool should surface
**None expected.** The instruction is fully specified. If the tool asks anything, it's a signal of over-caution.

| # | Hypothetical gap | Why it'd be wrong to ask |
|---|---|---|
| 1 | "Which pipette to use?" | Only one pipette in config; obvious choice |
| 2 | "What's in source A1?" | Doesn't matter for executing the instruction; assume the user has loaded it |
| 3 | "Tip strategy?" | Single transfer; no tip change needed mid-operation |

A SINGLE clarifying question on a sample-volume detail (e.g. "confirm 50 µL not 5 µL") would be acceptable; more than one suggests over-asking.

## Failure modes to watch in generated code

| Risk | What we'd see |
|---|---|
| Crashes | Pipeline can't even handle the trivial case — fundamental gap |
| Over-asks | Asks 3+ questions on a fully-specified single transfer |
| Wrong volume | Pipettes 5, 500, or some other number |
| Wrong well | Source A1 → destination B1 or some other mismatch |
| Picks up multiple tips | Bizarre but possible if the tool is multi-channel-confused |

## What success looks like
- Pipeline runs end-to-end with 0–1 clarifying questions.
- Generated code: pickup tip from `tiprack_300`, aspirate 50 µL from `source_plate['A1']`, dispense to `destination_plate['A1']`, drop tip.
- Total generated code is short — a single `transfer()` call or 3–4 explicit primitive calls.

## What concerning failure looks like
- Crash.
- 5+ clarifying questions for a fully-specified instruction.
- Wrong volume or wrong well in generated code.
- Generates a multi-step protocol when only one transfer was asked.

## Open questions
- What's the minimum question-burden the pipeline imposes? This case is the cleanest measurement — if it asks 2 things, every more complex case will ask at least that many.
