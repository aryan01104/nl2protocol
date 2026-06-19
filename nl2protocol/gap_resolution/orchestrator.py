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
        """
        if self._reporter is None:
            return
        from nl2protocol.reporting import StageEvent
        self._reporter.emit(StageEvent(kind=kind, data=data, stage_name=stage_name))

    def run(self, spec: Any, context: dict,
            gap_filter: Optional[Callable[[Gap], bool]] = None) -> OrchestratorOutcome:
        """Drive the detect → suggest → review → present → apply loop until
        convergence or iteration cap.
        """
        from nl2protocol.gap_resolution.registry import detect_all

        iterations: List[IterationResult] = []
        for i in range(1, self._max_iterations + 1):
            gaps = detect_all(spec, context, self._detectors)
            if gap_filter is not None:
                gaps = [g for g in gaps if gap_filter(g)]
            if not gaps:
                if i == 1:
                    iterations.append(IterationResult(iteration=1))
                return OrchestratorOutcome(spec=spec, iterations=iterations,
                                            aborted=False, converged=True)

            self._emit("gap_iteration_start",
                       {"iteration": i, "gap_count": len(gaps)},
                       stage_name="stage_3_gap_resolver")
            self._emit("pipeline_progress",
                       {"message": f"iteration {i} — detected {len(gaps)} gaps"},
                       stage_name="stage_3_gap_resolver")

            iter_result = IterationResult(iteration=i)
            iterations.append(iter_result)

            # TOPOLOGICAL SORT (so dependent gaps get upstream values in this iteration)
            gaps = topo_sort_gaps(gaps)

            for gap in gaps:
                self._emit("gap_detected", {
                    "gap_id": gap.id,
                    "gap_kind": gap.kind,
                    "field_path": gap.field_path,
                    "step_order": gap.step_order,
                    "description": gap.description,
                    "severity": gap.severity,
                }, stage_name="stage_3_gap_resolver")

            self._emit("pipeline_progress",
                       {"message": f"iteration {i} — running suggesters"},
                       stage_name="stage_3_gap_resolver")
            suggestions: dict = {}
            for gap in gaps:
                suggestions[gap.id] = self._first_suggestion(gap, spec, context)

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

                if review is not None and getattr(review, "objection", None):
                    gap.metadata["reviewer_objection"] = review.objection

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
        """
        if auto_accepted:
            kind = "auto_accepted"
        else:
            kind = resolution.user_action_provenance
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
    """
    from nl2protocol.models.spec import (
        InstructionProvenance, InferredProvenance, ProvenanceBase, validate_provenance,
    )

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
            field_obj.provenance = validate_provenance({
                **prov.model_dump(), **_verdict_updates(review),
            })

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
                setattr(ref, prov_attr, validate_provenance({
                    **prov.model_dump(), **_verdict_updates(review),
                }))

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
            ref.resolved_label_provenance = validate_provenance({
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
    """
    import re as _re
    pids: list = []
    field_path = getattr(gap, "field_path", "") or ""


    affected = (gap.metadata or {}).get("affected_paths") if gap.metadata else None
    paths_to_walk = list(affected) if isinstance(affected, list) else [field_path]

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
    """
    if field_obj is None:
        return
    from nl2protocol.models.spec import (
        InstructionProvenance, InferredProvenance, ProvenanceBase, validate_provenance,
    )
    for prov_attr in ("provenance", "description_provenance", "wells_provenance"):
        prov = getattr(field_obj, prov_attr, None)
        if prov is None:
            continue
        setattr(field_obj, prov_attr, validate_provenance({
            **prov.model_dump(),
            "review_status": user_action_provenance,
            "reviewer_objection": None,
        }))


def _stamp_resolution_action(loc_ref, user_action_provenance: str, label) -> None:
    """Stamp `loc_ref.resolved_label_provenance.review_status` after the
    user picks (or edits) a config labware label for an ambiguous
    LocationRef.
    """
    from nl2protocol.models.spec import (
        InstructionProvenance, InferredProvenance, ProvenanceBase, validate_provenance,
    )
    existing = getattr(loc_ref, "resolved_label_provenance", None)
    description = getattr(loc_ref, "description", "")
    if existing is None:
        loc_ref.resolved_label_provenance = InferredProvenance(
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
    loc_ref.resolved_label_provenance = validate_provenance({
        **existing.model_dump(),
        "review_status": user_action_provenance,
        "reviewer_objection": None,
    })


def _build_suggested_provenance(suggestion: Suggestion, review_status: str):
    """Construct the Provenance to stamp on a spec field when accepting a Suggestion.
    """
    from nl2protocol.models.spec import (
        InstructionProvenance, InferredProvenance, ProvenanceBase, validate_provenance,
    )
    cited_text = getattr(suggestion, "cited_text", None)
    if suggestion.provenance_source == "cited" and cited_text:
        return InstructionProvenance(
            source="instruction",
            cited_text=[cited_text],
            review_status=review_status,
            confidence=suggestion.confidence,
        )
    return InferredProvenance(
        source="inferred",
        positive_reasoning=suggestion.positive_reasoning,
        why_not_in_instruction=suggestion.why_not_in_instruction,
        review_status=review_status,
        confidence=suggestion.confidence,
    )


def _expected_field_type_for_step(step, fname: str):
    """Return the underlying type expected at `step.<fname>`.
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
    """Write a Resolution's value into the spec at the gap's field_path AND
    stamp the user's action onto the resulting Provenance (ADR-0009).
    """
    affected_paths = (gap.metadata or {}).get("affected_paths") if hasattr(gap, "metadata") else None
    if affected_paths and len(affected_paths) > 1:
        for path in affected_paths:
            _apply_at_path(spec, path, resolution, suggestion)
        return
    _apply_at_path(spec, gap.field_path, resolution, suggestion)


_LOCATIONREF_VALUE_SUBFIELDS = {
    "well": "wells_provenance",
    "wells": "wells_provenance",
    "well_range": "wells_provenance",
    "description": "description_provenance",
}


def _apply_at_path(spec, path: str, resolution: Resolution,
                   suggestion: Optional[Suggestion] = None) -> None:
    """Single-path apply — extracted from default_apply_resolution so
    deduped Gaps can call it once per affected path.
    """
    import re
    from nl2protocol.models.spec import push_revision, replace_with_history_preserved

    new_value = resolution.new_value
    user_action = resolution.user_action_provenance

    m = re.match(r"steps\[(\d+)\]\.(\w+)\.(\w*provenance)$", path)
    if m:
        idx, fname, slot = int(m.group(1)), m.group(2), m.group(3)
        parent = getattr(spec.steps[idx], fname, None)
        if parent is None:
            return
        from nl2protocol.models.spec import (
        InstructionProvenance, InferredProvenance, ProvenanceBase, validate_provenance,
    )
        if resolution.action == "accept_suggestion":
            if suggestion is not None:
                new_prov = _build_suggested_provenance(
                    suggestion, review_status="user_accepted_suggestion",
                )
                proposed_value = suggestion.value
            else:
                new_prov = new_value if isinstance(new_value, ProvenanceBase) else None
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
                setattr(parent, slot, validate_provenance({
                    **existing_prov.model_dump(),
                    "review_status": "user_overrode_fabrication",
                    "reviewer_objection": None,
                }))
            return
        if resolution.action == "edit" and hasattr(parent, "value"):
            if hasattr(parent, "prior_revisions"):
                push_revision(parent)
            parent.value = new_value
            setattr(parent, slot, InferredProvenance(
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
        return

    if resolution.action == "override":
        m = re.match(r"steps\[(\d+)\]\.(\w+)$", path)
        if m:
            idx, fname = int(m.group(1)), m.group(2)
            existing = getattr(spec.steps[idx], fname, None)
            if existing is not None and hasattr(existing, "prior_revisions"):
                push_revision(existing)
            _stamp_user_action(existing, user_action)
        return

    m = re.match(r"initial_contents\[(\d+)\]\.volume_ul$", path)
    if m:
        idx = int(m.group(1))
        wc = spec.initial_contents[idx]
        push_revision(wc, volume_ul=float(new_value))
        return

    m = re.match(r"initial_contents\[(\d+)\]\.well$", path)
    if m:
        idx = int(m.group(1))
        wc = spec.initial_contents[idx]
        push_revision(wc, well=str(new_value))
        return

    # steps[N].<field>
    m = re.match(r"steps\[(\d+)\]\.(\w+)$", path)
    if m:
        idx, fname = int(m.group(1)), m.group(2)
        if resolution.action == "accept_suggestion":
            expected = _expected_field_type_for_step(spec.steps[idx], fname)
            if (expected is not None
                    and isinstance(expected, type)
                    and not isinstance(new_value, expected)):
                raise TypeError(
                    f"Suggester contract violation at {path!r}: "
                    f"expected {expected.__name__} instance, got "
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
        elif resolution.action == "edit":
            existing = getattr(spec.steps[idx], fname, None)
            if existing is not None and hasattr(existing, "value"):
                if hasattr(existing, "prior_revisions"):
                    push_revision(existing)
                existing.value = new_value
                _stamp_user_action(existing, user_action)
            else:
                setattr(spec.steps[idx], fname, new_value)
                _stamp_user_action(new_value, user_action)
        else:
            setattr(spec.steps[idx], fname, new_value)
        return

    m = re.match(r"steps\[(\d+)\]\.(\w+)\.(\w+)$", path)
    if m:
        idx, fname, subfield = int(m.group(1)), m.group(2), m.group(3)
        target = getattr(spec.steps[idx], fname)
        if target is not None:
            if hasattr(target, "prior_revisions"):
                push_revision(target)
            setattr(target, subfield, new_value)
            if subfield == "resolved_label":
                _stamp_resolution_action(target, user_action, new_value)
                return
            from nl2protocol.models.spec import (
        InstructionProvenance, InferredProvenance, ProvenanceBase, validate_provenance,
    )
            prov_slot = _LOCATIONREF_VALUE_SUBFIELDS.get(subfield)
            if prov_slot is not None:
                new_prov = None
                if (resolution.action == "accept_suggestion"
                        and suggestion is not None):
                    new_prov = _build_suggested_provenance(
                        suggestion, review_status=user_action,
                    )
                elif resolution.action == "edit":
                    new_prov = InferredProvenance(
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
                    setattr(target, prov_slot, new_prov)
                    return
            _stamp_user_action(target, user_action)
        return
