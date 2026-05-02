# ADR-0001: Tip exhaustion is an error, not a warning

**Status:** Accepted
**Date:** 2026-04-26 (post-hoc; decision made in commit `961ebc3`)

## Context

The pipeline plans pipette tip usage by counting wells touched per step and comparing to tips loaded in the configured tipracks. When estimated usage exceeds available tips, the constraint checker can either:

(a) emit a **warning** and let the protocol be exported — the user discovers the problem at robot runtime, when the OT-2 halts mid-protocol and requires manual intervention. Reagents may be partially dispensed, plates may be partially populated, and the protocol typically cannot resume cleanly.

(b) emit an **error** and refuse to export — the user is forced to add tipracks, reduce scope, or switch to `same_tip` before the protocol can leave validation.

Earlier versions chose (a). Mid-run halts repeatedly produced wasted reagents and confused users who hadn't read the warning.

## Decision

Tip insufficiency is an **error** (`Severity.ERROR`) with violation type `TIP_INSUFFICIENT`. Validation refuses to pass. The user must resolve before export.

## Consequences

**Easier:**
- Failures surface in seconds (validation) instead of minutes (mid-run halt).
- No partial protocol state to clean up.
- The error message can suggest concrete fixes (add tipracks, switch to `same_tip`).

**Harder:**
- Conservative estimation can over-count tips (e.g. for steps that *might* reuse tips depending on runtime state). Some valid protocols may be rejected and require user override or manual `same_tip` annotation.
- Forces the planner to be honest about tip estimates per pipette mount, not aggregated.

**Now committed to:** any future validation gap that *could* cause mid-run halts is treated as fail-loud by default. Warnings are reserved for things that degrade quality but don't crash execution.
