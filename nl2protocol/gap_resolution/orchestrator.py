"""
Orchestrator — the resolver loop (ADR-0008).

Runs DETECT → topological-sort → SUGGEST → REVIEW → CLASSIFY → PRESENT →
APPLY → RE-DETECT, up to N=3 iterations. Replaces the existing ad-hoc
post-extraction logic in pipeline.py when the --use-gap-resolver flag is
set.

Per ADR-0008:
  - Suggesters tried in registry-defined precedence order; first non-None wins.
  - Reviewer batches all source="inferred"/"domain_default" suggestions.
  - Auto-accept iff (Suggestion exists)
                AND (suggestion.confidence >= 0.85)
                AND (gap.kind not in ALWAYS_CONFIRM)
                AND (review_status != "reviewed_disagree").
  - Topological-sorted SUGGEST so dependent gaps see upstream values
    within the same iteration (set_temp before wait_for_temp; labware
    before constraints; substance before source).
  - Re-detect after batch resolution; loop until clean or N reached.
  - Bounded loop terminates on convergence, abort, or iteration cap.

The orchestrator never reaches into the spec directly except to APPLY
resolutions. All detection lives in detectors; all suggestion lives in
suggesters; all UI lives in the ConfirmationHandler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from nl2protocol.gap_resolution.protocols import (
    ConfirmationHandler,
    GapDetector,
    Suggester,
)
from nl2protocol.gap_resolution.types import (
    Gap,
    Resolution,
    ReviewResult,
    Suggestion,
)


# ============================================================================
# Defaults from ADR-0008
# ============================================================================

DEFAULT_AUTO_ACCEPT_THRESHOLD = 0.85
DEFAULT_MAX_ITERATIONS = 3

# Gap kinds that ALWAYS go to user confirmation regardless of suggestion
# confidence — per ADR-0008.
ALWAYS_CONFIRM_KINDS = {
    "fabricated",
    "ambiguous",
    "constraint_violation",
}


# ============================================================================
# Topological ordering for SUGGEST
# ============================================================================

# Field-suffix → priority. Lower priority runs first (so dependent gaps see
# upstream resolutions). Per ADR-0008's dependency analysis:
#   set_temperature.temperature (upstream of carryover)
#   labware-resolution gaps     (upstream of constraints + capacity)
#   substance                   (upstream of source lookup)
#   ...everything else
_FIELD_PRIORITY = [
    (".temperature", 0),    # set_temperature outputs first
    (".substance", 0),      # substance enables source lookup
    (".source", 1),         # source needs substance to be resolved
    (".destination", 1),
    (".duration", 2),
    (".volume", 2),
    (".note", 2),
    ("initial_contents", 3),
]


def topo_sort_gaps(gaps: List[Gap]) -> List[Gap]:
    """Stable-sort gaps so suggesters for upstream fields run first.

    Within each priority bucket, original detector order is preserved
    (Python's sort is stable).
    """
    def priority(g: Gap) -> int:
        for suffix, p in _FIELD_PRIORITY:
            if g.field_path.endswith(suffix) or suffix in g.field_path:
                return p
        return 9  # everything else last

    return sorted(gaps, key=priority)


# ============================================================================
# Result records — for state log + downstream observability
# ============================================================================

@dataclass
class GapResolutionRecord:
    """One Gap's full lifecycle within an iteration: what was detected,
    what was suggested, what the reviewer said, how it was resolved.

    Used by the pipeline state log for audit. Not consumed by the
    orchestrator's logic — purely observational.
    """

    gap: Gap
    suggestion: Optional[Suggestion]
    review: Optional[ReviewResult]
    resolution: Optional[Resolution]   # None if still unresolved at iteration end
    auto_accepted: bool


@dataclass
class IterationResult:
    iteration: int
    records: List[GapResolutionRecord] = field(default_factory=list)
    aborted: bool = False


@dataclass
class OrchestratorOutcome:
    """The orchestrator's final disposition.

    `aborted` true means the user (or an internal rule) halted the loop;
    the spec may be partially resolved. `iterations` is always populated
    with at least one entry.
    """

    spec: Any
    iterations: List[IterationResult]
    aborted: bool
    converged: bool                    # True iff final iteration produced 0 gaps


# ============================================================================
# Orchestrator
# ============================================================================

class Orchestrator:
    """The resolver loop. Stateless — all state lives in the spec being
    walked and the IterationResult records returned.

    Construction takes the registered components; `run()` takes the spec
    and a context dict (instruction, config) and drives the loop.
    """

    def __init__(
        self,
        detectors: List[GapDetector],
        suggesters: List[Suggester],
        reviewer: Optional[Any],            # IndependentReviewSuggester or None
        handler: ConfirmationHandler,
        apply_resolution: Callable[[Any, Gap, Resolution, Optional[Suggestion]], None],
        auto_accept_threshold: float = DEFAULT_AUTO_ACCEPT_THRESHOLD,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ):
        """`apply_resolution` writes a Resolution back into the spec.
        Externalized so the orchestrator stays test-friendly without
        tying it to specific spec/Provenance internals.
        """
        self._detectors = detectors
        self._suggesters = suggesters
        self._reviewer = reviewer
        self._handler = handler
        self._apply = apply_resolution
        self._threshold = auto_accept_threshold
        self._max_iterations = max_iterations

    def run(self, spec: Any, context: dict) -> OrchestratorOutcome:
        from nl2protocol.gap_resolution.registry import detect_all

        iterations: List[IterationResult] = []
        for i in range(1, self._max_iterations + 1):
            # DETECT first so a clean spec doesn't append an empty iteration
            # record (would mislead state-log readers about how much work happened).
            gaps = detect_all(spec, context, self._detectors)
            if not gaps:
                # Converged. Don't record an empty iteration unless this is
                # iteration 1 (caller may want to know the spec was already clean).
                if i == 1:
                    iterations.append(IterationResult(iteration=1))
                return OrchestratorOutcome(spec=spec, iterations=iterations,
                                            aborted=False, converged=True)

            iter_result = IterationResult(iteration=i)
            iterations.append(iter_result)

            # TOPOLOGICAL SORT (so dependent gaps get upstream values in this iteration)
            gaps = topo_sort_gaps(gaps)

            # SUGGEST: try suggesters in registry order; first non-None wins.
            suggestions: dict = {}
            for gap in gaps:
                suggestions[gap.id] = self._first_suggestion(gap, spec, context)

            # REVIEW: batched call over inferred/domain_default suggestions.
            # Stamp the verdicts onto the spec's Provenances so the audit
            # trail survives past this iteration (ADR-0009). The hasattr
            # guard lets test fakes pass dict-specs without tripping the
            # stamp; real ProtocolSpec instances always have `.steps`.
            reviews: dict = {}
            if self._reviewer is not None:
                reviews = self._reviewer.review(spec, context)
                if hasattr(spec, "steps"):
                    stamp_reviewer_verdicts(spec, reviews)

            # CLASSIFY + PRESENT + APPLY
            for gap in gaps:
                suggestion = suggestions.get(gap.id)
                review = reviews.get(gap.field_path)
                auto_accept = self._is_auto_acceptable(gap, suggestion, review)

                if auto_accept and suggestion is not None:
                    resolution = Resolution(
                        action="accept_suggestion",
                        new_value=suggestion.value,
                        user_action_provenance="user_accepted_suggestion",  # auto, semantically same
                    )
                    iter_result.records.append(GapResolutionRecord(
                        gap=gap, suggestion=suggestion, review=review,
                        resolution=resolution, auto_accepted=True,
                    ))
                    self._apply(spec, gap, resolution, suggestion)
                    continue

                # Present to user.
                resolution = self._handler.present(gap, suggestion)
                iter_result.records.append(GapResolutionRecord(
                    gap=gap, suggestion=suggestion, review=review,
                    resolution=resolution, auto_accepted=False,
                ))

                if resolution.action == "abort":
                    iter_result.aborted = True
                    return OrchestratorOutcome(spec=spec, iterations=iterations,
                                                aborted=True, converged=False)
                if resolution.action == "skip":
                    continue
                # accept_suggestion or edit → apply
                self._apply(spec, gap, resolution, suggestion)

            # End of iteration; loop top will re-detect.

        # Hit iteration cap without converging.
        # Final detect to know if anything remains.
        from nl2protocol.gap_resolution.registry import detect_all as _detect
        final_gaps = _detect(spec, context, self._detectors)
        return OrchestratorOutcome(
            spec=spec,
            iterations=iterations,
            aborted=False,
            converged=(not final_gaps),
        )

    def _first_suggestion(self, gap: Gap, spec, context: dict) -> Optional[Suggestion]:
        """Suggester precedence: first to return non-None wins for this Gap."""
        for s in self._suggesters:
            try:
                result = s.suggest(gap, spec, context)
            except Exception:
                # Suggesters that crash should not break the loop.
                # (Production: log this; for now, swallow.)
                continue
            if result is not None:
                return result
        return None

    def _is_auto_acceptable(
        self,
        gap: Gap,
        suggestion: Optional[Suggestion],
        review: Optional[ReviewResult],
    ) -> bool:
        if suggestion is None:
            return False
        if gap.kind in ALWAYS_CONFIRM_KINDS:
            return False
        if suggestion.confidence < self._threshold:
            return False
        if review is not None:
            if not (review.confirms_positive and review.confirms_negative):
                return False
        return True


# ============================================================================
# Reviewer-verdict stamping (ADR-0009)
# ============================================================================

def stamp_reviewer_verdicts(spec, reviews: dict) -> None:
    """Stamp review_status + reviewer_objection onto every Provenance whose
    field_path appears in `reviews`.

    Pre:    `spec` is a ProtocolSpec; `reviews` maps field_path -> ReviewResult
            as returned by IndependentReviewSuggester.review(). Every
            ReviewResult is well-formed (objection set iff disagreed) — that
            invariant is enforced upstream by ReviewResult.__post_init__.

    Post:   For each Provenance attached to a step field whose field_path is
            in `reviews`:
              * If review.confirms_positive AND review.confirms_negative:
                  review_status      -> "reviewed_agree"
                  reviewer_objection -> None
              * Otherwise:
                  review_status      -> "reviewed_disagree"
                  reviewer_objection -> review.objection (verbatim)
            The stamp covers per-step value fields (volume, duration,
            temperature, substance, source, destination); LocationRef
            provenance on source/destination is reached via attribute
            traversal. Provenances whose field_path isn't in `reviews` are
            left untouched — their review_status keeps its existing value
            (typically "original" on the first reviewer pass).

    Side effects: Mutates the spec in place — replaces `field_obj.provenance`
            with a re-validated Provenance carrying the new state. Re-validation
            re-runs Provenance's invariants (positive_reasoning required for
            non-instruction sources; reviewer_objection iff reviewed_disagree).

    Raises: pydantic.ValidationError if the resulting Provenance somehow
            violates schema invariants — should not happen by construction.
    """
    from nl2protocol.models.spec import Provenance

    for step_idx, step in enumerate(spec.steps):
        for fname in ("volume", "duration", "temperature", "substance",
                       "source", "destination"):
            field_obj = getattr(step, fname, None)
            if field_obj is None:
                continue
            prov = getattr(field_obj, "provenance", None)
            if prov is None:
                continue
            review = reviews.get(f"steps[{step_idx}].{fname}")
            if review is None:
                continue
            agreed = review.confirms_positive and review.confirms_negative
            updates = {
                "review_status": "reviewed_agree" if agreed else "reviewed_disagree",
                "reviewer_objection": None if agreed else review.objection,
            }
            field_obj.provenance = Provenance.model_validate({
                **prov.model_dump(), **updates,
            })


# ============================================================================
# Default apply_resolution callback for the spec
# ============================================================================

def _stamp_user_action(field_obj, user_action_provenance: str) -> None:
    """Stamp `field_obj.provenance.review_status = user_action_provenance`,
    clearing reviewer_objection in the process (a user action supersedes
    any prior reviewer state).

    Pre:    `field_obj` is None, or any object that may carry a
            `.provenance` attribute (Provenanced*, LocationRef).
            `user_action_provenance` is one of the user_* values that
            Provenance.review_status accepts (user_confirmed,
            user_edited, user_accepted_suggestion, user_skipped).

    Post:   When `field_obj` has a non-None `.provenance`:
              * field_obj.provenance is REPLACED with a re-validated copy
                whose review_status equals the passed user_action_provenance
                and whose reviewer_objection is None.
            When `field_obj` is None or lacks a `.provenance`: no-op.
            Re-validation re-runs Provenance's per-source invariants and
            its review_status biconditional.

    Side effects: Mutates `field_obj.provenance` in place.

    Raises: pydantic.ValidationError if the resulting Provenance violates
            its invariants — should not happen because user_action values
            are all valid review_status Literals AND reviewer_objection
            is cleared (so the disagree-iff-objection invariant can't
            be violated).
    """
    if field_obj is None:
        return
    prov = getattr(field_obj, "provenance", None)
    if prov is None:
        return
    from nl2protocol.models.spec import Provenance
    field_obj.provenance = Provenance.model_validate({
        **prov.model_dump(),
        "review_status": user_action_provenance,
        "reviewer_objection": None,
    })


def default_apply_resolution(spec, gap: Gap, resolution: Resolution,
                              suggestion: Optional[Suggestion]) -> None:
    """Write a Resolution's value into the spec at the gap's field_path AND
    stamp the user's action onto the resulting Provenance (ADR-0009).

    Path-shape coverage (PR2 gap kinds):
      * `initial_contents[N].volume_ul`     — primitive float write; no Provenance.
      * `steps[N].<field>`                  — top-level step field; Provenance stamped.
      * `steps[N].<field>.<subfield>`       — nested write under a parent model
                                              (e.g. steps[0].destination.wells);
                                              parent's Provenance stamped.

    Pre:    `resolution.action` is "accept_suggestion" or "edit"; skip and
            abort are short-circuited by the orchestrator before calling this.
            For "accept_suggestion", `resolution.new_value` is the same shape
            as the field (a Provenance-bearing model for Provenanced fields,
            a primitive for initial_contents.volume_ul).
            For "edit", `resolution.new_value` is the user-typed scalar
            already coerced by the handler's coerce_value callback.

    Post:   The spec is mutated in place:
              * accept_suggestion + Provenanced field:
                  the field is REPLACED with `new_value`; new_value's
                  provenance is stamped with review_status = user_accepted_suggestion.
              * edit + Provenanced field:
                  the field's `.value` attribute is mutated (preserving the
                  surrounding model + its provenance type/shape); provenance
                  is stamped with review_status = user_edited.
              * subfield write:
                  the subfield is set; parent's provenance is stamped.
              * initial_contents.volume_ul:
                  the float is written; no Provenance to stamp.
            In all stamping cases, `reviewer_objection` is cleared because
            the user's action terminates the review lifecycle for that value.

    Side effects: Mutates the spec in place. Replaces or mutates Pydantic
            sub-models on the spec.

    Raises: pydantic.ValidationError on invariant violation in the resulting
            Provenance.
    """
    import re

    path = gap.field_path
    new_value = resolution.new_value
    user_action = resolution.user_action_provenance

    # initial_contents[N].volume_ul (primitive — no Provenance to stamp)
    m = re.match(r"initial_contents\[(\d+)\]\.volume_ul$", path)
    if m:
        idx = int(m.group(1))
        spec.initial_contents[idx].volume_ul = float(new_value)
        return

    # steps[N].<field>
    m = re.match(r"steps\[(\d+)\]\.(\w+)$", path)
    if m:
        idx, fname = int(m.group(1)), m.group(2)
        if resolution.action == "accept_suggestion":
            # new_value is a Provenance-bearing model from the suggester.
            # Replace the field, then stamp.
            setattr(spec.steps[idx], fname, new_value)
            _stamp_user_action(new_value, user_action)
        elif resolution.action == "edit":
            # new_value is a user-typed scalar. Mutate the existing model's
            # `.value` (preserving its type + provenance shape) so the field
            # stays a well-formed Provenanced* / LocationRef.
            existing = getattr(spec.steps[idx], fname, None)
            if existing is not None and hasattr(existing, "value"):
                existing.value = new_value
                _stamp_user_action(existing, user_action)
            else:
                # Fall through to raw setattr (LocationRef edits without a
                # .value attribute, or fields that don't exist yet). Stamp
                # only if the new_value carries provenance.
                setattr(spec.steps[idx], fname, new_value)
                _stamp_user_action(new_value, user_action)
        else:
            # Defensive: any other action just writes raw.
            setattr(spec.steps[idx], fname, new_value)
        return

    # steps[N].<field>.<subfield> (e.g. steps[0].destination.wells)
    m = re.match(r"steps\[(\d+)\]\.(\w+)\.(\w+)$", path)
    if m:
        idx, fname, subfield = int(m.group(1)), m.group(2), m.group(3)
        target = getattr(spec.steps[idx], fname)
        if target is not None:
            setattr(target, subfield, new_value)
            # Stamp the parent's provenance — the subfield change is a
            # user action on the same logical value (e.g., editing the
            # well list under destination is editing destination).
            _stamp_user_action(target, user_action)
        return

    # Unknown path shape: silently no-op (defensive — better than crashing).
    # Future: raise to surface unhandled gap kinds.
