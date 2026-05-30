# Expected behavior — 16_elisa_compressed

## Case summary
**Base:** Case 06 (Mabtech ELISA wash, full form).
**Modification:** Instruction compressed from ~400 chars to ~85 chars. Config identical.
**Form axis:** Compressed.

## What this tests
Same as case 15 but on a different protocol kind:
1. **Register tolerance** — does the pipeline produce the same code from a one-liner as from the full Mabtech text?
2. **Token-cost vs. length** — the second data point for the token-usage todo, on a different kind of workflow.

## Expected delta from case 06

| Stage | Case 06 (full) | Case 16 (compressed) |
|---|---|---|
| Generated code structure | Same | Same |
| Questions asked | 1–2 | Probably same or slightly more |
| Input tokens | ~150 from instruction | ~30 from instruction |
| Output tokens | Roughly equal | Roughly equal |

Less change here than 01↔15 because case 06's instruction was already terse. This is intentional: comparing 01↔15 vs. 06↔16 tells us whether compression's benefit/cost depends on the base instruction length.

## Information present in compressed instruction
- 5 washes (same as case 06)
- PBS-T (same)
- "across the assay plate" (implies all 96 wells, same as case 06)
- 300 µL per well (same)

## Information NOT in compressed instruction (vs case 06)
- The 300 µL justification ("excess volumes ensure no residual molecules cling")
- The finishing-step note ("tap against absorbent paper")
- The "immediately proceed" warning

## Most likely overall outcome
Pipeline runs almost identically to case 06. The omitted bits (excess-volume rationale, tapping) are either auto-supplied by the tool or asked.

The biggest expected delta is that the **tapping step is no longer in the instruction at all** — case 06 should have surfaced this as "can't automate"; case 16 won't, because it's not there to surface. Either:
- (a) Tool generates the wash code and stops cleanly (since tapping isn't asked).
- (b) Tool asks "anything after the washes?" — over-asking but defensible.

## Acceptable outcomes
- **Same code as case 06, minus the tapping pause.**
- **Same code with same pause/skip pattern** even though tapping isn't mentioned — would mean tool retrieves ELISA convention from its own training.
- **Asks "should I add a finishing step?"** — acceptable.

## What concerning failure looks like
- **Generates code with FEWER than 5 washes** (one or two only, misreading "5" or defaulting to 3).
- **Pipettes wrong volume** (200 µL or 100 µL instead of 300 µL).
- **Misses the 96-well multi-channel optimization** despite simple instruction.
- **Crashes on the terse input** — pipeline expects more structure than it gets.

## What success looks like
- Pipeline runs end-to-end.
- Generated code traverses 12 columns × 5 washes with multi-channel (or 96 × 5 with single — both acceptable).
- Volume = 300 µL, wash count = 5 — both verbatim from the instruction.
- ≤ 2 clarifying questions.

## Token-vs-length comparison data

After running cases 01, 06, 15, 16:

| Pair | Δ chars | Δ input tokens | Δ output tokens | Δ LLM calls |
|---|---|---|---|---|
| 01 (full) → 15 (compressed) | -2050 | ? | ? | ? |
| 06 (full) → 16 (compressed) | -315 | ? | ? | ? |

Goal: see if (Δ input tokens) / (Δ chars) is roughly constant, OR if cost-per-char differs between protocol kinds.
