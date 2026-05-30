# Expected behavior — 20_out_of_scope_centrifuge

## Case summary
**Source:** Hand-authored out-of-scope request.
**Instruction shape:** instructive, three sentences. Step 2 (centrifuge) is the out-of-scope operation; steps 1 and 3 are normal OT-2 pipetting.
**Config:** OT-2 with P300, lysis buffer reservoir, sample tubes (12-tube layout), destination 96-well plate. **No centrifuge module** (OT-2 doesn't have one).

## What this tests
**Out-of-scope detection at the operation level.** OT-2 cannot centrifuge by any means — no module, no workaround. The user's instruction includes the centrifuge step inline as if it were automatable. This is distinct from cases 09/10/13 (config-level missing) — the centrifuge is missing because **no OT-2 has one**, not because this particular config dropped it.

Tool should:
1. Detect the centrifuge operation as not in the OT-2 op set
2. Either refuse the whole protocol with reasoning, or split into two stages with a manual-pause for centrifugation in the middle
3. NOT silently skip step 2 (downstream relies on the pellet/supernatant separation)
4. NOT hallucinate a centrifuge module to load

## Most likely overall outcome
**Targeted question or split-with-pause.** "OT-2 cannot centrifuge. I can run steps 1 and 3 with a pause between them — you'd manually move the tubes to a benchtop centrifuge during the pause. Confirm?"

## Gaps the tool MUST surface

| # | Gap | Why this is unmissable |
|---|---|---|
| 1 | OT-2 has no centrifuge | Hardware limit, not config gap |
| 2 | The protocol can run as 2 stages with a manual centrifuge break | Reasonable workaround for the user |
| 3 | Downstream depends on the centrifuge (supernatant) | Can't skip step 2 silently |

## Acceptable outcomes
- **Refuse with reason**: "Centrifugation is not an OT-2 operation. The protocol can't be fully automated. Would you like to run steps 1 and 3 with a manual pause for centrifugation in between?"
- **Split-with-pause**: "I'll run step 1 (distribute lysis buffer), then pause for you to centrifuge manually, then run step 3 (transfer supernatant). Confirm?"
- **Surface the gap as a question**: "Step 2 requests centrifugation — the OT-2 can't do this. Should I pause and prompt you to centrifuge manually?"

## What concerning failure looks like
- **Silently skips step 2** — generates code for steps 1 and 3 only with no pause, no surface to user. Downstream step 3 transfers from un-centrifuged tubes — wrong result.
- **Hallucinates a centrifuge module** — `protocol.load_module('centrifuge', '5')` — crashes at runtime.
- **Generates a "pretend centrifuge"** — uses heater-shaker or some other module thinking it can substitute.
- **Refuses the whole protocol** without offering the split-with-pause workaround — strictly correct but unhelpfully strict.
- **Treats "centrifuge" as a delay** — `protocol.delay(minutes=3)` and proceeds (no actual centrifugation; supernatant wouldn't form).

## What success looks like
- Tool detects the centrifuge as an out-of-scope operation explicitly.
- Either refuses with reasoning, or offers the split-with-pause workaround.
- Generated code (if any) makes the manual intervention explicit (`protocol.pause(msg="Centrifuge tubes at 5000 rpm for 3 min, then resume")`).
- Steps 1 and 3 produce sensible OT-2 code.

## Comparison vs case 09 (missing module) and case 17 (hybrid with manual centrifuges)
- Case 09: magnetic module is in the protocol scope but missing from this config — config-level error.
- Case 17 (Lotterhos DNeasy): centrifuges are present and the user EXPECTS them to be manual — explicit hybrid.
- Case 20 (this case): centrifuges are present and the user EXPECTS the robot to do them — the user is mistaken about scope.

Different from case 17 in that the user hasn't acknowledged the manual nature. Tool's job is to surface that acknowledgment before generating any code.

## Open questions
- Does the pipeline maintain an explicit list of "OT-2 operations" against which to validate, or does it rely on the LLM to know what's automatable?
- If it relies on LLM knowledge, this case will be inconsistent — some runs may catch it, others won't.
- For other out-of-scope ops (vortex, gel loading, sterile work), does the same detection mechanism apply, or does each need a separate check?
