# ADR-0009: Provenance schema expansion — positive/negative reasoning, review status, user action

**Status:** Proposed
**Date:** 2026-05-04

## Context

Today's `Provenance` schema (per ADR-0005) carries:

```python
class Provenance:
    source: Literal["instruction", "domain_default", "inferred"]
    cited_text: Optional[str]    # required when source="instruction", null otherwise
    reasoning: Optional[str]     # required when source != "instruction", null otherwise
    confidence: float            # 0.0–1.0
```

This is enough for "where did this value come from?" at the level of *which kind of source* and *one fused justification string*. It is NOT enough for three things the unified gap-resolution architecture (ADR-0008) needs:

1. **Auditing the negative claim.** When a value is `inferred`, today's `reasoning` field bundles two distinct claims into one paragraph: *why this value works* AND *why the instruction couldn't supply it*. A reviewer (LLM or human) cannot evaluate them independently. The negative claim — "the instruction doesn't supply this" — is the one users currently can't audit without re-reading the entire instruction.
2. **Recording independent review.** ADR-0008 introduces an `IndependentReviewSuggester` stage that batches every inferred / domain_default provenance through a reviewer LLM. Today's schema has no field to record whether a value was reviewed, whether the reviewer agreed, or what specifically the reviewer objected to.
3. **Recording user interaction.** When a user accepts a suggestion, edits it, or skips review, today's schema can't capture that. From the report's perspective, an LLM-resolved value the user never saw is indistinguishable from a value the user explicitly accepted.

The downstream consequence: the provenance report renders every non-instruction value with a single `▴` marker and one tooltip, with no way to differentiate "the model inferred this and you didn't see it" from "the model inferred this, an independent reviewer agreed, and you accepted the suggestion." Those are very different epistemic states, and a portfolio-grade interpretability tool should distinguish them.

## Decision

Expand `Provenance` to carry the three missing dimensions. Field-by-field rationale below; full schema at the end.

### Replace `reasoning` with `positive_reasoning` + `why_not_in_instruction`

The two claims become two fields:

- `positive_reasoning: str` — *why this value is the right answer.* "5-minute incubation per Bio-Rad Bradford protocol." Tests against domain knowledge.
- `why_not_in_instruction: str` — *why the instruction couldn't supply it.* "The instruction asks for 'a Bradford' but never specifies the incubation duration." Tests against the instruction text.

Both are independently falsifiable. The reviewer can check `positive_reasoning` for domain plausibility and `why_not_in_instruction` for textual accuracy, returning a verdict on each. The user can see both claims separately in the report tooltip and understand exactly which one (if any) is shaky.

Required iff `source != "instruction"` (matching the current rule for `reasoning`). Mutually-exclusive validator updated:

- `source == "instruction"` → both null, `cited_text` populated.
- `source != "instruction"` → both populated, `cited_text` null.

### Add `review_status`

Records what the independent-review pass concluded:

```python
review_status: Literal["original", "reviewed_agree", "reviewed_disagree",
                       "user_confirmed", "user_edited", "user_accepted_suggestion",
                       "user_skipped"]
```

Defaults to `"original"` (no review run). Set by `IndependentReviewSuggester` to `"reviewed_agree"` or `"reviewed_disagree"`. Set by the `ConfirmationHandler` to one of the user-action values when the user interacts with the gap.

State diagram:

```
                "original"  (just produced by extraction or a suggester)
                    │
                    ▼
              [review pass]
                ↙       ↘
   "reviewed_agree"   "reviewed_disagree"  ←─── reviewer_objection populated
                    │           │
                    │           ▼
                    │    [escalates to user]
                    │           │
                    └──────┬────┘
                           ▼
                   [user interacts]
                  ↙    ↓     ↓     ↘
   "user_     "user_ "user_  "user_
   confirmed" edited" accepted_ skipped"
                       suggestion"
```

`"user_confirmed"` = user explicitly accepted the existing value (no edit).
`"user_edited"` = user typed a replacement.
`"user_accepted_suggestion"` = user accepted the suggester's proposal.
`"user_skipped"` = user chose to leave the gap unresolved (only valid for `severity="quality"` gaps).

The state diagram is enforced by a `model_validator`: e.g. `"user_*"` only allowed if reached via review or directly via confirmation; `"reviewed_disagree"` requires `reviewer_objection` to be set.

### Add `reviewer_objection`

Set only when `review_status == "reviewed_disagree"`. Records the reviewer's specific objection text — what claim it disagreed with and why. Surfaced to the user in the confirmation prompt so they have concrete evidence to act on.

```python
reviewer_objection: Optional[str]   # set iff review_status == "reviewed_disagree"
```

Validator: required if `review_status == "reviewed_disagree"`, must be null otherwise.

### Full updated schema

```python
class Provenance(BaseModel):
    source: Literal["instruction", "domain_default", "inferred"]

    # Mutually exclusive by source (existing rule, retained):
    cited_text: Optional[str]                # iff source == "instruction"

    # New: split former `reasoning` into two falsifiable claims
    positive_reasoning: Optional[str]        # iff source != "instruction"
    why_not_in_instruction: Optional[str]    # iff source != "instruction"

    confidence: float

    # New: review and user-interaction state
    review_status: ReviewStatus = "original"
    reviewer_objection: Optional[str] = None  # iff review_status == "reviewed_disagree"

    @model_validator(mode='after')
    def validate_field_combinations(self):
        # Source ↔ cite/reasoning mutual exclusion
        # review_status ↔ reviewer_objection
        # confidence ∈ [0, 1]
        ...
```

### Backwards compatibility

Existing `pipeline_state.json` files have the old single-`reasoning` field. To avoid breaking eval infrastructure that loads them:

- Add a `@field_validator(mode="before")` on `Provenance` that detects the old shape (single `reasoning` field present, new fields absent) and migrates: `positive_reasoning = reasoning`, `why_not_in_instruction = None`, `review_status = "original"`.
- Mark old single-`reasoning` reads as deprecated in a CHANGELOG entry. Plan removal in a future ADR after the full pipeline cycles through the new schema.
- Existing tests that construct `Provenance` with `reasoning=...` continue to work via the migration validator.

## Consequences

**Positive:**

- Reviewer LLM has two falsifiable claims to test independently. Reduces single-claim agreement bias.
- User sees concrete evidence (positive reasoning, negative reasoning, optional reviewer objection) when asked to confirm — not just "the model said so."
- Report can render epistemic states distinctly: "model inferred, you didn't review" vs "model inferred, reviewer agreed, you accepted" vs "model inferred, reviewer disagreed, you overrode." Today these look identical.
- Audit trail: any value's provenance now answers "where did this come from, what did the reviewer think, and what did the user do?"

**Negative / costs:**

- Every inferred / domain_default value now requires the LLM to articulate two reasoning claims instead of one. Prompt changes; LLM output longer; risk that the LLM produces vague boilerplate for the negative claim if the prompt doesn't demand specifics.
- Schema migration (backwards-compat validator) is non-trivial — needs tests against old fixtures.
- `review_status` state machine is a new invariant the validators must enforce. Misuse (e.g. setting `review_status="user_confirmed"` directly without going through review) silently corrupts the audit trail unless validators catch it.
- Report rendering needs updating to show the new fields (positive vs negative tooltip rows; review_status badge on the ▴ marker).
- Test fixtures across the codebase need updating to provide the new fields. Migration validator covers most cases, but new tests should construct with the new shape.

## Implementation notes

**Prompt changes for extraction.** The system prompt in `nl2protocol/extraction/prompts.py` adds: when the source is not `instruction`, populate BOTH `positive_reasoning` (why this value) AND `why_not_in_instruction` (the specific missing element you would have looked for). Anti-pattern example: `why_not_in_instruction = "not specified"` (too vague — must name what you would have expected to find and what the instruction does say instead).

**Prompt for IndependentReviewSuggester.** Reviewer is given the value, the spec context, the instruction, and asked: (a) does `positive_reasoning` actually justify the value given domain norms? (b) is `why_not_in_instruction` correct — does the instruction really not supply this value? Returns structured `ReviewResult` per ADR-0008. Frame as skeptical auditor; consider cross-model review (Haiku reviewing Opus or vice versa) to reduce same-model agreement bias.

**Report rendering changes** (in `nl2protocol/reporting.py` + template):
- Tooltip for non-instruction values shows two labeled rows: "WHY THIS VALUE: …" and "WHY NOT FROM INSTRUCTION: …"
- ▴ marker variant per `review_status`:
  - `original` — plain ▴
  - `reviewed_agree` — ▴ with subtle checkmark overlay or different color
  - `reviewed_disagree` — ▴ with warning indicator + reviewer_objection in tooltip
  - `user_*` — ▴ with user-action badge
- Header stat: "N atomic values · M% non-instruction · K% reviewer-confirmed · J% user-confirmed"

**Order of implementation:**
1. Schema change with backwards-compat validator. Run full test suite to confirm no regressions on old-shape data.
2. Extractor prompt updates to produce new fields.
3. ADR-0008's `IndependentReviewSuggester` consumes new fields.
4. Report rendering surfaces them.

## Alternatives considered

**(A) Keep single `reasoning` field, add review/user metadata only.** Cheaper change. Rejected because the reviewer needs structured claims to test — without the positive/negative split, review is rubber-stamp-prone.

**(B) Add separate `Review` and `UserAction` objects rather than fields on `Provenance`.** More normalized. Rejected because every read site (renderer, reviewer, eval, extractor) would have to traverse a join. Provenance is read frequently; flatter wins for ergonomics.

**(C) Use a single freeform `metadata: dict` for all the new fields.** Most flexible. Rejected because the value is in the typed validators — `review_status` enum, `reviewer_objection` required-when-disagree, etc. A dict abandons type safety.

## References

- ADR-0005 — original Provenance schema with single `reasoning` field
- ADR-0007 — field-level vs validator-level enforcement (this ADR's new fields use both)
- ADR-0008 — Unified gap-resolution interface (consumes the schema this ADR defines)
- `nl2protocol/models/spec.py:Provenance` — schema to be modified
- `nl2protocol/extraction/prompts.py` — extractor prompt to be updated
- `nl2protocol/reporting.py` + template — report rendering to be extended
