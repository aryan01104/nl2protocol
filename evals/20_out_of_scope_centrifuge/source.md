# Source — Hand-authored out-of-scope user request

**Origin:** Hand-authored — not derived from a published lab protocol.
**Why hand-authored:** This is testing what happens when a user asks the OT-2 to do something outside its hardware envelope. Published protocols rarely include centrifuge / vortex / gel steps as part of an automated workflow, but real users **do** type these all the time because they think of the protocol as a whole and assume the robot can run the whole thing.

The instruction below is representative of a user asking the OT-2 to perform a 3-step prep that includes a centrifuge step in the middle — the user expects the robot to handle the whole thing.

## What the user got wrong
The OT-2 has no centrifuge module. Centrifugation can be performed manually (user moves the tubes), or with a separate benchtop centrifuge between OT-2 stages, but never automated within an OT-2 protocol run.

## Why this is a distinct error type
Cases 09, 10, 13 are config-level errors (missing module, wrong pipette). Case 20 is an **operation-level** error — the user's instruction requests an operation that OT-2 cannot perform regardless of config. Even with infinite labware and pipettes, the OT-2 cannot centrifuge.

## What the tool should do
- Detect the centrifuge step as out-of-scope
- Either refuse the whole protocol with a clear reason, or pause for manual centrifugation between the pipetting steps
- NOT silently skip the centrifuge step (downstream depends on pellet/supernatant separation)
- NOT hallucinate a centrifuge module
