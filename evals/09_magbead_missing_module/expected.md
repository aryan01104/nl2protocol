# Expected behavior — 09_magbead_missing_module

## Case summary
**Base:** Case 01 (Lotterhos magbead fragmented DNA).
**Modification:** `mag_mod` removed from config. All other labware/pipettes identical.
**Error type:** Missing labware / module.

## What this tests
This is the **highest-signal error case**. Magbead protocols depend on the magnetic module as a hard requirement — without it, the protocol is unrunnable. The "right" behavior is to **refuse** or **surface the gap as a question**, not generate code.

This is also where this tool should clearly beat Claude Code: Claude Code would likely generate `magnetic_module = protocol.load_module(...)` and crash at runtime; this tool should detect the gap at extraction or planning time.

## Most likely overall outcome
**Refusal or targeted question.** The pipeline should recognize that ~7 of the 18 instruction steps require a magnet (engage, disengage, "on the magnet", "remove from magnet") and that no magnetic module is in the config.

## Gaps the tool MUST surface

| # | Gap | Source step | Why this is unmissable |
|---|---|---|---|
| 1 | Missing magnetic module | Steps 2.5, 2.7, 2.8, 2.10, 2.11, 2.17 explicitly invoke "the magnet" | No defensible default; instruction is impossible to execute |
| 2 | Whether to add the module or use a different approach | After surfacing #1 | User might want to use a manual magnet rack alongside OT-2 |

## Acceptable outcomes
- **Refuse** with reason: "This protocol requires a magnetic module which is not in your config. Add it, or adapt the protocol for manual magnet handling."
- **Ask**: "I see this needs a magnetic module but none is configured. Should I assume you have one and add it, or skip the magnet steps?"
- **Surface assumption**: "I'm assuming you'll use a manual magnetic rack between OT-2 steps. The protocol will pause at each engage step for you to move plates."

## What concerning failure looks like
- **Silent code generation** that calls `protocol.load_module('magnetic module gen2', '4')` despite no module in config (would crash at runtime).
- **Silent skipping** of all magnet engage/disengage steps (generates code that's silently broken — no engagement = no cleanup).
- **Code generation with wrong assumption** (e.g., assumes the sample plate IS the magnetic module).
- **Failure to detect the contradiction at all** — pipeline runs through extraction, planning, and code-gen without flagging.

## Open questions
- Does the pipeline's labware/module resolution stage have a "required module not found" check, or is it implicit?
- Does it depend on the instruction parser identifying "magnet" as a module reference?
