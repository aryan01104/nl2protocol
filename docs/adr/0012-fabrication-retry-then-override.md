# ADR-0012: Fabrication override path with audit trail

**Status:** Accepted
**Date:** 2026-05-05

## Context

ADR-0008's orchestrator routes every kind of Gap through one detect → suggest → review → user-confirm loop. `ProvenanceWarningDetector` emits Gaps for fabrications — values whose claimed `cited_text` doesn't appear in the user's instruction. Generic suggesters (deterministic + `LLMSpotSuggester`) try to resolve them; if nothing succeeds, the user gets a CLI prompt with accept / edit / skip / quit.

Pre-PR3b had a "Proceed anyway? [y/N]" prompt that produced specs with no audit trail of the user's override. PR3b removed it, replacing with a hard block via deletion of the `format_for_confirmation` legacy queue. That's gone too far in the other direction. There's a real (rare) class of cases where:

- The LLM extracted a real value from the instruction
- The verifier (substring matching, case-insensitive, whitespace-normalized) flags the cited_text as fabricated
- But the verifier is wrong — the user's instruction has a synonym, abbreviation, or paraphrase the verifier doesn't recognize as a match

In those cases, the user knows their instruction grounds the value; the verifier doesn't. The right affordance is for the user to override AND for the audit trail to record "system flagged this; user committed anyway" so a downstream reader of the report can see exactly which values bypassed the verifier check.

## Decision

Add a new `Resolution.action = "override"` and a new `Provenance.review_status = "user_overrode_fabrication"`. Surface an `[o]verride (keep current value)` option in `CLIConfirmationHandler` ONLY for `gap.kind == "fabricated"`. The orchestrator's `default_apply_resolution` handles the new action by NOT writing the value and instead stamping the existing field's primary `provenance.review_status` with `user_overrode_fabrication`.

The HTML report (ADR-0011) renders overridden fabrications visibly so any reader of the audit can spot them. No new suggester needed — the existing `LLMSpotSuggester` already handles fabrication retries generically; its prompt receives the gap.description which carries the "claimed X but not in text" message. If LLMSpot resolves the fabrication, the user never sees it. If LLMSpot can't, the user sees the override option.

### What the user sees

For a fabrication that survived the suggester pass:

```
[BLOCKER] Step 3 volume — fabrication detected
  current: '50uL'
  (description from underlying verifier explaining what's flagged)
  suggested: <if LLMSpot produced one>
    reasoning, confidence, etc.

  [a]ccept suggestion, [e]dit, [o]verride (keep current value), [s]kip, [q]uit
```

The `[o]verride` option appears only for fabrication gaps. Other gap kinds keep the existing four-option prompt.

### Why Option B (override path only) instead of Option A (override + dedicated retry suggester)

Option A would have added a `FabricationRetrySuggester` — a focused LLM retry whose prompt explicitly carries the previous failure context ("you previously cited 'X' but it wasn't in the instruction; try again"). The retry would auto-resolve clear-error fabrications (paraphrase recovery, typo correction) without bothering the user.

Option B (this ADR) skips the dedicated retry. `LLMSpotSuggester` handles fabrication gaps generically — its existing prompt includes `gap.description` which already contains the verifier's flag message, so the LLM has the context to attempt a fix. Less reliable than a focused retry, but fabrications are rare AND if LLMSpot misses one, the override surface still works.

The marginal reliability gain from a dedicated retry isn't worth the architectural complexity given how rarely fabrications fire (the Bradford smoke runs we did had zero). Keep the floor (override + audit) and rely on the existing suggesters for ceiling (auto-resolution). Real-run data showing fabrications are common enough to warrant a dedicated retry can revisit.

## Schema additions

`Provenance.review_status` (in `nl2protocol/models/spec.py`) gains `"user_overrode_fabrication"`.

`Resolution.action` (in `nl2protocol/gap_resolution/types.py`) gains `"override"`.

`Resolution.user_action_provenance` gains `"user_overrode_fabrication"`.

`Provenance.require_appropriate_field_for_source` validator stays unchanged — overriding a fabrication doesn't change `source` (it stays `instruction` with the original fabricated `cited_text`); only `review_status` updates. The `reviewer_objection` field stays None for overrides — that field is reserved for the IndependentReviewSuggester's verdicts on inferred reasoning, not for the deterministic verifier's fabrication flags.

## Apply path

`default_apply_resolution` (in `nl2protocol/gap_resolution/orchestrator.py`) gains an early-return branch for `action="override"`:
- Locate the field at the gap's path
- Stamp `_stamp_user_action(field_obj, "user_overrode_fabrication")` (which re-validates the Provenance with the new review_status)
- Skip the value-write branches entirely (the existing fabricated value stays)

The branch fires before the path-shape regex matches, so it's order-of-operations independent.

## CLI handler

`CLIConfirmationHandler._action_prompt` now takes the `gap` and conditionally adds `, [o]verride (keep current value)` to the prompt text when `gap.kind == "fabricated"`. `_interpret_response` recognizes `o` / `override` and constructs `Resolution(action="override", new_value=None, user_action_provenance="user_overrode_fabrication")`. Defensive: if the user types `o` on a non-fabrication gap (option shouldn't have been presented), treat as skip.

## Alternatives considered

**(A) Block fabrications hard.** Pre-PR3b's behavior. Rejected because the verifier-missed-a-synonym class of cases (rare but real) deserves an affordance, not a re-run loop.

**(B) Allow override but don't audit it.** Pre-PR3b's "Proceed anyway? [y/N]" affordance. Rejected because the override silently produced specs that read as if the LLM did the right thing — the audit trail had no record of the override, so reviewers couldn't tell which values had been forced through.

**(C) Build `FabricationRetrySuggester` (Option A above).** Rejected for now in favor of simpler scope. Easy to revisit if real-run data shows fabrications need focused retry handling.

**(D) Have the user write a new cite as override input.** Force the user to provide a substring of their instruction that the verifier accepts. Rejected because the (synonym-recovery) case is exactly where the user's wording uses words the verifier doesn't recognize as matching. Forcing them to coin a verifier-acceptable phrasing means rewriting the instruction, which defeats the purpose.

## Tradeoffs

**No focused retry means LLMSpot is the only LLM-side defense.** LLMSpot's generic prompt isn't fabrication-aware in the same way a dedicated retry would be. Some fabrications LLMSpot would have missed will fall through to override. Acceptable because override is a working safety net.

**Override is reversible only by re-running.** Once the user overrides, the spec carries the override stamp and proceeds to codegen. If they realize the override was wrong later, they re-run with a corrected instruction. This matches the rest of the pipeline's "spec is committed at run end" model.

**The override option's rarity means most users will never see it.** That's the design intent — it's a safety valve, not a mainline path. The audit trail makes it visible to readers of the report when it does fire.

## References

- ADR-0008 — unified gap-resolution architecture (the loop this extends)
- ADR-0009 — Provenance schema expansion (review_status state machine; this ADR adds one new state)
- ADR-0011 — HTML visualization (Phase 6 was reserved here for this orchestrator change; render polish for the override stamp lives there)
- `nl2protocol/models/spec.py` — `Provenance.review_status` Literal extension
- `nl2protocol/gap_resolution/types.py` — `ResolutionAction` + `Resolution.user_action_provenance` Literal extensions
- `nl2protocol/gap_resolution/orchestrator.py` — `default_apply_resolution` override branch
- `nl2protocol/gap_resolution/handlers.py` — `CLIConfirmationHandler` `[o]verride` prompt option
