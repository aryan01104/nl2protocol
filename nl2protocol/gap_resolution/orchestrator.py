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
            reviews: dict = {}
            if self._reviewer is not None:
                reviews = self._reviewer.review(spec, context)

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
# Default apply_resolution callback for the spec
# ============================================================================

def default_apply_resolution(spec, gap: Gap, resolution: Resolution,
                              suggestion: Optional[Suggestion]) -> None:
    """Write a Resolution's value into the spec at the gap's field_path.

    This is the "mutation point" the orchestrator uses. Path parsing is
    minimal — supports `steps[N].<field>` and `initial_contents[N].volume_ul`,
    which covers PR2's gap kinds. Future gap kinds may need extension.

    Pre:    `resolution.action` is "accept_suggestion" or "edit". Skip and
            abort are handled by the orchestrator before calling this.
    Post:   The spec is mutated in place. Provenance is attached to the
            written value when applicable (LocationRef, Provenanced*).
    """
    import re

    path = gap.field_path
    new_value = resolution.new_value

    # initial_contents[N].volume_ul
    m = re.match(r"initial_contents\[(\d+)\]\.volume_ul$", path)
    if m:
        idx = int(m.group(1))
        spec.initial_contents[idx].volume_ul = float(new_value)
        return

    # steps[N].<field>
    m = re.match(r"steps\[(\d+)\]\.(\w+)$", path)
    if m:
        idx, fname = int(m.group(1)), m.group(2)
        setattr(spec.steps[idx], fname, new_value)
        return

    # steps[N].<field>.<subfield> (e.g. steps[0].destination.wells)
    m = re.match(r"steps\[(\d+)\]\.(\w+)\.(\w+)$", path)
    if m:
        idx, fname, subfield = int(m.group(1)), m.group(2), m.group(3)
        target = getattr(spec.steps[idx], fname)
        if target is not None:
            setattr(target, subfield, new_value)
        return

    # Unknown path shape: silently no-op (defensive — better than crashing).
    # Future: raise to surface unhandled gap kinds.
