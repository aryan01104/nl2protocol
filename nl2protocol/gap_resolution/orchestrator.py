"""
Orchestrator — the resolver loop (ADR-0008).

Runs DETECT → topological-sort → SUGGEST → REVIEW → CLASSIFY → PRESENT →
APPLY → RE-DETECT, up to N=3 iterations. Sole post-extraction resolution
path since PR3b deleted the legacy verify/fill/refine block in pipeline.py.

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
from nl2protocol.gap_resolution.targets import (
    ConstraintPlaceholder,
    GapTarget,
    InitialVolume,
    InitialWell,
    NamespaceSplit,
    StepField,
    StepProvenance,
    StepSubfield,
    UnknownTarget,
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
        reporter: Optional[Any] = None,
    ):
        """`apply_resolution` writes a Resolution back into the spec.
        Externalized so the orchestrator stays test-friendly without
        tying it to specific spec/Provenance internals.

        `reporter` (ADR-0011 Phase 1) is an optional Reporter that
        receives storytelling events (gap_iteration_*, gap_detected,
        gap_resolved) as the loop progresses. Defaults to None — the
        orchestrator no-ops on emission. Live mode (Phase 3) passes a
        WebSocketReporter so the surface streams the loop in real time.
        """
        self._detectors = detectors
        self._suggesters = suggesters
        self._reviewer = reviewer
        self._handler = handler
        self._apply = apply_resolution
        self._threshold = auto_accept_threshold
        self._max_iterations = max_iterations
        self._reporter = reporter

    def _emit(self, kind: str, data: dict, stage_name: Optional[str] = None) -> None:
        """Emit a storytelling event if a reporter is wired, else no-op.

        Pre:    `kind` is one of the EventKind literals defined in
                nl2protocol.reporting; `data` is the kind-specific dict.
        Post:   When `self._reporter is not None`: a `StageEvent` is
                constructed and passed to `self._reporter.emit(...)`.
                When None: silent no-op (test fakes that don't pass
                a reporter run through the loop unchanged).
        Side effects: Calls reporter.emit which may have I/O (CLI writes,
                WebSocket sends, in-memory buffering — depends on the
                Reporter implementation).
        """
        if self._reporter is None:
            return
        from nl2protocol.reporting import StageEvent
        self._reporter.emit(StageEvent(kind=kind, data=data, stage_name=stage_name))

    def run(self, spec: Any, context: dict,
            gap_filter: Optional[Callable[[Gap], bool]] = None) -> OrchestratorOutcome:
        """Drive the detect → suggest → review → present → apply loop until
        convergence or iteration cap.

        `gap_filter`, when supplied, narrows the loop to a sub-set of gaps:
        each iteration's detected gaps are filtered through the predicate
        before topo-sort, and only matching gaps are presented / applied.
        Used by the pre-orchestrator description-gap pass in pipeline.py
        to resolve description-fabrication gaps BEFORE labware matching
        runs, so labware picks land against finalized descriptions.
        The full orchestrator at the end of the pipeline uses no filter
        and sees the remaining gaps (volume, wells, etc.).
        """
        from nl2protocol.gap_resolution.registry import detect_all

        iterations: List[IterationResult] = []
        for i in range(1, self._max_iterations + 1):
            # DETECT first so a clean spec doesn't append an empty iteration
            # record (would mislead state-log readers about how much work happened).
            gaps = detect_all(spec, context, self._detectors)
            if gap_filter is not None:
                gaps = [g for g in gaps if gap_filter(g)]
            if not gaps:
                # Converged. Don't record an empty iteration unless this is
                # iteration 1 (caller may want to know the spec was already clean).
                if i == 1:
                    iterations.append(IterationResult(iteration=1))
                return OrchestratorOutcome(spec=spec, iterations=iterations,
                                            aborted=False, converged=True)

            # ADR-0011 Phase 1: announce iteration start with the gap-set
            # snapshot the iteration is about to operate on.
            self._emit("gap_iteration_start",
                       {"iteration": i, "gap_count": len(gaps)},
                       stage_name="stage_3_gap_resolver")
            # Phase 3g (Group B): plain-language sub-line for the live
            # indicator. "iteration N — detecting gaps" reads as the
            # micro-action the user sees while we run the loop.
            self._emit("pipeline_progress",
                       {"message": f"iteration {i} — detected {len(gaps)} gaps"},
                       stage_name="stage_3_gap_resolver")

            iter_result = IterationResult(iteration=i)
            iterations.append(iter_result)

            # TOPOLOGICAL SORT (so dependent gaps get upstream values in this iteration)
            gaps = topo_sort_gaps(gaps)

            # ADR-0011 Phase 1: per-gap detection event (one per gap, in
            # topological order — the renderer can reflect priority by
            # event arrival order).
            for gap in gaps:
                self._emit("gap_detected", {
                    "gap_id": gap.id,
                    "gap_kind": gap.kind,
                    "field_path": gap.field_path,
                    "step_order": gap.step_order,
                    "description": gap.description,
                    "severity": gap.severity,
                }, stage_name="stage_3_gap_resolver")

            # SUGGEST: try suggesters in registry order; first non-None wins.
            self._emit("pipeline_progress",
                       {"message": f"iteration {i} — running suggesters"},
                       stage_name="stage_3_gap_resolver")
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
                review_count = sum(1 for s in suggestions.values()
                                    if s is not None
                                    and getattr(s, "provenance_source", "") in ("inferred", "domain_default"))
                if review_count > 0:
                    self._emit("pipeline_progress",
                               {"message": f"iteration {i} — Haiku auditing {review_count} suggestions"},
                               stage_name="stage_3_gap_resolver")
                reviews = self._reviewer.review(spec, context)
                if hasattr(spec, "steps"):
                    stamp_reviewer_verdicts(spec, reviews)

            # CLASSIFY + PRESENT + APPLY
            resolved_in_iteration = 0
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
                    self._emit_gap_resolved(gap, resolution, suggestion, auto_accepted=True)
                    resolved_in_iteration += 1
                    continue

                # If the reviewer disagreed with the suggestion, surface the
                # objection text to the user so they have a falsifier in
                # hand when deciding accept/edit/skip. `Gap` is frozen
                # but `gap.metadata` is a mutable dict — stamp in place.
                # Auto-accept already requires both confirms_* to be True,
                # so a gap that reaches present() is exactly the set where
                # an objection (if any) is load-bearing for the decision.
                if review is not None and getattr(review, "objection", None):
                    gap.metadata["reviewer_objection"] = review.objection

                # Phase 3b-3 (Group C): cross-column spotlight. For
                # initial-contents gaps, look up the underlying labware
                # + well and stamp the prov-ids of every spec cell that
                # references them. The HTML modal reads this out of
                # gap.metadata and pulses those cells while the prompt
                # is open, anchoring the user's attention to the place
                # in the spec their decision affects.
                _stamp_spotlight_prov_ids(gap, spec)

                # Present to user.
                resolution = self._handler.present(gap, suggestion)
                iter_result.records.append(GapResolutionRecord(
                    gap=gap, suggestion=suggestion, review=review,
                    resolution=resolution, auto_accepted=False,
                ))

                if resolution.action == "abort":
                    iter_result.aborted = True
                    self._emit_gap_resolved(gap, resolution, suggestion, auto_accepted=False)
                    self._emit("gap_iteration_end", {
                        "iteration": i,
                        "resolved_count": resolved_in_iteration,
                        "remaining": len(gaps) - resolved_in_iteration - 1,
                        "aborted": True,
                    }, stage_name="stage_3_gap_resolver")
                    return OrchestratorOutcome(spec=spec, iterations=iterations,
                                                aborted=True, converged=False)
                if resolution.action == "skip":
                    self._emit_gap_resolved(gap, resolution, suggestion, auto_accepted=False)
                    continue
                # accept_suggestion or edit → apply
                # Note: for fabricated gaps, the apply path builds the new
                # Provenance directly from `suggestion` (so it can land both
                # value AND provenance when the suggester proposed a value
                # rewrite). resolution.new_value stays as suggestion.value.
                self._apply(spec, gap, resolution, suggestion)
                self._emit_gap_resolved(gap, resolution, suggestion, auto_accepted=False)
                resolved_in_iteration += 1

            # End of iteration; loop top will re-detect.
            self._emit("gap_iteration_end", {
                "iteration": i,
                "resolved_count": resolved_in_iteration,
                "remaining": len(gaps) - resolved_in_iteration,
                "aborted": False,
            }, stage_name="stage_3_gap_resolver")
            self._emit("pipeline_progress",
                       {"message": f"iteration {i} — applied {resolved_in_iteration} fixes"},
                       stage_name="stage_3_gap_resolver")

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

    def _emit_gap_resolved(self, gap: Gap, resolution: Resolution,
                            suggestion: Optional[Suggestion], auto_accepted: bool) -> None:
        """Emit a gap_resolved event with resolution_kind matching the
        Provenance.review_status taxonomy.

        Pre:    `gap` is the Gap that was just resolved, skipped, or aborted;
                `resolution` is the Resolution returned by the handler (or
                synthesized for auto-accept); `suggestion` is the matching
                Suggestion when one existed; `auto_accepted` distinguishes
                orchestrator auto-accept from handler-driven resolution.
        Post:   Emits a "gap_resolved" StageEvent whose data carries the
                gap id + the resolution_kind:
                  * "auto_accepted" when auto_accepted=True
                  * else mirrors resolution.user_action_provenance
                    ("user_accepted_suggestion" / "user_edited" /
                    "user_skipped" / "user_aborted" / "user_confirmed")
                Plus field_path / step_order for the renderer's spec
                cell-anchoring, and a value_repr for compact display.
        Side effects: Same as `_emit` — reporter.emit may do I/O.
        """
        if auto_accepted:
            kind = "auto_accepted"
        else:
            kind = resolution.user_action_provenance
        # Compact value display: prefer the suggestion's value (deterministic
        # case) or the user's typed value; fall back to current_value or
        # placeholder for skip/abort.
        value = resolution.new_value if resolution.new_value is not None else gap.current_value
        try:
            value_repr = repr(value) if value is not None else ""
        except Exception:
            value_repr = "<unrepresentable>"
        self._emit("gap_resolved", {
            "gap_id": gap.id,
            "resolution_kind": kind,
            "value_repr": value_repr[:200],   # cap noise — tooltip can show full value
            "auto_accepted": auto_accepted,
            "field_path": gap.field_path,
            "step_order": gap.step_order,
        }, stage_name="stage_3_gap_resolver")

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

    Post:   For each Provenance whose field_path appears in `reviews`:
              * If review.confirms_positive AND review.confirms_negative:
                  review_status      -> "reviewed_agree"
                  reviewer_objection -> None
              * Otherwise:
                  review_status      -> "reviewed_disagree"
                  reviewer_objection -> review.objection (verbatim)
            Two slots are walked:
              * Per-step value fields (volume / duration / temperature /
                substance / source / destination) — stamps the field's
                primary `provenance`.
              * LocationRef.resolved_label — when the field_path ends in
                `.resolved_label`, the stamp goes to the LocationRef's
                `resolved_label_provenance` slot, NOT the primary
                provenance. This is the same separation
                `_stamp_resolution_action` enforces on the user-action
                side (PR3a step 3 + ADR-0009 audit-trail symmetry).
            Provenances whose field_path isn't in `reviews` are left
            untouched.

    Side effects: Mutates the spec in place — replaces
            `field_obj.provenance` (or `loc_ref.resolved_label_provenance`)
            with a re-validated Provenance carrying the new state.
            Re-validation re-runs Provenance's invariants
            (positive_reasoning required for non-instruction sources;
            reviewer_objection iff reviewed_disagree).

    Raises: pydantic.ValidationError if the resulting Provenance somehow
            violates schema invariants — should not happen by construction.
    """
    from nl2protocol.models.spec import Provenance

    # User-action statuses are TERMINAL: once the user has acted on a
    # slot, the reviewer pass MUST NOT overwrite that decision. Without
    # this guard a reviewer that fires after a pre-orchestrator modal
    # closes would silently flip the user's decision to a reviewer
    # verdict (the objection stays in the field but the audit trail
    # forgets the user ever acted).
    _TERMINAL_USER_STATUSES = frozenset({
        "user_confirmed",
        "user_edited",
        "user_accepted_suggestion",
        "user_skipped",
        "user_overrode_fabrication",
    })

    def _verdict_updates(review):
        agreed = review.confirms_positive and review.confirms_negative
        return {
            "review_status": "reviewed_agree" if agreed else "reviewed_disagree",
            "reviewer_objection": None if agreed else review.objection,
        }

    def _should_stamp(prov) -> bool:
        """A slot is stamped iff (a) the reviewer would have actually
        looked at it (skip instruction-sourced — same rule
        IndependentReviewSuggester applies when collecting claims),
        AND (b) it doesn't already carry a terminal user-action
        status (user decisions are sacrosanct in their own slot).
        """
        if prov is None:
            return False
        if prov.source == "instruction":
            return False
        if prov.review_status in _TERMINAL_USER_STATUSES:
            return False
        return True

    for step_idx, step in enumerate(spec.steps):
        # Atomic-field provenances (one slot each).
        for fname in ("volume", "duration", "temperature", "substance"):
            field_obj = getattr(step, fname, None)
            if field_obj is None:
                continue
            prov = getattr(field_obj, "provenance", None)
            if not _should_stamp(prov):
                continue
            review = reviews.get(f"steps[{step_idx}].{fname}")
            if review is None:
                continue
            field_obj.provenance = Provenance.model_validate({
                **prov.model_dump(), **_verdict_updates(review),
            })

        # LocationRef field provenances (two slots: description + wells).
        # A reviewer verdict on `steps[N].source` covers the field as a
        # whole, but each slot is gated independently by `_should_stamp`
        # — an instruction-sourced sibling stays at its original status
        # rather than picking up a verdict from a slot the model
        # actually reviewed (mirror of the reviewer's own skip rule
        # over instruction-sourced provenances).
        for role in ("source", "destination"):
            ref = getattr(step, role, None)
            if ref is None:
                continue
            review = reviews.get(f"steps[{step_idx}].{role}")
            if review is None:
                continue
            for prov_attr in ("description_provenance", "wells_provenance"):
                prov = getattr(ref, prov_attr, None)
                if not _should_stamp(prov):
                    continue
                setattr(ref, prov_attr, Provenance.model_validate({
                    **prov.model_dump(), **_verdict_updates(review),
                }))

        # Labware-resolution provenances on LocationRefs (PR3a step 3).
        for role in ("source", "destination"):
            ref = getattr(step, role, None)
            if ref is None:
                continue
            rprov = getattr(ref, "resolved_label_provenance", None)
            if not _should_stamp(rprov):
                continue
            review = reviews.get(f"steps[{step_idx}].{role}.resolved_label")
            if review is None:
                continue
            ref.resolved_label_provenance = Provenance.model_validate({
                **rprov.model_dump(), **_verdict_updates(review),
            })


# ============================================================================
# Default apply_resolution callback for the spec
# ============================================================================

def _stamp_spotlight_prov_ids(gap: Gap, spec) -> None:
    """Stamp `gap.metadata["spotlight_prov_ids"]` with the prov-ids of
    spec cells the user's decision will land on. The HTML gap modal
    reads this and (a) anchors itself near the first target cell with
    dotted arrows to all targets (#73), (b) pulses cells via
    `.prov-spotlight` while the prompt is open.

    Three gap shapes get handled:
    1. `initial_contents[N].volume_ul` (rare in live mode now that
       the IC batch handles these) — find every step whose source/
       destination references that (labware, well) and stamp those
       step cells.
    2. `steps[N].<field>` for field in volume/substance/duration/
       temperature/source/destination — stamp the cell directly
       ("s{N}-<field>"). Covers missing-field, fabrication, and
       most constraint-violation gaps.
    3. `steps[N].<field>.<subfield>` (LocationRef sub-fields like
       resolved_label) — stamp the parent cell since that's where
       the value renders.

    Constraint-violation gaps with `metadata.affected_paths` (dedupe)
    expand to all affected step cells.

    Pre:    `gap` is the gap about to be presented. `gap.metadata` is
            the mutable dict on the (frozen) gap. `spec` is the current
            ProtocolSpec.
    Post:   When at least one cell matches, `gap.metadata["spotlight_prov_ids"]`
            holds a space-separated string of "s{step_idx}-{field}" prov-ids
            (deduped, order-preserving). Helper is a silent no-op on
            shape mismatches — it's a UX hint, not load-bearing.
    """
    import re as _re
    pids: list = []
    field_path = getattr(gap, "field_path", "") or ""

    # Constraint dedupe: affected_paths (a list of dotted field paths)
    # lets one gap stand for resolutions across many cells. Spotlight
    # all of them.
    affected = (gap.metadata or {}).get("affected_paths") if gap.metadata else None
    paths_to_walk = list(affected) if isinstance(affected, list) else [field_path]

    # Cells the renderer actually exposes as data-prov-id="s{N}-{field}".
    _RENDERABLE_FIELDS = {"volume", "substance", "duration", "temperature",
                            "source", "destination"}

    for path in paths_to_walk:
        if not path:
            continue
        sm = _re.match(r"steps\[(\d+)\]\.(\w+)(?:\.\w+)?$", path)
        if sm:
            step_idx, field = int(sm.group(1)), sm.group(2)
            if field in _RENDERABLE_FIELDS:
                pids.append(f"s{step_idx}-{field}")

    # Initial-contents shape: spotlight every step cell that touches
    # the same (labware, well).
    ic_match = _re.match(r"initial_contents\[(\d+)\]", field_path)
    if ic_match:
        try:
            from nl2protocol.reporting import _lab_state_row_target_prov_ids
            idx = int(ic_match.group(1))
            ic_list = getattr(spec, "initial_contents", None) or []
            if idx < len(ic_list):
                ic = ic_list[idx]
                labware = getattr(ic, "labware", None)
                well = getattr(ic, "well", None)
                if labware:
                    pids.extend(_lab_state_row_target_prov_ids(spec, labware, well or ""))
        except Exception:
            pass

    if pids:
        seen, deduped = set(), []
        for p in pids:
            if p not in seen:
                seen.add(p)
                deduped.append(p)
        gap.metadata["spotlight_prov_ids"] = " ".join(deduped)


def _stamp_user_action(field_obj, user_action_provenance: str) -> None:
    """Stamp review_status=user_action_provenance on every Provenance slot
    of field_obj, clearing reviewer_objection (a user action supersedes
    any prior reviewer state).

    Pre:    `field_obj` is None, or any object that may carry one or more
            Provenance slots. Atomic Provenanced* types carry `.provenance`;
            LocationRef carries `.description_provenance` and (optionally)
            `.wells_provenance`. `user_action_provenance` is one of the
            user_* values that Provenance.review_status accepts
            (user_confirmed, user_edited, user_accepted_suggestion,
            user_skipped).

    Post:   Every populated provenance slot on field_obj is REPLACED with
            a re-validated copy whose review_status equals the passed
            user_action_provenance and whose reviewer_objection is None.
            When `field_obj` is None or carries no provenance slots: no-op.
            Re-validation re-runs Provenance's per-source invariants and
            its review_status biconditional.

    Side effects: Mutates provenance slots on field_obj in place.

    Raises: pydantic.ValidationError if a resulting Provenance violates
            its invariants — should not happen because user_action values
            are all valid review_status Literals AND reviewer_objection
            is cleared (so the disagree-iff-objection invariant can't
            be violated).
    """
    if field_obj is None:
        return
    from nl2protocol.models.spec import Provenance
    for prov_attr in ("provenance", "description_provenance", "wells_provenance"):
        prov = getattr(field_obj, prov_attr, None)
        if prov is None:
            continue
        setattr(field_obj, prov_attr, Provenance.model_validate({
            **prov.model_dump(),
            "review_status": user_action_provenance,
            "reviewer_objection": None,
        }))


def _stamp_resolution_action(loc_ref, user_action_provenance: str, label) -> None:
    """Stamp `loc_ref.resolved_label_provenance.review_status` after the
    user picks (or edits) a config labware label for an ambiguous
    LocationRef.

    Why this is separate from `_stamp_user_action`: a LocationRef has
    TWO Provenance slots — `provenance` (about the location/wells the
    user described) and `resolved_label_provenance` (about which config
    labware the description maps to). When the user resolves an
    ambiguity Gap, the action affects the resolution decision, not the
    user's location-description. Stamping the wrong slot would corrupt
    the audit trail.

    Pre:    `loc_ref` is a LocationRef whose `resolved_label` was just
            written (by `default_apply_resolution`'s subfield branch).
            `user_action_provenance` is one of the user_* review_status
            values. `label` is the config-labware string the user
            picked — used as fallback when the resolver hadn't yet
            written a resolved_label_provenance (e.g. the resolver
            skipped this ref because the LLM returned null).

    Post:   When `loc_ref.resolved_label_provenance` exists:
              * It is REPLACED with a re-validated copy whose
                review_status equals user_action_provenance and whose
                reviewer_objection is None.
            When `loc_ref.resolved_label_provenance` is None (no prior
            resolver attempt):
              * A fresh Provenance is constructed with source='inferred',
                positive_reasoning naming the user's pick, why_not_in_instruction
                noting the description-vs-config-key gap, review_status set
                to user_action_provenance, confidence 1.0 (the user is the
                authority).

    Side effects: Mutates `loc_ref.resolved_label_provenance` in place.

    Raises: pydantic.ValidationError if the resulting Provenance violates
            its invariants — should not happen by construction.
    """
    from nl2protocol.models.spec import Provenance
    existing = getattr(loc_ref, "resolved_label_provenance", None)
    description = getattr(loc_ref, "description", "")
    if existing is None:
        loc_ref.resolved_label_provenance = Provenance(
            source="inferred",
            positive_reasoning=(
                f"User picked config label '{label}' for description "
                f"'{description}'."
            ),
            why_not_in_instruction=(
                f"Description '{description}' did not uniquely identify "
                f"a config labware via automatic matching; user resolved "
                f"the ambiguity directly."
            ),
            review_status=user_action_provenance,
            confidence=1.0,
        )
        return
    loc_ref.resolved_label_provenance = Provenance.model_validate({
        **existing.model_dump(),
        "review_status": user_action_provenance,
        "reviewer_objection": None,
    })


def _build_suggested_provenance(suggestion: Suggestion, review_status: str):
    """Construct the Provenance to stamp on a spec field when accepting a Suggestion.

    Pre:    `suggestion` is the Suggestion being accepted; `review_status`
            is a valid `Provenance.review_status` literal capturing the
            user (or auto) action that drove acceptance.

    Post:   When `suggestion.provenance_source == "cited"` AND
            `suggestion.cited_text` is non-empty → returns a Provenance
            with `source="instruction"`, `cited_text=[suggestion.cited_text]`,
            `confidence=suggestion.confidence`, and `review_status` as
            given. positive_reasoning + why_not_in_instruction are LEFT
            NULL because Provenance.require_appropriate_field_for_source
            forbids them when source="instruction".
            Otherwise → returns a Provenance with `source="inferred"`,
            `positive_reasoning=suggestion.positive_reasoning`,
            `why_not_in_instruction=suggestion.why_not_in_instruction`,
            same confidence + review_status.

    Why: bridges the suggester-internal "cited" label into the spec-level
    Provenance.source = "instruction". Without this, an LLM-identified
    citation showed up as `(cited)` in the modal but landed in the spec
    as `inferred` after acceptance, losing the colored cite/value linkage
    in the report.
    """
    from nl2protocol.models.spec import Provenance
    cited_text = getattr(suggestion, "cited_text", None)
    if suggestion.provenance_source == "cited" and cited_text:
        return Provenance(
            source="instruction",
            cited_text=[cited_text],
            review_status=review_status,
            confidence=suggestion.confidence,
        )
    return Provenance(
        source="inferred",
        positive_reasoning=suggestion.positive_reasoning,
        why_not_in_instruction=suggestion.why_not_in_instruction,
        review_status=review_status,
        confidence=suggestion.confidence,
    )


def _expected_field_type_for_step(step, fname: str):
    """Return the underlying type expected at `step.<fname>`.

    Pre:    `step` is a Pydantic model instance (e.g. ExtractedStep);
            `fname` is a candidate field name.
    Post:   - When the field exists with an `Optional[X]` (i.e. `X | None`)
              annotation where X is a single concrete class, returns X.
            - When the field exists with a plain class annotation,
              returns that class.
            - Otherwise returns None (field not found, Union with >1 non-None
              arm, generic alias, etc.). Caller treats None as "no type
              guard available — skip the isinstance check."
    Side effects: None.
    """
    from typing import Union, get_args, get_origin
    import types as _types
    field_info = type(step).model_fields.get(fname)
    if field_info is None:
        return None
    ann = field_info.annotation
    origin = get_origin(ann)
    union_origin = getattr(_types, "UnionType", None)
    if origin is Union or (union_origin is not None and origin is union_origin):
        non_none = [a for a in get_args(ann) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
        return None
    return ann


def default_apply_resolution(spec, gap: Gap, resolution: Resolution,
                              suggestion: Optional[Suggestion]) -> None:
    """Write a Resolution's value into the spec at every target in
    `gap.targets` AND stamp the user's action onto the resulting Provenance
    (ADR-0009).

    Pre:    `gap.targets` is non-empty (Phase 2 post-init guarantees this:
            it auto-derives from `field_path` and/or
            `metadata["affected_paths"]` when the caller didn't pass an
            explicit list).
            `resolution.action` is "accept_suggestion", "edit", or
            "override"; skip and abort are short-circuited by the
            orchestrator before calling this.
            For "accept_suggestion", `resolution.new_value` is the same
            shape as the field (a Provenance-bearing model for Provenanced
            fields, a primitive for initial_contents.volume_ul). For
            "edit", `resolution.new_value` is the user-typed scalar.

    Post:   The spec is mutated in place. For each target in `gap.targets`,
            the write semantics match the variant:
              * StepField  — accept_suggestion REPLACES the field via
                `replace_with_history_preserved`; edit mutates `.value`;
                override stamps prov on the existing value.
              * StepSubfield — mutates the subfield, stamps the parent's
                provenance (or `resolved_label_provenance` when the
                subfield is `resolved_label`).
              * StepProvenance — writes the fabrication-shaped slot;
                accept_suggestion rebuilds the Provenance from the
                suggester's reasoning.
              * InitialVolume / InitialWell — pushes a revision on the
                WellContents primitive.
              * ConstraintPlaceholder — no-op; informational gaps the
                user resolves by editing config offline.
              * NamespaceSplit / UnknownTarget — no-op; not addressable
                spec slots.
            Deduped constraint-violation gaps (`gap.targets` length > 1)
            apply the resolution to EVERY target — the user answered once,
            and the answer propagates to every affected step.

    Side effects: Mutates the spec in place.
    Raises: pydantic.ValidationError on invariant violation; TypeError if
            a suggester delivers a value whose type doesn't match the
            target field's declared type (contract guard at the StepField
            arm).
    """
    for target in gap.targets:
        _apply_at_target(spec, target, resolution, suggestion)


# LocationRef value sub-fields → their provenance slot. When an apply
# path changes one of these via accept_suggestion or edit, we replace
# the corresponding provenance with one that honestly attributes the
# new value (instead of leaving the stale instruction-cited provenance
# in place from the old value).
_LOCATIONREF_VALUE_SUBFIELDS = {
    "well": "wells_provenance",
    "wells": "wells_provenance",
    "well_range": "wells_provenance",
    "description": "description_provenance",
}


def _apply_at_target(spec, target: GapTarget, resolution: Resolution,
                     suggestion: Optional[Suggestion] = None) -> None:
    """Apply a single Resolution to one typed GapTarget.

    Replaces the regex-based `_apply_at_path` (Phase 3 of the typed-
    targets refactor). Dispatches on `target` variant with `match` rather
    than parsing a dotted path; each arm preserves the exact write
    semantics of its old regex-branch counterpart.

    Revision-history contract: each variant arm performs exactly ONE
    logical write per call. For fields that carry a `prior_revisions`
    chain (Provenanced* / LocationRef / WellContents), the pre-write
    state is snapshotted into `prior_revisions` BEFORE the head fields
    are mutated. Stamping (`_stamp_user_action` /
    `_stamp_resolution_action`) operates on the head AFTER the snapshot.

    Pre:    `target` is any GapTarget variant. For spec-addressable
            variants (StepField, StepSubfield, StepProvenance, Initial*),
            the referenced spec index must exist; for sentinel variants
            (ConstraintPlaceholder, NamespaceSplit, UnknownTarget) the
            call is a no-op regardless of state.

    Post:   The spec is mutated in place per the per-variant semantics
            documented under `default_apply_resolution`. Sentinel
            variants leave the spec unchanged.

    Side effects: Mutates Pydantic sub-models on the spec; may push
            revisions onto `prior_revisions` chains.

    Raises: pydantic.ValidationError on invariant violation; TypeError
            from the StepField/accept_suggestion contract guard if a
            suggester delivered a value whose type doesn't match the
            target field's declared type.
    """
    from nl2protocol.models.spec import (
        Provenance,
        push_revision,
        replace_with_history_preserved,
    )

    new_value = resolution.new_value
    user_action = resolution.user_action_provenance

    match target:
        # --- StepProvenance — fabrication-shaped slot (parent has a
        # `.<slot>provenance` attribute the verifier flagged as malformed).
        case StepProvenance(step_idx=idx, field=fname, slot=slot):
            parent = getattr(spec.steps[idx], fname, None)
            if parent is None:
                return
            if resolution.action == "accept_suggestion":
                if suggestion is not None:
                    new_prov = _build_suggested_provenance(
                        suggestion, review_status="user_accepted_suggestion",
                    )
                    proposed_value = suggestion.value
                else:
                    new_prov = new_value if isinstance(new_value, Provenance) else None
                    proposed_value = None
                if new_prov is None:
                    return
                value_field = None
                if proposed_value is not None:
                    if slot == "provenance" and hasattr(parent, "value"):
                        value_field = "value"
                    elif slot == "description_provenance" and hasattr(parent, "description"):
                        value_field = "description"
                if hasattr(parent, "prior_revisions"):
                    push_revision(parent)
                if value_field is not None:
                    setattr(parent, value_field, proposed_value)
                setattr(parent, slot, new_prov)
                return
            if resolution.action == "override":
                existing_prov = getattr(parent, slot, None)
                if existing_prov is not None:
                    if hasattr(parent, "prior_revisions"):
                        push_revision(parent)
                    setattr(parent, slot, Provenance.model_validate({
                        **existing_prov.model_dump(),
                        "review_status": "user_overrode_fabrication",
                        "reviewer_objection": None,
                    }))
                return
            if resolution.action == "edit" and hasattr(parent, "value"):
                if hasattr(parent, "prior_revisions"):
                    push_revision(parent)
                parent.value = new_value
                setattr(parent, slot, Provenance(
                    source="inferred",
                    positive_reasoning=(
                        "User-typed value during fabrication resolution."
                    ),
                    why_not_in_instruction=(
                        "User edited the value; original citation was malformed."
                    ),
                    confidence=1.0,
                    review_status="user_edited",
                ))
                return
            # Edit on LocationRef sub-slots, or unknown action: silent
            # no-op.
            return

        # --- StepField — top-level step attribute (volume, source, ...)
        case StepField(step_idx=idx, field=fname):
            if resolution.action == "override":
                # ADR-0012: user kept the value, just stamped the prov.
                existing = getattr(spec.steps[idx], fname, None)
                if existing is not None and hasattr(existing, "prior_revisions"):
                    push_revision(existing)
                _stamp_user_action(existing, user_action)
                return
            if resolution.action == "accept_suggestion":
                # Contract guard: suggester must deliver the typed model,
                # not a raw dict/str/int. Surfaces suggester bugs at the
                # source rather than poisoning the spec downstream.
                expected = _expected_field_type_for_step(spec.steps[idx], fname)
                if (expected is not None
                        and isinstance(expected, type)
                        and not isinstance(new_value, expected)):
                    raise TypeError(
                        f"Suggester contract violation at "
                        f"steps[{idx}].{fname}: expected "
                        f"{expected.__name__} instance, got "
                        f"{type(new_value).__name__} ({new_value!r:.80}). "
                        f"Suggesters must construct the typed Pydantic "
                        f"model (see ConfigLookupSuggester for the "
                        f"canonical pattern)."
                    )
                old = getattr(spec.steps[idx], fname, None)
                transferred = (
                    replace_with_history_preserved(old, new_value)
                    if old is not None
                    else new_value
                )
                setattr(spec.steps[idx], fname, transferred)
                _stamp_user_action(transferred, user_action)
                return
            if resolution.action == "edit":
                existing = getattr(spec.steps[idx], fname, None)
                if existing is not None and hasattr(existing, "value"):
                    if hasattr(existing, "prior_revisions"):
                        push_revision(existing)
                    existing.value = new_value
                    _stamp_user_action(existing, user_action)
                else:
                    setattr(spec.steps[idx], fname, new_value)
                    _stamp_user_action(new_value, user_action)
                return
            # Defensive: any other action writes raw.
            setattr(spec.steps[idx], fname, new_value)
            return

        # --- StepSubfield — nested LocationRef slot
        # (steps[N].source.wells, steps[N].destination.resolved_label, ...)
        case StepSubfield(step_idx=idx, field=fname, subfield=subfield):
            parent = getattr(spec.steps[idx], fname)
            if parent is None:
                return
            if hasattr(parent, "prior_revisions"):
                push_revision(parent)
            setattr(parent, subfield, new_value)
            # resolved_label has its own provenance slot (resolution-time
            # decision, distinct from the location-reading provenance).
            if subfield == "resolved_label":
                _stamp_resolution_action(parent, user_action, new_value)
                return
            # Other LocationRef value subfields: replace the matching
            # *_provenance with one that honestly attributes the new value.
            prov_slot = _LOCATIONREF_VALUE_SUBFIELDS.get(subfield)
            if prov_slot is not None:
                new_prov = None
                if (resolution.action == "accept_suggestion"
                        and suggestion is not None):
                    new_prov = _build_suggested_provenance(
                        suggestion, review_status=user_action,
                    )
                elif resolution.action == "edit":
                    new_prov = Provenance(
                        source="inferred",
                        positive_reasoning=(
                            "User edited this value directly during gap "
                            "resolution; not lifted from the instruction."
                        ),
                        why_not_in_instruction=(
                            "User chose this value; it was not cited from "
                            "the instruction."
                        ),
                        review_status=user_action,
                        confidence=1.0,
                    )
                if new_prov is not None:
                    setattr(parent, prov_slot, new_prov)
                    return
            _stamp_user_action(parent, user_action)
            return

        # --- InitialVolume — primitive write on WellContents.
        case InitialVolume(well_idx=idx):
            wc = spec.initial_contents[idx]
            push_revision(wc, volume_ul=float(new_value))
            return

        # --- InitialWell — symmetric primitive write.
        case InitialWell(well_idx=idx):
            wc = spec.initial_contents[idx]
            push_revision(wc, well=str(new_value))
            return

        # --- Sentinel variants — no spec slot, no write.
        # ConstraintPlaceholder: informational constraint flag (tip count,
        # slot conflict, etc.). User resolves by editing config and re-
        # running. This is the structural fix to the BCA-style crash —
        # the variant explicitly says "no spec target" so we never
        # setattr a fictional field.
        # NamespaceSplit / UnknownTarget: not addressable spec slots;
        # surface elsewhere if at all.
        case ConstraintPlaceholder() | NamespaceSplit() | UnknownTarget():
            return

        case _:
            # Exhaustiveness backstop. Should be unreachable given the
            # GapTarget Union is closed; if it fires, a new variant was
            # added without updating this dispatch.
            return
