# ADR-0008: Unified gap-resolution interface

**Status:** Proposed
**Date:** 2026-05-04

## Context

Today's post-extraction pipeline routes different kinds of gaps through different ad-hoc paths:

| Gap kind | Detector | Resolver | User input? |
|---|---|---|---|
| Missing transfer volume / source / destination | `missing_fields` | `fill_lookup_and_carryover_gaps` (deterministic) → `refine` (LLM regenerates whole spec) → abort | None — resolver-only |
| `wait_for_temperature` missing temperature | (same) | `fill_lookup_and_carryover_gaps` carryover branch | None |
| Substance → source location | (same) | `fill_lookup_and_carryover_gaps` lookup branch | None |
| `initial_contents.volume_ul` null | `_build_initial_volume_queue` | `_confirm_provenance_items` with custom prompt | "1) Enter / 2) Accept default / 3) Exit" |
| Fabricated cite | `_verify_claimed_instruction_provenance` | None — hard block | None |
| Low-confidence inferred / domain_default | `_flag_uncertain_claims` | `_confirm_provenance_items` | "Enter/edit/skip-all/quit" |
| Ambiguous labware → config_label | `LabwareResolver` | `_confirm_labware_assignments` | "Pick 1-N" |
| Constraint violations (well doesn't exist, capacity exceeded) | `ConstraintChecker` | None — block in non-interactive, "Proceed anyway? y/N" interactive | binary y/N |

Six detectors. Three confirmation paths with different UX. Two automatic-fix paths (deterministic + LLM refine) with no user visibility. One detector that always blocks. The result is an architecture that:

- Treats functionally-similar problems differently based on the order they were implemented
- Bypasses user input for some gap kinds and demands it for others, with no principled threshold
- Silently regenerates the whole spec via `refine` when missing-fields persist (see ADR-0008 retrospective: that path can also overwrite previously-correct values, has no provenance marker for what changed, and aborts on token-truncation parse errors with a non-actionable message)
- Cannot suggest fixes for constraint violations even when the fix is mechanical (e.g. "wells A7-A8 don't exist on a 4×6 rack — clip to A1-A6 + B1-B2?")

A user reading the pipeline cannot predict which kind of intervention any given gap will receive without tracing the code. New gap kinds get added by extending whichever path is closest, propagating the asymmetry.

## Decision

Replace the ad-hoc paths with one uniform pipeline. Every gap, regardless of kind or detection point, flows through the same five-stage loop: **detect → suggest → review → resolve → re-detect**. The CLI confirmation prompt and a future HTML confirmation UI become pluggable presenters of the same `Gap` and `Suggestion` objects.

### Five components

**1. `Gap` dataclass.** Typed, structured representation of any deficiency:

```python
@dataclass
class Gap:
    id: str                          # stable handle, e.g. "step3.source"
    step_order: Optional[int]
    field_path: str                  # dotted: "steps[3].source", "initial_contents[0].volume_ul"
    kind: GapKind                    # missing | fabricated | low_confidence | ambiguous |
                                     # constraint_violation | unverifiable
    current_value: Any
    description: str                 # human-readable
    severity: Literal["blocker", "suggestion", "quality"]
```

**2. `GapDetector` protocol.** Each existing detector becomes one implementation. They REPORT only — never mutate.

```python
class GapDetector(Protocol):
    def detect(self, spec: ProtocolSpec, context: dict) -> List[Gap]: ...
```

Migrations:
- `missing_fields` → `MissingFieldsDetector` (action-required completeness from ADR-0006)
- `_verify_claimed_instruction_provenance` → `FabricationDetector`
- `_verify_config_claims` → `ConfigCiteFabricationDetector`
- `_flag_uncertain_claims` → `ConfidenceThresholdDetector`
- `ConstraintChecker.check_all` → `ConstraintViolationDetector`
- `_build_initial_volume_queue` → `InitialContentsVolumeDetector`
- `LabwareResolver` ambiguity → `LabwareAmbiguityDetector`

New detectors are added by registering one more class. No orchestrator changes.

**3. `Suggester` protocol.** Given a Gap, propose a value:

```python
class Suggester(Protocol):
    def suggest(self, gap: Gap, spec: ProtocolSpec, context: dict) -> Optional[Suggestion]: ...

@dataclass
class Suggestion:
    value: Any
    provenance_source: Literal["inferred", "domain_default", "deterministic"]
    reasoning: str                   # positive reasoning — why this value works
    why_not_in_instruction: Optional[str]  # negative reasoning (per ADR-0009)
    confidence: float
```

Suggester registry, in fixed precedence order (cheapest-and-most-certain first):

```
1. DETERMINISTIC SUGGESTERS         (correct-or-empty, no LLM call)
   - ConfigLookupSuggester          substance → source via config.labware.contents
   - CarryoverSuggester             wait_for_temperature → prior set_temperature
   - WellCapacitySuggester          initial_contents.volume_ul → labware well capacity
   - WellRangeClipSuggester         constraint violation → clip out-of-range wells
   - RegexFromNoteSuggester         pause/comment notes → extract embedded values

2. LLM SPOT SUGGESTERS              (one Gap per call, focused context)
   - LLMSpotSuggester               asks one question with: gap description,
                                    relevant instruction snippet (not full text),
                                    relevant config slice, neighboring steps
                                    Returns Suggestion with positive + negative reasoning.

3. LAST-RESORT FALLBACK             (only when explicitly requested)
   - LLMFullRefineSuggester         today's `refine`, demoted to opt-in:
                                    triggers only when (1) and (2) fail and the
                                    user opts in via prompt. Spot-replacement,
                                    not silent overwrite.
```

Multiple suggesters can attempt one Gap. They're tried in registry order; the first to return a non-None Suggestion wins. Their confidence drives whether the suggestion auto-applies or requires user review (see Resolution rules below).

**4. `IndependentReviewSuggester` (post-suggest stage).** Run BEFORE user confirmation. Batches every `source="inferred"` or `source="domain_default"` provenance into one reviewer LLM call. Per ADR-0009, the structured output:

```python
@dataclass
class ReviewResult:
    field_path: str
    confirms_positive: bool          # is the positive reasoning sound?
    confirms_negative: bool          # is the negative reasoning correct? (i.e. instruction
                                     # really doesn't supply this value?)
    objection: Optional[str]         # specific text only when either is False
```

The reviewer prompt frames itself as a skeptical auditor and tests two independently-falsifiable claims, not a single fused vibe-check. Routing:
- both `confirms_*` → upgrade to `review_status="reviewed_agree"`, eligible for auto-accept
- either disconfirmed → `review_status="reviewed_disagree"`, escalate to user with the reviewer's `objection` text surfaced

Cost: one batched call per pipeline run, structured output ~2K tokens for a 14-step spec.
Bias mitigation: prompt explicitly frames the reviewer as a skeptical auditor; if budget allows, use a different model (Haiku reviewing Opus or vice versa) — the same-model-self-review agreement-bias is real.

**5. `ConfirmationHandler` protocol.** Pluggable presentation:

```python
class ConfirmationHandler(Protocol):
    def present(self, gap: Gap, suggestion: Optional[Suggestion]) -> Resolution: ...

@dataclass
class Resolution:
    action: Literal["accept_suggestion", "edit", "skip", "abort"]
    new_value: Any
    user_action_provenance: Literal["user_confirmed", "user_edited",
                                     "user_accepted_suggestion", "user_skipped"]
```

Implementations:
- `CLIConfirmationHandler` — collapses today's three CLI prompt paths into one. Shows: gap description, current value, suggestion + reasoning (when available), action set.
- `HTMLConfirmationHandler` (deferred follow-up) — renders Gap+Suggestion to the existing report DOM and accepts resolutions via a bridge mechanism (writes to a side-channel JSON file, polled by the running CLI; or full WebSocket when warranted). Same data flows through both — same per-citation hue, same ▴ marker, same justification block.

Both handlers consume the same `Gap` and `Suggestion` types. Adding the HTML handler requires no changes to detectors, suggesters, or the orchestrator.

### The resolution loop

```
LOOP (max N iterations, default N=3):

  DETECT
    Run every registered GapDetector against spec, collect all Gaps.

  SUGGEST
    For each Gap:
      Run suggesters in registry order until one returns a Suggestion
      (or none do).

  REVIEW (only for source="inferred" or "domain_default" suggestions)
    Batch into one reviewer call. Apply review_status to each.

  RANK
    Sort gaps: blocker → suggestion → quality.
    Within each tier: most-impactful first (e.g. fabrications before
    low-confidence inferences).

  CLASSIFY for routing
    For each Gap:
      auto-accept ← (Suggestion exists)
                    AND (suggestion.confidence >= 0.85)
                    AND (gap.kind not in ALWAYS_CONFIRM)
                    AND (review_status != "reviewed_disagree")
      else        ← present to user via ConfirmationHandler

    ALWAYS_CONFIRM = {fabricated, ambiguous, constraint_violation,
                      reviewed_disagree}

  APPLY
    Auto-accept set: write Suggestion.value into spec, attach Provenance
                     with source = suggestion.provenance_source,
                     review_status as set by the reviewer (or "original"
                     if no review),
                     reasoning = suggestion.reasoning,
                     why_not_in_instruction = suggestion.why_not_in_instruction.
    User-confirmed set: write Resolution.new_value into spec, attach
                     Provenance with user_action_provenance from Resolution.

  RE-DETECT
    Re-run all detectors. New Gaps surfaced (e.g. user's edit introduced a
    constraint violation) loop back through the same flow.
    Termination:
      - no Gaps remain → promote to CompleteProtocolSpec (ADR-0006 strict)
      - same Gap survives N iterations without progress → escalate to user
        with whatever partial information the suggesters produced
      - user aborts at any confirmation → pipeline halt, save state log
```

### Order rationale

Five principles drive this ordering.

**Principle 1: Detect everything before resolving anything.** Today's pipeline interleaves detect/fix/detect, so the user might confirm 5 items before discovering item 6 is a hard blocker that aborts the run. Under the new flow the user sees the FULL picture in one batch — they can scan, prioritize, and abort early if it's hopeless.

**Principle 2: Cheapest, most-certain suggesters first.** Deterministic sources (config lookup, capacity defaults, action-semantics carryover) are correct-or-empty. LLM sources are plausible-but-fallible and cost API calls. Try the cheap-and-certain first; only escalate when they fail. This also keeps cost predictable: most gaps get resolved without an LLM call.

**Principle 3: Spot-suggestion before full-refine.** Today's `refine` regenerates the whole spec, risking collateral changes to fields that were correct on the first pass and triggering token-truncation parse errors on long specs. A spot-suggester (LLM asked "what's the source for water in step 3, given config has reagent_reservoir A2: water?") affects ONE field, with bounded output, can't regress others, and tags provenance honestly. Full-refine becomes opt-in for the rare case where spot-suggesters fail.

**Principle 4: Independent review BEFORE user confirmation.** The reviewer pass is structural: it tests positive AND negative reasoning as two falsifiable claims, not as a vibe-check. When the reviewer disagrees with either, the user sees the SPECIFIC objection ("the instruction does mention 'incubate at room temperature' on line 5 — your negative reasoning was wrong"). This gives the user concrete evidence to act on instead of having to verify the LLM's claim from scratch.

**Principle 5: Re-detect after a full batch resolution, not per-Gap.** Some resolutions invalidate other detections — user fills `initial_contents.volume_ul`, satisfying a downstream `volume_below_capacity` constraint warning. Re-detecting catches this. But re-detecting after EACH user pick would shift the queue mid-walk (items disappear, new ones appear), which is disorienting. Batch the re-detection; show any newly-surfaced Gaps as a clearly-labeled second pass.

**Bounded loop:** if the user's edit introduces a new Gap, which their next edit fixes but introduces a third, we could loop forever. Cap at 3 iterations; on overflow, surface "couldn't reach a clean spec — review remaining issues and accept-as-is or abort."

### Gap dependencies — batched resolution with topological ordering

Most gaps are independent: fabrication detection on step 3 doesn't affect step 7; low-confidence inferred volume on step 5 has no bearing on step 6's substance. For the independent majority, the SUGGEST stage processes them in any order and the user sees them as one batch.

A bounded set of gap kinds genuinely depend on each other:

- **Carryover chain.** `wait_for_temperature.temperature` reads the prior `set_temperature.temperature`. If both are gaps, the dependent (wait) cannot be suggested until the upstream (set) resolves.
- **Labware-before-constraint.** Constraint detection (well-out-of-range, capacity-exceeded) reads `LocationRef.resolved_label`. If labware resolution is itself a gap, the constraint check uses whatever current resolved_label is or emits conditionally — neither is correct until labware is fixed.
- **Substance-before-source.** A user edit of `substance` invalidates the suggested `source` (since source was looked up by substance name).

These dependencies are handled by **two mechanisms working together**:

1. **Topological-sorted SUGGEST stage.** Within each iteration, the orchestrator builds a dependency graph over the current Gap set and processes suggesters in topological order. set_temperature before wait_for_temperature; labware resolution before constraint checks; substance before source. This maximizes the work done in iteration 1: dependent gaps see upstream values when their suggester runs, so they get a meaningful suggestion the user can accept in the same batch.

2. **Re-detect loop catches what topological ordering missed.** Cases where the dependency only fires AFTER user resolution (e.g. user EDITED set_temperature in iteration 1, which changes what wait_for_temperature should inherit) surface in iteration 2 as a freshly-detected gap with a now-correct suggestion.

The combination keeps the architecture batch-by-default (which is correct for the independent majority and cheaper UX) while preserving correctness for the dependent minority (which the re-detect loop covers regardless).

**Optional sequential mode.** A flag (`--sequential-confirmation`) reduces batch size to 1 between resolutions: detect, suggest, present one gap, resolve, re-detect, repeat. Slower and more typing, but no batched-blind-spot — the user sees each gap with full information from all prior resolutions. Default off; on for users who want maximum visibility into cascading edits.

**Grouped presentation as a presenter concern.** The orchestrator emits per-Gap items into the resolution queue. The `ConfirmationHandler` is free to group visually — "steps 1 + 2 are linked via temperature carryover: pick 4°C for both, or set them separately." CLI handler can use simple per-Gap prompts; HTML handler can render richer grouped UI. Same data flows through both; presentation differences don't propagate into the architecture.

## Worked example: Bradford instruction under this architecture

Initial state from Stage 2 extraction: 14 steps, instruction underspecifies water source.

**Iteration 1:**

DETECT collects:
- Blocker × 5: `step{3,5,7,9,10}.source` missing for water transfers
- Blocker × 1: `step13.volume` missing
- Blocker × 2: constraint violation `wells A7,A8 not on sample_rack`
- Quality × N: any low-confidence atoms

SUGGEST runs per gap:
- Water source: `ConfigLookupSuggester` finds `reagent_reservoir.A2: water` → Suggestion(value=A2, source="inferred", reasoning="Config labware reagent_reservoir lists 'water' at A2", why_not_in_instruction="Setup section names labware contents only for samples, BSA stock, and Bradford reagent — water source not stated", confidence=0.9)
- step13.volume: `WellCapacitySuggester` proposes 100uL based on prior mix step volume.
- A7,A8 constraint: `WellRangeClipSuggester` proposes "clip to A1-A6 + B1-B2."

REVIEW batches all source="inferred" suggestions; reviewer agrees with all (positive reasoning sound, negative reasoning matches instruction text). All marked `reviewed_agree`.

RANK + CLASSIFY: all suggestions confidence ≥ 0.85, none in ALWAYS_CONFIRM, all `reviewed_agree` → ALL auto-accept.

APPLY: spec updated with deterministic-suggester provenance + reviewed_agree status.

RE-DETECT: clean. Promote to CompleteProtocolSpec.

User saw: zero confirmation prompts. Report shows ▴ markers on the auto-resolved values; tooltip surfaces both reasoning lines + "reviewed_agree" status. User can scan if they want.

**Variant — reviewer objects to one suggestion:**

If `IndependentReviewSuggester` finds the negative reasoning wrong on step 7 ("the instruction says '5uL water (1000 ug/mL)' which DOES name water as the diluent — your negative reasoning that water source isn't named is incorrect"), step 7 gets `review_status="reviewed_disagree"` and `reviewer_objection="..."`. It moves to ALWAYS_CONFIRM. User sees one prompt:

```
Step 7 source = water
  Suggested: reagent_reservoir.A2 (config lookup)
  Reviewer objection: instruction line "5uL water (1000 ug/mL)" mentions
                      water but doesn't name a source. The reviewer flagged
                      that your "not in instruction" reasoning was overstated
                      — water IS mentioned, just not assigned a labware.
                      The config-lookup suggestion still seems right but
                      worth your review.
  Action: [a]ccept suggestion, [e]dit, [s]kip, [q]uit:
```

User makes an informed decision. The reviewer surfaced a concrete textual claim, not a vague disagreement.

## Consequences

**Positive:**

- One mental model for every gap. New contributors don't re-discover the asymmetric handling.
- Provenance is honest and complete. Every value carries: where the value came from (suggester source), what the model thought (reasoning + why-not-in-instruction), whether it survived independent review (review_status), and what the user did (user_action_provenance from Resolution). Together these answer "where did this number come from, and who agreed to it?"
- New gap kinds register one detector + one suggester. No orchestrator surgery.
- HTML confirmation UI becomes a separate `ConfirmationHandler` implementation — no detector or suggester changes.
- `refine`'s silent overwrite + parse-error fragility goes away. Full-refine is opt-in last-resort.
- Constraint violations get a fix-suggest path (e.g. well-range clipping) for the first time.

**Negative / costs:**

- Substantial refactor. ~7 detectors + ~6 suggesters + 1 reviewer + 1 orchestrator + 1 CLI handler = real implementation work, plus comprehensive tests. Probably 2-3 ADR-sized PRs to land safely.
- Adds latency: one batched reviewer call per loop iteration. Acceptable (~3-5s) for the safety it provides.
- Reviewer agreement-bias risk. Mitigations specified above (skeptical-auditor framing, optional cross-model review). Worth empirical validation: take 20 known-bad inferred values, verify the reviewer catches ≥70%. If it doesn't, switch reviewer model.
- Schema migration for Provenance (ADR-0009) means existing pipeline_state.json files become legacy data. Backwards-compat handler can read old single-`reasoning` field and treat it as `positive_reasoning` with `why_not_in_instruction=None`.
- The `confidence ≥ 0.85` auto-accept threshold + `ALWAYS_CONFIRM` list are hardcoded knobs. Future ADR may want to make them per-deployment configurable.

## Migration plan

Land in three PRs:

1. **Skeleton + detectors.** Define `Gap`, `GapDetector`, `Suggester`, `Suggestion`, `Resolution`, `ConfirmationHandler` types. Implement detectors as adapters around existing functions (no behavior change). Add registry. New code unused; existing pipeline untouched.
2. **Suggesters + orchestrator.** Implement deterministic suggesters (mostly extracting from `fill_lookup_and_carryover_gaps` + `_build_initial_volume_queue`). Implement `LLMSpotSuggester`. Implement `IndependentReviewSuggester`. Implement orchestrator loop. Wire `CLIConfirmationHandler` as adapter around existing prompt code. Behind a feature flag so the new path runs in parallel with the old for validation.
3. **Cutover + cleanup.** Remove old ad-hoc paths. Make new path the default. Delete `extractor.refine` (or convert to opt-in `LLMFullRefineSuggester`). Update tests. Drop the feature flag.

## Alternatives considered

**(A) Patch the existing paths individually.** Add a "missing source" user prompt; add a "constraint violation suggest-fix" path; add a reviewer pass over `_flag_uncertain_claims` outputs. Each fix is small. Rejected because the asymmetry persists — every new gap kind adds one more special case, the architecture stays organic-grown. ADR-0008's whole point is naming the pattern so contributors don't keep adding to it.

**(B) Keep ad-hoc paths but unify the user-prompt UI only.** Single `ConfirmationHandler` that all three current prompt paths route through. Helps with the future HTML UI. Rejected because the gap-detection + suggestion logic stays asymmetric, and the HTML UI gets the gnarly union of three different gap shapes to render.

**(C) Smaller scope: only unify gap-detection.** Implement the `Gap` type and detectors but keep current resolution paths. Rejected because the value comes from the loop — uniform suggestion + review + confirmation. Detectors alone don't help.

## Deferred / future work

- **HTML confirmation UI** as `HTMLConfirmationHandler`. Bridge mechanism (file polling vs WebSocket) is itself an ADR-worth question.
- **Cross-model reviewer.** Empirical validation that same-model review catches errors; if not, route reviewer call to a different model. Ties into eval infrastructure.
- **Per-deployment thresholds.** `confidence ≥ 0.85` and `ALWAYS_CONFIRM` list as configuration, not constants.
- **Suggestion confidence calibration.** If suggesters consistently over- or under-estimate confidence, calibrate against held-out evaluation data. Out of scope for the architecture ADR; live concern once the new path is the default.
- **Loop bound tuning.** N=3 is a guess. Empirical data from real runs may justify higher or lower.

## References

- ADR-0006 — CompleteProtocolSpec strict completeness (the validator that catches what this resolution loop should fix)
- ADR-0007 — field-level vs validator-level enforcement (defines where invariants live; this ADR defines how they get satisfied)
- ADR-0009 — Provenance schema expansion (positive/negative reasoning, review_status, user_action — the data model this resolution loop produces)
- `nl2protocol/pipeline.py:917-1153` — current post-extraction stages (to be replaced by the resolver loop)
- `nl2protocol/extraction/extractor.py:250-302` — current `refine` implementation (to be demoted to opt-in last-resort)
- `nl2protocol/extraction/extractor.py:441-676` — current detectors (to become `GapDetector` implementations)
