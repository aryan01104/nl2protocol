"""
Contract tests for the gap-resolution orchestrator (ADR-0008 PR2).

The orchestrator is a state machine: detect → topo-sort → suggest →
review → classify → present → apply → re-detect, max N iterations.

These tests use FAKE detectors / suggesters / handlers / appliers to
isolate the orchestrator's logic. No LLM calls.
"""

from __future__ import annotations

import pytest

from nl2protocol.models.spec import InstructionProvenance, InferredProvenance, validate_provenance
from nl2protocol.gap_resolution import (
    Gap,
    GapResolutionRecord,
    IterationResult,
    Orchestrator,
    Resolution,
    ReviewResult,
    Suggestion,
    stamp_reviewer_verdicts,
    topo_sort_gaps,
)


# ============================================================================
# FAKES
# ============================================================================

class FakeDetector:
    """Detector that returns a pre-baked list of Gaps. After N detect()
    calls (one per iteration), the list shifts so we can simulate
    "iteration 2 surfaces new gaps" or "iteration 2 is clean."
    """

    def __init__(self, batches):
        # `batches` is a list of List[Gap] — one entry per detect() call.
        # If detect() is called more times than batches has entries,
        # subsequent calls return the LAST batch (so we don't crash on
        # the orchestrator's final post-loop detect).
        self._batches = batches
        self._call_count = 0

    def detect(self, spec, context):
        idx = min(self._call_count, len(self._batches) - 1)
        self._call_count += 1
        return list(self._batches[idx])


class FakeSuggester:
    """Suggester that returns a pre-mapped Suggestion (or None) per Gap.id."""

    def __init__(self, mapping):
        self._mapping = mapping

    def suggest(self, gap, spec, context):
        return self._mapping.get(gap.id)


class FakeReviewer:
    """Reviewer that returns a pre-mapped ReviewResult per field_path."""

    def __init__(self, mapping):
        self._mapping = mapping

    def review(self, spec, context):
        return dict(self._mapping)


class FakeHandler:
    """Confirmation handler that returns scripted Resolutions in order."""

    def __init__(self, scripted):
        # `scripted` is a list of Resolution; consumed in present() order.
        self._scripted = list(scripted)
        self.calls = []

    def present(self, gap, suggestion):
        self.calls.append((gap, suggestion))
        if not self._scripted:
            # Default: skip everything if script runs out.
            return Resolution(action="skip", new_value=None,
                              user_action_provenance="user_skipped")
        return self._scripted.pop(0)


def fake_apply(spec, gap, resolution, suggestion):
    """Apply callback that records mutations on a list (no real spec)."""
    spec["applied"].append((gap.id, resolution.new_value))


def make_spec():
    """Minimal spec stand-in. Orchestrator only writes via apply_resolution
    (which we stub above), so we don't need a real ProtocolSpec."""
    return {"applied": []}


def gap(gid, kind="missing", severity="blocker", field_path=None):
    return Gap(
        id=gid,
        step_order=1,
        field_path=field_path or f"steps[0].{gid}",
        kind=kind,
        current_value=None,
        description=f"gap {gid}",
        severity=severity,
    )


def good_suggestion(value="filled", confidence=0.9):
    return Suggestion(
        value=value,
        provenance_source="deterministic",
        positive_reasoning="reason",
        why_not_in_instruction="missing from instruction",
        confidence=confidence,
    )


# ============================================================================
# Topological sort
# ============================================================================

class TestTopoSortGaps:
    """Upstream fields run before dependent ones within an iteration."""

    def test_temperature_before_source(self):
        # Temperature has priority 0; source has priority 1; topological
        # order puts temperature first.
        gaps = [
            gap("a", field_path="steps[0].source"),
            gap("b", field_path="steps[0].temperature"),
        ]
        sorted_gaps = topo_sort_gaps(gaps)
        assert sorted_gaps[0].id == "b"
        assert sorted_gaps[1].id == "a"

    def test_substance_before_source(self):
        gaps = [
            gap("a", field_path="steps[0].source"),
            gap("b", field_path="steps[0].substance"),
        ]
        sorted_gaps = topo_sort_gaps(gaps)
        assert sorted_gaps[0].id == "b"

    def test_stable_within_priority_bucket(self):
        # Two .source gaps: same priority, original order preserved.
        gaps = [
            gap("first", field_path="steps[0].source"),
            gap("second", field_path="steps[1].source"),
        ]
        sorted_gaps = topo_sort_gaps(gaps)
        assert [g.id for g in sorted_gaps] == ["first", "second"]


# ============================================================================
# Auto-accept rules
# ============================================================================

class TestAutoAccept:
    """Auto-accept iff suggestion exists, confidence >= threshold, kind not
    in ALWAYS_CONFIRM, reviewer didn't disagree."""

    def test_high_confidence_suggestion_auto_accepts(self):
        spec = make_spec()
        gaps = [gap("g1", kind="missing")]
        sug = good_suggestion(confidence=0.9)
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": sug})],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
            auto_accept_threshold=0.85,
        )
        outcome = orch.run(spec, context={})
        assert outcome.iterations[0].records[0].auto_accepted is True
        assert spec["applied"] == [("g1", "filled")]

    def test_low_confidence_falls_to_user(self):
        spec = make_spec()
        gaps = [gap("g1")]
        sug = good_suggestion(confidence=0.5)
        handler = FakeHandler([
            Resolution(action="accept_suggestion", new_value="filled",
                       user_action_provenance="user_accepted_suggestion"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": sug})],
            reviewer=None,
            handler=handler,
            apply_resolution=fake_apply,
            auto_accept_threshold=0.85,
        )
        outcome = orch.run(spec, context={})
        assert outcome.iterations[0].records[0].auto_accepted is False
        assert handler.calls  # user was prompted

    def test_skipped_persistent_gap_not_represented_or_looped(self):
        # A detector that keeps emitting the SAME gap every iteration (e.g. a
        # reviewer disagree from unchanged state). Skipping it must NOT re-ask
        # it to the iteration cap — seen-id dedup handles it once and converges.
        spec = make_spec()
        g = gap("persistent", kind="low_confidence", severity="quality")
        handler = FakeHandler([
            Resolution(action="skip", new_value=None,
                       user_action_provenance="user_skipped"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([[g]])],  # last batch repeats -> always [g]
            suggesters=[],
            reviewer=None,
            handler=handler,
            apply_resolution=fake_apply,
        )
        outcome = orch.run(spec, context={})
        assert len(handler.calls) == 1   # asked exactly once
        assert outcome.converged is True
        assert outcome.aborted is False

    def test_fabricated_kind_always_confirms(self):
        spec = make_spec()
        gaps = [gap("g1", kind="fabricated")]
        # Even with high confidence, fabricated gaps go to the user.
        sug = good_suggestion(confidence=0.99)
        handler = FakeHandler([
            Resolution(action="accept_suggestion", new_value="filled",
                       user_action_provenance="user_accepted_suggestion"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": sug})],
            reviewer=None,
            handler=handler,
            apply_resolution=fake_apply,
        )
        orch.run(spec, context={})
        assert handler.calls  # forced to user

    def test_reviewer_disagreement_blocks_auto_accept(self):
        spec = make_spec()
        gaps = [gap("g1")]
        sug = good_suggestion(confidence=0.95)
        review = ReviewResult(
            field_path=gaps[0].field_path,
            confirms_positive=False, confirms_negative=True,
            objection="instruction line 5 says X",
        )
        handler = FakeHandler([
            Resolution(action="edit", new_value="user-typed",
                       user_action_provenance="user_edited"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": sug})],
            reviewer=FakeReviewer({gaps[0].field_path: review}),
            handler=handler,
            apply_resolution=fake_apply,
        )
        orch.run(spec, context={})
        assert handler.calls  # reviewer objection forced to user

    def test_reviewer_objection_stamped_into_gap_metadata_before_present(self):
        # Reviewer disagreement → orchestrator copies the objection text into
        # gap.metadata["reviewer_objection"] before handing the gap to the
        # handler, so both the CLI and HTML handlers can render the
        # falsifier alongside the suggestion.
        spec = make_spec()
        gaps = [gap("g1")]
        sug = good_suggestion(confidence=0.95)
        review = ReviewResult(
            field_path=gaps[0].field_path,
            confirms_positive=False, confirms_negative=True,
            objection="instruction line 5 says X",
        )
        handler = FakeHandler([
            Resolution(action="skip", new_value=None,
                       user_action_provenance="user_skipped"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": sug})],
            reviewer=FakeReviewer({gaps[0].field_path: review}),
            handler=handler,
            apply_resolution=fake_apply,
        )
        orch.run(spec, context={})
        seen_gap, _ = handler.calls[0]
        assert seen_gap.metadata.get("reviewer_objection") == "instruction line 5 says X"

    def test_reviewer_agreement_leaves_metadata_clean(self):
        # When the reviewer confirms both claims, no objection exists,
        # nothing should be stamped onto gap.metadata. Even if auto-accept
        # would have fired (it does here for confidence ≥ threshold), the
        # gap never reaches present(); but if it did the metadata should
        # be empty.
        spec = make_spec()
        gaps = [gap("g1")]
        sug = good_suggestion(confidence=0.5)  # below auto-accept threshold
        review = ReviewResult(
            field_path=gaps[0].field_path,
            confirms_positive=True, confirms_negative=True,
            objection=None,
        )
        handler = FakeHandler([
            Resolution(action="skip", new_value=None,
                       user_action_provenance="user_skipped"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": sug})],
            reviewer=FakeReviewer({gaps[0].field_path: review}),
            handler=handler,
            apply_resolution=fake_apply,
        )
        orch.run(spec, context={})
        seen_gap, _ = handler.calls[0]
        assert "reviewer_objection" not in seen_gap.metadata

    def test_initial_contents_gap_gets_spotlight_prov_ids_stamped(self):
        # Phase 3b-3 (Group C): for initial-contents gaps, the orchestrator
        # stamps gap.metadata["spotlight_prov_ids"] with the prov-ids of
        # every spec cell that references the underlying labware/well, so
        # the modal can pulse those cells while the user decides.
        from types import SimpleNamespace as _NS
        from nl2protocol.gap_resolution.orchestrator import _stamp_spotlight_prov_ids

        # Minimal fake spec: one step whose source references a labware/well
        # that matches initial_contents[0].
        spec = _NS(
            initial_contents=[
                _NS(labware="tube rack", well="A1", volume_ul=None,
                    substance="sample"),
            ],
            steps=[
                _NS(
                    source=_NS(resolved_label="tube rack",
                                description="tube rack",
                                well="A1", wells=None, well_range=None),
                    destination=None,
                ),
            ],
        )
        g = Gap(
            id="ic0", step_order=None,
            field_path="initial_contents[0].volume_ul",
            kind="missing", current_value=None,
            description="vol missing", severity="blocker", metadata={},
        )
        _stamp_spotlight_prov_ids(g, spec)
        assert "s0-source" in g.metadata.get("spotlight_prov_ids", "")

    def test_step_level_gap_stamps_matching_cell_prov_id(self):
        # Phase 3j (#73): step-level gaps now spotlight their target
        # cell directly so the gap modal can anchor + draw attach
        # arrows. Field path "steps[N].volume" → prov-id "sN-volume".
        from types import SimpleNamespace as _NS
        from nl2protocol.gap_resolution.orchestrator import _stamp_spotlight_prov_ids

        spec = _NS(initial_contents=[], steps=[])
        g = Gap(
            id="s0v", step_order=0,
            field_path="steps[0].volume",
            kind="missing", current_value=None,
            description="vol missing", severity="blocker", metadata={},
        )
        _stamp_spotlight_prov_ids(g, spec)
        assert g.metadata.get("spotlight_prov_ids") == "s0-volume"

    def test_constraint_gap_with_affected_paths_stamps_all(self):
        # Phase 3j (#73): constraint-violation gaps carry affected_paths
        # in metadata (per dedupe — ADR-0010). Spotlight expands to
        # every affected step cell so the modal's dotted arrows reach
        # all of them.
        from types import SimpleNamespace as _NS
        from nl2protocol.gap_resolution.orchestrator import _stamp_spotlight_prov_ids

        spec = _NS(initial_contents=[], steps=[])
        g = Gap(
            id="cap", step_order=None,
            field_path="constraints.pipette_capacity",
            kind="constraint_violation", current_value=None,
            description="exceeds capacity", severity="blocker",
            metadata={"affected_paths": [
                "steps[2].volume", "steps[5].volume", "steps[7].volume",
            ]},
        )
        _stamp_spotlight_prov_ids(g, spec)
        got = g.metadata.get("spotlight_prov_ids", "").split()
        assert got == ["s2-volume", "s5-volume", "s7-volume"]

    def test_unrenderable_field_path_no_spotlight(self):
        # Defensive: field_path matching steps[N].<thing> where <thing>
        # isn't a renderer-exposed cell (e.g., steps[0].action) is a
        # silent no-op — no spotlight rather than a stamp pointing at
        # nothing.
        from types import SimpleNamespace as _NS
        from nl2protocol.gap_resolution.orchestrator import _stamp_spotlight_prov_ids

        spec = _NS(initial_contents=[], steps=[])
        g = Gap(
            id="s0a", step_order=0,
            field_path="steps[0].action",
            kind="missing", current_value=None,
            description="action missing", severity="blocker", metadata={},
        )
        _stamp_spotlight_prov_ids(g, spec)
        assert "spotlight_prov_ids" not in g.metadata

    def test_spotlight_helper_gracefully_handles_oob_index(self):
        # Defensive: malformed gap (index past initial_contents length)
        # should no-op, not crash. UX hint, not load-bearing.
        from types import SimpleNamespace as _NS
        from nl2protocol.gap_resolution.orchestrator import _stamp_spotlight_prov_ids

        spec = _NS(initial_contents=[], steps=[])
        g = Gap(
            id="ic9", step_order=None,
            field_path="initial_contents[9].volume_ul",
            kind="missing", current_value=None,
            description="vol missing", severity="blocker", metadata={},
        )
        _stamp_spotlight_prov_ids(g, spec)
        assert "spotlight_prov_ids" not in g.metadata

    def test_no_reviewer_means_no_objection_metadata(self):
        # Defensive: when no reviewer is configured, nothing should land
        # in gap.metadata under reviewer_objection. The orchestrator's
        # stamp pass should be a no-op.
        spec = make_spec()
        gaps = [gap("g1", severity="quality")]
        sug = good_suggestion(confidence=0.5)
        handler = FakeHandler([
            Resolution(action="skip", new_value=None,
                       user_action_provenance="user_skipped"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": sug})],
            reviewer=None,
            handler=handler,
            apply_resolution=fake_apply,
        )
        orch.run(spec, context={})
        seen_gap, _ = handler.calls[0]
        assert "reviewer_objection" not in seen_gap.metadata


# ============================================================================
# Loop convergence + abort
# ============================================================================

class TestLoopBehavior:
    """Re-detect after each iteration; loop terminates on convergence,
    abort, or iteration cap."""

    def test_converges_when_no_gaps(self):
        spec = make_spec()
        # First detect returns gaps; suggestions auto-accept; second
        # detect returns nothing (clean).
        gaps = [gap("g1")]
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": good_suggestion(0.95)})],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
        )
        outcome = orch.run(spec, context={})
        assert outcome.converged is True
        assert outcome.aborted is False
        assert len(outcome.iterations) == 1

    def test_user_abort_halts_loop(self):
        spec = make_spec()
        gaps = [gap("g1")]
        handler = FakeHandler([
            Resolution(action="abort", new_value=None,
                       user_action_provenance="user_aborted"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([gaps])],
            suggesters=[FakeSuggester({"g1": good_suggestion(confidence=0.5)})],
            reviewer=None,
            handler=handler,
            apply_resolution=fake_apply,
        )
        outcome = orch.run(spec, context={})
        assert outcome.aborted is True
        assert outcome.converged is False

    def test_iteration_cap_terminates(self):
        spec = make_spec()
        # A genuinely non-converging run: each iteration surfaces a NEW gap
        # (distinct id) that the user skips. Seen-id dedup retires each old
        # gap, but new ones keep appearing, so the run hits the cap without
        # converging. (A *repeated* same-id gap would instead be deduped and
        # converge — see test_skipped_persistent_gap_not_represented_or_looped.)
        handler = FakeHandler([])  # default: skip everything
        orch = Orchestrator(
            detectors=[FakeDetector([[gap("g1")], [gap("g2")],
                                     [gap("g3")], [gap("g4")]])],
            suggesters=[FakeSuggester({})],
            reviewer=None,
            handler=handler,
            apply_resolution=fake_apply,
            max_iterations=3,
        )
        outcome = orch.run(spec, context={})
        # Hit the cap; loop terminated without convergence.
        assert len(outcome.iterations) == 3
        assert outcome.converged is False

    def test_re_detect_picks_up_new_gaps(self):
        spec = make_spec()
        # Iteration 1: gap g1. Iteration 2: gap g2 (cascading).
        # Iteration 3: clean.
        orch = Orchestrator(
            detectors=[FakeDetector([
                [gap("g1")],
                [gap("g2")],
                [],
            ])],
            suggesters=[FakeSuggester({
                "g1": good_suggestion(value="v1", confidence=0.95),
                "g2": good_suggestion(value="v2", confidence=0.95),
            })],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
        )
        outcome = orch.run(spec, context={})
        assert outcome.converged is True
        assert spec["applied"] == [("g1", "v1"), ("g2", "v2")]


# ============================================================================
# Suggester precedence
# ============================================================================

class TestSuggesterPrecedence:
    """First non-None Suggestion wins. Subsequent suggesters don't run for
    that gap."""

    def test_first_suggester_wins(self):
        spec = make_spec()
        gaps = [gap("g1")]
        s1 = FakeSuggester({"g1": good_suggestion(value="from_first", confidence=0.9)})
        s2 = FakeSuggester({"g1": good_suggestion(value="from_second", confidence=0.9)})
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[s1, s2],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
        )
        orch.run(spec, context={})
        assert spec["applied"] == [("g1", "from_first")]

    def test_falls_through_when_first_returns_none(self):
        spec = make_spec()
        gaps = [gap("g1")]
        s1 = FakeSuggester({})  # returns None
        s2 = FakeSuggester({"g1": good_suggestion(value="from_second", confidence=0.9)})
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[s1, s2],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
        )
        orch.run(spec, context={})
        assert spec["applied"] == [("g1", "from_second")]


# ============================================================================
# Records / observability
# ============================================================================

class TestRecords:
    """Each Gap gets a GapResolutionRecord per iteration capturing the full
    lifecycle (suggestion + review + resolution + auto_accepted)."""

    def test_record_captures_auto_accept(self):
        spec = make_spec()
        gaps = [gap("g1")]
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": good_suggestion(confidence=0.9)})],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
        )
        outcome = orch.run(spec, context={})
        record = outcome.iterations[0].records[0]
        assert record.gap.id == "g1"
        assert record.suggestion is not None
        assert record.auto_accepted is True
        assert record.resolution is not None
        assert record.resolution.action == "accept_suggestion"

    def test_record_captures_user_edit(self):
        spec = make_spec()
        gaps = [gap("g1")]
        handler = FakeHandler([
            Resolution(action="edit", new_value="custom",
                       user_action_provenance="user_edited"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": good_suggestion(confidence=0.5)})],
            reviewer=None,
            handler=handler,
            apply_resolution=fake_apply,
        )
        outcome = orch.run(spec, context={})
        record = outcome.iterations[0].records[0]
        assert record.auto_accepted is False
        assert record.resolution.action == "edit"
        assert record.resolution.new_value == "custom"


# ============================================================================
# Reviewer-verdict stamping (ADR-0009)
# ============================================================================

def _real_spec():
    """Build a real ProtocolSpec with non-instruction Provenances on
    volume / source / destination so the stamp helper has fields to mutate.
    """
    from nl2protocol.models.spec import (
        CompositionProvenance, ExtractedStep, LocationRef,
        InferredProvenance, ProtocolSpec, ProvenancedVolume,
    )

    def _inferred_prov(reasoning="some reasoning",
                       why_not="instruction omitted this"):
        return InferredProvenance(
            source="inferred",
            positive_reasoning=reasoning,
            why_not_in_instruction=why_not,
            confidence=0.9,
        )

    comp = CompositionProvenance(
        step_cited_text="t",
        parameters_cited_texts=["t"],
        parameters_reasoning="t",
        grounding=["instruction"],
        confidence=1.0,
    )
    step = ExtractedStep(
        order=1, action="transfer",
        volume=ProvenancedVolume(
            value=50.0, unit="uL", exact=True,
            provenance=_inferred_prov("inferred 50uL", "instruction lacks the volume"),
        ),
        source=LocationRef(
            description="src", well="A1",
            description_provenance=_inferred_prov("config lookup", "instruction omits source"), wells_provenance=_inferred_prov("config lookup", "instruction omits source"),
        ),
        destination=LocationRef(
            description="dst", well="B1",
            description_provenance=_inferred_prov("dest from context", "instruction omits dest"), wells_provenance=_inferred_prov("dest from context", "instruction omits dest"),
        ),
        composition_provenance=comp,
    )
    return ProtocolSpec(summary="t", steps=[step])


class TestStampReviewerVerdicts:
    """Walk spec; for each Provenance whose field_path appears in `reviews`,
    stamp review_status (and reviewer_objection on disagreement)."""

    def test_agreement_stamps_reviewed_agree_no_objection(self):
        spec = _real_spec()
        reviews = {
            "steps[0].volume": ReviewResult(
                field_path="steps[0].volume",
                confirms_positive=True, confirms_negative=True,
                objection=None,
            ),
        }
        stamp_reviewer_verdicts(spec, reviews)
        prov = spec.steps[0].volume.provenance
        assert prov.review_status == "reviewed_agree"
        assert prov.reviewer_objection is None

    def test_disagreement_stamps_reviewed_disagree_with_objection(self):
        spec = _real_spec()
        reviews = {
            "steps[0].source": ReviewResult(
                field_path="steps[0].source",
                confirms_positive=True, confirms_negative=False,
                objection="instruction line 3 names this source verbatim",
            ),
        }
        stamp_reviewer_verdicts(spec, reviews)
        prov = spec.steps[0].source.description_provenance
        assert prov.review_status == "reviewed_disagree"
        assert prov.reviewer_objection == "instruction line 3 names this source verbatim"
        # The verdict propagates to both LocationRef slots.
        assert spec.steps[0].source.wells_provenance.review_status == "reviewed_disagree"

    def test_disagreement_on_positive_also_stamps_disagree(self):
        # Either-claim disagreement → reviewed_disagree (not just both-disagree).
        spec = _real_spec()
        reviews = {
            "steps[0].volume": ReviewResult(
                field_path="steps[0].volume",
                confirms_positive=False, confirms_negative=True,
                objection="50uL is not standard for this protocol",
            ),
        }
        stamp_reviewer_verdicts(spec, reviews)
        assert spec.steps[0].volume.provenance.review_status == "reviewed_disagree"

    def test_field_not_in_reviews_left_untouched(self):
        spec = _real_spec()
        # Only stamp volume; source + destination should keep their original status.
        reviews = {
            "steps[0].volume": ReviewResult(
                field_path="steps[0].volume",
                confirms_positive=True, confirms_negative=True,
                objection=None,
            ),
        }
        stamp_reviewer_verdicts(spec, reviews)
        assert spec.steps[0].volume.provenance.review_status == "reviewed_agree"
        assert spec.steps[0].source.description_provenance.review_status == "original"
        assert spec.steps[0].destination.description_provenance.review_status == "original"

    def test_empty_reviews_leaves_spec_untouched(self):
        spec = _real_spec()
        stamp_reviewer_verdicts(spec, reviews={})
        assert spec.steps[0].volume.provenance.review_status == "original"
        for fname in ("source", "destination"):
            ref = getattr(spec.steps[0], fname)
            assert ref.description_provenance.review_status == "original"
            if ref.wells_provenance:
                assert ref.wells_provenance.review_status == "original"

    def test_user_action_statuses_are_terminal_and_not_overwritten(self):
        """User-action statuses (user_accepted_suggestion, user_edited,
        user_confirmed, user_skipped, user_overrode_fabrication) are
        TERMINAL — once the user has acted on a slot, the reviewer pass
        must NOT overwrite that decision. Without this guard, a reviewer
        pass that fires after a pre-orchestrator modal closes would
        silently flip review_status away from the user-action status,
        masking the user's choice and burying any objection in the
        provenance audit trail only."""
        from nl2protocol.models.spec import validate_provenance
        for terminal in (
            "user_confirmed", "user_edited", "user_accepted_suggestion",
            "user_skipped", "user_overrode_fabrication",
        ):
            spec = _real_spec()
            # Stamp the user-action status onto volume's provenance.
            existing = spec.steps[0].volume.provenance
            spec.steps[0].volume.provenance = validate_provenance({
                **existing.model_dump(),
                "review_status": terminal,
            })
            reviews = {
                "steps[0].volume": ReviewResult(
                    field_path="steps[0].volume",
                    confirms_positive=False, confirms_negative=False,
                    objection="reviewer disagrees",
                ),
            }
            stamp_reviewer_verdicts(spec, reviews)
            assert spec.steps[0].volume.provenance.review_status == terminal, (
                f"{terminal} got overwritten")

    def test_instruction_sourced_slot_not_stamped_via_sibling(self):
        """When the reviewer judges a LocationRef as a whole (returning
        a verdict keyed on `steps[N].source`), the stamp must ONLY
        touch slots whose source != "instruction". The reviewer itself
        skips instruction-sourced provenances when collecting claims,
        so propagating a verdict onto an instruction-sourced sibling
        slot creates a false audit trail ("reviewed_agree" on a slot
        no model ever read)."""
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, LocationRef,
            InferredProvenance, InstructionProvenance, ProtocolSpec,
        )
        comp = CompositionProvenance(
            step_cited_text="t", parameters_cited_texts=["t"],
            parameters_reasoning="t", grounding=["instruction"], confidence=1.0,
        )
        # description_provenance: inferred (gets reviewed).
        # wells_provenance: instruction-sourced (must NOT be stamped).
        step = ExtractedStep(
            order=1, action="transfer",
            source=LocationRef(
                description="src", well="A1",
                description_provenance=InferredProvenance(
                    source="inferred",
                    positive_reasoning="config lookup",
                    why_not_in_instruction="instruction omits source",
                    confidence=0.9,
                ),
                wells_provenance=InstructionProvenance(
                    source="instruction",
                    cited_text=["A1"],
                    confidence=1.0,
                ),
            ),
            composition_provenance=comp,
        )
        spec = ProtocolSpec(summary="t", steps=[step])
        reviews = {
            "steps[0].source": ReviewResult(
                field_path="steps[0].source",
                confirms_positive=True, confirms_negative=True,
                objection=None,
            ),
        }
        stamp_reviewer_verdicts(spec, reviews)
        # Description slot got the verdict (inferred → reviewable).
        assert spec.steps[0].source.description_provenance.review_status == "reviewed_agree"
        # Wells slot stayed "original" — never reviewed, never stamped.
        assert spec.steps[0].source.wells_provenance.review_status == "original"

    def test_orchestrator_wires_stamp_after_review(self):
        # Wire-level test: the orchestrator's run() invokes stamp after the
        # reviewer pass, so the spec carries reviewer state by the time the
        # outcome is returned.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = _real_spec()
        review = ReviewResult(
            field_path="steps[0].volume",
            confirms_positive=False, confirms_negative=True,
            objection="re-check needed",
        )
        # Suggester returns None (so no auto-accept path); user skips the gap.
        # The stamp happens regardless of how the gap is resolved.
        gaps = [gap("g1", field_path="steps[0].volume")]
        handler = FakeHandler([
            Resolution(action="skip", new_value=None,
                       user_action_provenance="user_skipped"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({})],   # no suggestion
            reviewer=FakeReviewer({"steps[0].volume": review}),
            handler=handler,
            apply_resolution=default_apply_resolution,
        )
        orch.run(spec, context={})
        prov = spec.steps[0].volume.provenance
        assert prov.review_status == "reviewed_disagree"
        assert prov.reviewer_objection == "re-check needed"


# ============================================================================
# default_apply_resolution stamps user-action provenance (ADR-0009)
# ============================================================================

class TestDefaultApplyStampsUserAction:
    """default_apply_resolution writes the new value AND stamps
    review_status from resolution.user_action_provenance onto the
    resulting Provenance, clearing reviewer_objection in the process."""

    def _suggested_volume(self, value=75.0):
        from nl2protocol.models.spec import InferredProvenance, ProvenancedVolume
        return ProvenancedVolume(
            value=value, unit="uL", exact=True,
            provenance=InferredProvenance(
                source="inferred",
                positive_reasoning="suggester proposed this",
                why_not_in_instruction="instruction lacks the volume",
                confidence=0.9,
            ),
        )

    def _suggested_location(self, well="C3"):
        from nl2protocol.models.spec import LocationRef, InferredProvenance
        return LocationRef(
            description="config-found", well=well,
            description_provenance=InferredProvenance(
                source="inferred",
                positive_reasoning="config lookup",
                why_not_in_instruction="instruction omits source",
                confidence=0.9,
            ), wells_provenance=InferredProvenance(
                source="inferred",
                positive_reasoning="config lookup",
                why_not_in_instruction="instruction omits source",
                confidence=0.9,
            ),
        )

    def test_accept_suggestion_replaces_field_and_stamps(self):
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = _real_spec()
        new_volume = self._suggested_volume(value=75.0)
        g = gap("g1", field_path="steps[0].volume")
        res = Resolution(action="accept_suggestion", new_value=new_volume,
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(spec, g, res, suggestion=None)
        # Field replaced
        assert spec.steps[0].volume.value == 75.0
        # Provenance stamped
        assert spec.steps[0].volume.provenance.review_status == "user_accepted_suggestion"
        # Reviewer state cleared
        assert spec.steps[0].volume.provenance.reviewer_objection is None

    def test_edit_mutates_value_preserves_provenance_shape(self):
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = _real_spec()
        # Capture the model identity so we can prove .value was mutated
        # rather than the whole object replaced.
        original_volume_obj = spec.steps[0].volume
        g = gap("g1", field_path="steps[0].volume")
        res = Resolution(action="edit", new_value=42.0,
                         user_action_provenance="user_edited")
        default_apply_resolution(spec, g, res, suggestion=None)
        # Same Pydantic model instance; only .value changed.
        assert spec.steps[0].volume is original_volume_obj
        assert spec.steps[0].volume.value == 42.0
        # Provenance stamped
        assert spec.steps[0].volume.provenance.review_status == "user_edited"

    def test_accept_count_suggestion_stamps_repetitions_provenance(self):
        # repetitions is a bare scalar with a sibling _provenance field: accept
        # must record WHERE the count came from, not just set the int — else the
        # report shows an unattributed count.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        from nl2protocol.gap_resolution import MixCycleCountSuggester
        spec = _real_spec()
        g = gap("g1", field_path="steps[0].repetitions")
        sugg = MixCycleCountSuggester().suggest(g, spec, {})
        res = Resolution(action="accept_suggestion", new_value=sugg.value,
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(spec, g, res, suggestion=sugg)
        assert spec.steps[0].repetitions == sugg.value
        assert spec.steps[0].repetitions_provenance is not None
        assert spec.steps[0].repetitions_provenance.review_status == "user_accepted_suggestion"

    def test_edit_count_stamps_repetitions_provenance_user_edited(self):
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = _real_spec()
        g = gap("g1", field_path="steps[0].repetitions")
        res = Resolution(action="edit", new_value=7,
                         user_action_provenance="user_edited")
        default_apply_resolution(spec, g, res, suggestion=None)
        assert spec.steps[0].repetitions == 7
        assert spec.steps[0].repetitions_provenance is not None
        assert spec.steps[0].repetitions_provenance.review_status == "user_edited"

    def test_user_action_supersedes_prior_reviewer_objection(self):
        # If the reviewer disagreed with a value, then the user accepts/edits
        # anyway, the resulting Provenance reflects the user's action and
        # drops the reviewer's objection (audit trail in GapResolutionRecord).
        from nl2protocol.gap_resolution.orchestrator import (
            default_apply_resolution, stamp_reviewer_verdicts,
        )
        spec = _real_spec()
        # First simulate the reviewer disagreeing with the volume.
        stamp_reviewer_verdicts(spec, {
            "steps[0].volume": ReviewResult(
                field_path="steps[0].volume",
                confirms_positive=False, confirms_negative=True,
                objection="value seems off",
            ),
        })
        assert spec.steps[0].volume.provenance.reviewer_objection == "value seems off"
        # Now the user accepts the suggester's value.
        new_volume = self._suggested_volume(value=80.0)
        res = Resolution(action="accept_suggestion", new_value=new_volume,
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(
            spec, gap("g1", field_path="steps[0].volume"), res, suggestion=None,
        )
        prov = spec.steps[0].volume.provenance
        assert prov.review_status == "user_accepted_suggestion"
        assert prov.reviewer_objection is None

    def test_subfield_write_stamps_parent_provenance(self):
        # steps[0].destination.wells write — Phase 3c fix-2: editing a
        # value subfield replaces ONLY the corresponding provenance
        # (wells_provenance for wells/well/well_range; description_provenance
        # for description). The OTHER provenance slots on the LocationRef
        # stay as they were — the user didn't edit the description, so
        # description_provenance.review_status keeps its prior value.
        # (Pre-3c: _stamp_user_action broad-stamped every prov slot;
        # that conflated "user touched part of this object" with "user
        # touched every part" and is no longer the contract.)
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = _real_spec()
        new_wells = ["B2", "B3"]
        g = gap("g1", field_path="steps[0].destination.wells")
        res = Resolution(action="edit", new_value=new_wells,
                         user_action_provenance="user_edited")
        default_apply_resolution(spec, g, res, suggestion=None)
        assert spec.steps[0].destination.wells == new_wells
        # Wells provenance is REPLACED with an inferred "user edited" prov.
        assert spec.steps[0].destination.wells_provenance.source == "inferred"
        assert spec.steps[0].destination.wells_provenance.review_status == "user_edited"
        # Description provenance is untouched — the user didn't edit description.
        assert spec.steps[0].destination.description_provenance.review_status == "original"

    def test_cited_suggestion_on_fabrication_path_stamps_instruction_source(self):
        """When the orchestrator accepts a Suggestion with provenance_source='cited'
        on a fabrication-shaped gap (path ends in '.provenance'), the new
        Provenance on the spec field is source='instruction' with cited_text
        carrying the substring the LLM identified — NOT source='inferred'.

        Closes the seam between the suggester-internal 'cited' label and the
        spec-level Provenance.source so cited values render in the report
        the same way as extractor-sourced citations (col-1 cite span hue
        linked to col-3 value)."""
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = _real_spec()
        # Fabrication-shaped gap: the provenance slot is broken, not the value.
        g = gap("g1", field_path="steps[0].volume.provenance")
        suggestion = Suggestion(
            value=75.0,
            provenance_source="cited",
            positive_reasoning="LLM identified 75uL in instruction",
            why_not_in_instruction=None,
            confidence=0.95,
            cited_text="75uL of buffer",
        )
        res = Resolution(action="accept_suggestion", new_value=None,
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(spec, g, res, suggestion=suggestion)

        prov = spec.steps[0].volume.provenance
        assert prov.source == "instruction"
        assert prov.cited_text == ["75uL of buffer"]
        assert prov.review_status == "user_accepted_suggestion"
        # Value-field also written (the bridge path writes both).
        assert spec.steps[0].volume.value == 75.0

    def test_uncited_suggestion_on_fabrication_path_still_stamps_inferred(self):
        """Regression guard: when Suggestion.provenance_source != 'cited',
        the apply path's behavior is unchanged — source='inferred' with the
        suggester's reasoning is stamped onto the new Provenance."""
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = _real_spec()
        g = gap("g1", field_path="steps[0].volume.provenance")
        suggestion = Suggestion(
            value=75.0,
            provenance_source="inferred",
            positive_reasoning="standard SDS-PAGE loading volume",
            why_not_in_instruction="instruction omits the per-lane volume",
            confidence=0.7,
            cited_text=None,
        )
        res = Resolution(action="accept_suggestion", new_value=None,
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(spec, g, res, suggestion=suggestion)

        prov = spec.steps[0].volume.provenance
        assert prov.source == "inferred"
        assert prov.positive_reasoning == "standard SDS-PAGE loading volume"
        assert not hasattr(prov, "cited_text")

    def test_cited_suggestion_on_subfield_path_stamps_instruction_source(self):
        """Same bridge, exercised through the subfield path
        (steps[N].<field>.<subfield>). When the user accepts a cited
        suggestion for a wells edit, the resulting wells_provenance is
        source='instruction' with the LLM's cited substring."""
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = _real_spec()
        new_wells = ["C1", "C2"]
        g = gap("g1", field_path="steps[0].destination.wells")
        suggestion = Suggestion(
            value=new_wells,
            provenance_source="cited",
            positive_reasoning="wells C1 and C2 named in instruction",
            why_not_in_instruction=None,
            confidence=0.9,
            cited_text="transfer to wells C1 and C2",
        )
        res = Resolution(action="accept_suggestion", new_value=new_wells,
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(spec, g, res, suggestion=suggestion)

        wprov = spec.steps[0].destination.wells_provenance
        assert wprov.source == "instruction"
        assert wprov.cited_text == ["transfer to wells C1 and C2"]
        assert spec.steps[0].destination.wells == new_wells

    def test_initial_contents_volume_writes_float_no_provenance_changes(self):
        # initial_contents.volume_ul has no Provenance — the apply just
        # writes a float and returns. Nothing else mutated.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, Provenance,
            ProtocolSpec, ProvenancedVolume, WellContents,
        )
        spec = ProtocolSpec(
            summary="t",
            steps=[ExtractedStep(
                order=1, action="comment", note="placeholder",
                composition_provenance=CompositionProvenance(
                    step_cited_text="t", parameters_cited_texts=["t"],
                    parameters_reasoning="t", grounding=["instruction"],
                    confidence=1.0,
                ),
            )],
            initial_contents=[
                WellContents(labware="rack", well="A1", substance="x", volume_ul=None),
            ],
        )
        g = gap("g1", field_path="initial_contents[0].volume_ul")
        res = Resolution(action="edit", new_value=200.0,
                         user_action_provenance="user_edited")
        # Should not raise (even though there's no Provenance to stamp).
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        default_apply_resolution(spec, g, res, suggestion=None)
        assert spec.initial_contents[0].volume_ul == 200.0

    def test_initial_contents_well_writes_str_and_pushes_revision(self):
        # Symmetric with volume_ul: writing initial_contents[N].well sets
        # the well string and snapshots the prior state onto prior_revisions.
        # Without this branch the apply path silently no-ops and the
        # InitialContentsWellDetector re-detects the same gap forever.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, ProtocolSpec, WellContents,
        )
        spec = ProtocolSpec(
            summary="t",
            steps=[ExtractedStep(
                order=1, action="comment", note="placeholder",
                composition_provenance=CompositionProvenance(
                    step_cited_text="t", parameters_cited_texts=["t"],
                    parameters_reasoning="t", grounding=["instruction"],
                    confidence=1.0,
                ),
            )],
            initial_contents=[
                WellContents(labware="rack", well=None, substance="x",
                              volume_ul=None),
            ],
        )
        g = gap("g1", field_path="initial_contents[0].well")
        res = Resolution(action="accept_suggestion", new_value="A1",
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(spec, g, res, suggestion=None)
        wc = spec.initial_contents[0]
        assert wc.well == "A1"
        assert len(wc.prior_revisions) == 1
        assert wc.prior_revisions[0].well is None


# ============================================================================
# PR3a step 3: resolved_label routing in apply + reviewer + claim collection
# ============================================================================

def _real_spec_with_resolution_provenance():
    """Spec where source/destination LocationRefs already carry a
    resolved_label_provenance (as if the LabwareResolver picked them).
    Used to exercise the reviewer + stamp + apply paths that route via
    resolved_label_provenance instead of the LocationRef's primary provenance."""
    from nl2protocol.models.spec import (
        CompositionProvenance, ExtractedStep, LocationRef,
        InferredProvenance, InstructionProvenance, ProtocolSpec, ProvenancedVolume,
    )
    instr_prov = InstructionProvenance(
        source="instruction", cited_text="A1", confidence=1.0,
    )
    res_prov = InferredProvenance(
        source="inferred",
        positive_reasoning="resolver picked sample_rack for description 'rack'",
        why_not_in_instruction="user wrote 'rack' rather than 'sample_rack' literally",
        confidence=0.85,
    )
    comp = CompositionProvenance(
        step_cited_text="t", parameters_cited_texts=["t"],
        parameters_reasoning="t", grounding=["instruction"], confidence=1.0,
    )
    return ProtocolSpec(summary="t", steps=[ExtractedStep(
        order=1, action="transfer",
        volume=ProvenancedVolume(value=10.0, unit="uL", exact=True,
                                  provenance=instr_prov),
        source=LocationRef(
            description="rack", well="A1",
            resolved_label="sample_rack",
            description_provenance=instr_prov, wells_provenance=instr_prov,
            resolved_label_provenance=res_prov,
        ),
        destination=LocationRef(
            description="rack", well="B1",
            resolved_label="sample_rack",
            description_provenance=instr_prov, wells_provenance=instr_prov,
            resolved_label_provenance=res_prov,
        ),
        composition_provenance=comp,
    )])


class TestApplyResolutionForResolvedLabel:
    """default_apply_resolution routes `*.resolved_label` writes to
    `_stamp_resolution_action`, which stamps `resolved_label_provenance`
    rather than the LocationRef's primary `provenance`."""

    def test_user_picks_label_stamps_resolved_label_provenance(self):
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = _real_spec_with_resolution_provenance()
        # User picked a different config label than what the resolver had.
        g = gap("g1", field_path="steps[0].source.resolved_label")
        res = Resolution(action="accept_suggestion",
                         new_value="reagent_reservoir",
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(spec, g, res, suggestion=None)
        # resolved_label updated
        assert spec.steps[0].source.resolved_label == "reagent_reservoir"
        # resolved_label_provenance carries the user action
        rprov = spec.steps[0].source.resolved_label_provenance
        assert rprov.review_status == "user_accepted_suggestion"
        # Primary provenance is NOT touched (it's about the location, not the resolution)
        assert spec.steps[0].source.description_provenance.review_status == "original"

    def test_user_pick_when_no_prior_resolution_provenance(self):
        # If the resolver didn't run (no resolved_label_provenance set),
        # the apply should construct a fresh Provenance for the user's pick.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, LocationRef,
            InstructionProvenance, ProtocolSpec, ProvenancedVolume,
        )
        instr_prov = InstructionProvenance(source="instruction", cited_text="A1", confidence=1.0)
        comp = CompositionProvenance(
            step_cited_text="t", parameters_cited_texts=["t"],
            parameters_reasoning="t", grounding=["instruction"], confidence=1.0,
        )
        spec = ProtocolSpec(summary="t", steps=[ExtractedStep(
            order=1, action="transfer",
            volume=ProvenancedVolume(value=10.0, unit="uL", exact=True,
                                      provenance=instr_prov),
            source=LocationRef(description="rack", well="A1",
                                description_provenance=instr_prov, wells_provenance=instr_prov),
            destination=LocationRef(description="plate", well="B1",
                                     description_provenance=instr_prov, wells_provenance=instr_prov),
            composition_provenance=comp,
        )])
        g = gap("g1", field_path="steps[0].source.resolved_label")
        res = Resolution(action="accept_suggestion",
                         new_value="sample_rack",
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(spec, g, res, suggestion=None)
        rprov = spec.steps[0].source.resolved_label_provenance
        assert rprov is not None
        assert rprov.review_status == "user_accepted_suggestion"
        assert "sample_rack" in rprov.positive_reasoning


class TestStampReviewerVerdictsForResolvedLabel:
    """stamp_reviewer_verdicts also walks resolved_label_provenance — labware
    resolution claims get the same reviewer treatment as inferred spec values."""

    def test_agreement_stamps_resolved_label_provenance(self):
        spec = _real_spec_with_resolution_provenance()
        reviews = {
            "steps[0].source.resolved_label": ReviewResult(
                field_path="steps[0].source.resolved_label",
                confirms_positive=True, confirms_negative=True,
                objection=None,
            ),
        }
        stamp_reviewer_verdicts(spec, reviews)
        rprov = spec.steps[0].source.resolved_label_provenance
        assert rprov.review_status == "reviewed_agree"
        # Primary provenance untouched.
        assert spec.steps[0].source.description_provenance.review_status == "original"

    def test_disagreement_stamps_resolved_label_provenance_with_objection(self):
        spec = _real_spec_with_resolution_provenance()
        reviews = {
            "steps[0].destination.resolved_label": ReviewResult(
                field_path="steps[0].destination.resolved_label",
                confirms_positive=True, confirms_negative=False,
                objection="config has both sample_rack and bsa_rack — ambiguous",
            ),
        }
        stamp_reviewer_verdicts(spec, reviews)
        rprov = spec.steps[0].destination.resolved_label_provenance
        assert rprov.review_status == "reviewed_disagree"
        assert "ambiguous" in rprov.reviewer_objection


# ============================================================================
# ADR-0011 Phase 1: orchestrator emits storytelling events
# ============================================================================

class TestOrchestratorEmitsStorytellingEvents:
    """When a Reporter is wired, the orchestrator emits gap_iteration_start,
    gap_detected (one per gap, in topo order), gap_resolved (one per gap,
    with resolution_kind reflecting the resolution), and gap_iteration_end
    (with resolved_count + remaining)."""

    def _capturing(self):
        from nl2protocol.reporting import CapturingReporter
        return CapturingReporter()

    def test_emits_iteration_start_with_gap_count(self):
        spec = make_spec()
        reporter = self._capturing()
        gaps = [gap("g1"), gap("g2")]
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": good_suggestion(0.95),
                                        "g2": good_suggestion(0.95)})],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
            reporter=reporter,
        )
        orch.run(spec, context={})
        starts = reporter.events_of_kind("gap_iteration_start")
        assert len(starts) == 1
        assert starts[0].data["iteration"] == 1
        assert starts[0].data["gap_count"] == 2

    def test_emits_gap_detected_per_gap_in_topo_order(self):
        spec = make_spec()
        reporter = self._capturing()
        # Two gaps; topo_sort_gaps puts .temperature (priority 0) before
        # .source (priority 1). Detected order should reflect topo order.
        gaps = [
            gap("a", field_path="steps[0].source"),
            gap("b", field_path="steps[0].temperature"),
        ]
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"a": good_suggestion(0.95),
                                        "b": good_suggestion(0.95)})],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
            reporter=reporter,
        )
        orch.run(spec, context={})
        detected = reporter.events_of_kind("gap_detected")
        ids_in_order = [e.data["gap_id"] for e in detected]
        # Temperature first (priority 0), then source (priority 1).
        assert ids_in_order == ["b", "a"]
        # Each carries the right metadata.
        assert detected[0].data["field_path"] == "steps[0].temperature"
        assert detected[0].data["gap_kind"] == "missing"
        assert detected[0].data["severity"] == "blocker"

    def test_gap_resolved_kind_auto_accepted_when_orchestrator_skips_handler(self):
        spec = make_spec()
        reporter = self._capturing()
        gaps = [gap("g1")]
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": good_suggestion(0.95)})],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
            reporter=reporter,
        )
        orch.run(spec, context={})
        resolved = reporter.events_of_kind("gap_resolved")
        assert len(resolved) == 1
        assert resolved[0].data["resolution_kind"] == "auto_accepted"
        assert resolved[0].data["auto_accepted"] is True
        assert resolved[0].data["gap_id"] == "g1"

    def test_gap_resolved_kind_user_accepted_when_user_takes_suggestion(self):
        spec = make_spec()
        reporter = self._capturing()
        gaps = [gap("g1")]
        handler = FakeHandler([
            Resolution(action="accept_suggestion", new_value="filled",
                       user_action_provenance="user_accepted_suggestion"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": good_suggestion(confidence=0.5)})],
            reviewer=None,
            handler=handler,
            apply_resolution=fake_apply,
            reporter=reporter,
        )
        orch.run(spec, context={})
        resolved = reporter.events_of_kind("gap_resolved")
        assert len(resolved) == 1
        assert resolved[0].data["resolution_kind"] == "user_accepted_suggestion"
        assert resolved[0].data["auto_accepted"] is False

    def test_gap_resolved_kind_user_skipped_does_not_count_as_resolved(self):
        # Skipping leaves the gap unresolved; the gap_iteration_end event's
        # resolved_count reflects skipped gaps as not-resolved.
        spec = make_spec()
        reporter = self._capturing()
        gaps = [gap("g1"), gap("g2")]
        handler = FakeHandler([
            Resolution(action="skip", new_value=None,
                       user_action_provenance="user_skipped"),
            Resolution(action="skip", new_value=None,
                       user_action_provenance="user_skipped"),
        ])
        # Use FakeDetector with 4 batches so re-detect after skip doesn't
        # crash on iterating past the cap.
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, gaps, gaps, gaps])],
            suggesters=[FakeSuggester({})],
            reviewer=None,
            handler=handler,
            apply_resolution=fake_apply,
            reporter=reporter,
            max_iterations=1,
        )
        orch.run(spec, context={})
        resolved = reporter.events_of_kind("gap_resolved")
        # Two gaps both surface and both get skipped; both fire gap_resolved
        # with kind=user_skipped.
        assert len(resolved) == 2
        assert all(e.data["resolution_kind"] == "user_skipped" for e in resolved)
        # gap_iteration_end records 0 resolved, 2 remaining.
        ends = reporter.events_of_kind("gap_iteration_end")
        assert ends[0].data["resolved_count"] == 0
        assert ends[0].data["remaining"] == 2

    def test_emits_iteration_end_aborted_on_user_abort(self):
        spec = make_spec()
        reporter = self._capturing()
        gaps = [gap("g1"), gap("g2")]
        handler = FakeHandler([
            Resolution(action="abort", new_value=None,
                       user_action_provenance="user_aborted"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([gaps])],
            suggesters=[FakeSuggester({"g1": good_suggestion(confidence=0.5)})],
            reviewer=None,
            handler=handler,
            apply_resolution=fake_apply,
            reporter=reporter,
        )
        orch.run(spec, context={})
        ends = reporter.events_of_kind("gap_iteration_end")
        assert len(ends) == 1
        assert ends[0].data["aborted"] is True

    def test_no_reporter_means_no_emission(self):
        # Default behavior: orchestrator runs without a reporter and emits
        # nothing. Pre-Phase-1 callers that don't pass reporter still work.
        spec = make_spec()
        gaps = [gap("g1")]
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, []])],
            suggesters=[FakeSuggester({"g1": good_suggestion(0.95)})],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
            # reporter=None (default)
        )
        # Should not raise. Outcome unchanged.
        outcome = orch.run(spec, context={})
        assert outcome.converged is True

    def test_no_iteration_events_on_already_clean_spec(self):
        # Pre-iteration detect returns empty → orchestrator returns
        # immediately without emitting gap_iteration_start. The renderer
        # should NOT see fake "iteration 1" events for a spec that was
        # already clean post-extraction.
        spec = make_spec()
        reporter = self._capturing()
        orch = Orchestrator(
            detectors=[FakeDetector([[]])],   # empty from the start
            suggesters=[FakeSuggester({})],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
            reporter=reporter,
        )
        orch.run(spec, context={})
        assert reporter.events_of_kind("gap_iteration_start") == []
        assert reporter.events_of_kind("gap_detected") == []
        assert reporter.events_of_kind("gap_resolved") == []


class TestReviewerCollectsResolutionClaims:
    """IndependentReviewSuggester._collect_claims walks both spec-value
    provenances AND resolved_label_provenance entries — one batched
    review covers both surfaces."""

    def test_collects_resolved_label_claim(self):
        from nl2protocol.gap_resolution.suggesters import IndependentReviewSuggester
        spec = _real_spec_with_resolution_provenance()
        claims = IndependentReviewSuggester._collect_claims(spec)
        # Two refs (source + destination) each have an inferred
        # resolved_label_provenance → 2 resolution claims.
        resolution_paths = [c["field_path"] for c in claims
                            if c["field_path"].endswith(".resolved_label")]
        assert "steps[0].source.resolved_label" in resolution_paths
        assert "steps[0].destination.resolved_label" in resolution_paths

    def test_skips_instruction_sourced_resolution_provenance(self):
        # If a resolved_label_provenance happens to be instruction-sourced
        # (defensive — the resolver never produces this), don't review it.
        from nl2protocol.gap_resolution.suggesters import IndependentReviewSuggester
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, LocationRef,
            Provenance, ProtocolSpec, ProvenancedVolume,
        )
        instr_prov = InstructionProvenance(source="instruction", cited_text="A1", confidence=1.0)
        comp = CompositionProvenance(
            step_cited_text="t", parameters_cited_texts=["t"],
            parameters_reasoning="t", grounding=["instruction"], confidence=1.0,
        )
        spec = ProtocolSpec(summary="t", steps=[ExtractedStep(
            order=1, action="transfer",
            volume=ProvenancedVolume(value=10.0, unit="uL", exact=True,
                                      provenance=instr_prov),
            source=LocationRef(
                description="rack", well="A1",
                resolved_label="sample_rack",
                description_provenance=instr_prov, wells_provenance=instr_prov,
                resolved_label_provenance=InstructionProvenance(
                    source="instruction", cited_text="sample_rack", confidence=1.0,
                ),
            ),
            destination=LocationRef(
                description="plate", well="B1",
                description_provenance=instr_prov, wells_provenance=instr_prov,
            ),
            composition_provenance=comp,
        )])
        claims = IndependentReviewSuggester._collect_claims(spec)
        # No resolution claim emitted — instruction-sourced provenance is
        # by definition trusted (cited_text in the instruction).
        resolution_paths = [c["field_path"] for c in claims
                            if c["field_path"].endswith(".resolved_label")]
        assert resolution_paths == []


# ============================================================================
# ADR-0012: fabrication override path
# ============================================================================

class TestADR0012FabricationOverride:
    """ADR-0012: action='override' keeps the existing fabricated value
    AS-IS but stamps user_overrode_fabrication on the audit trail.
    Combined with the CLIConfirmationHandler's [o]verride option for
    fabrication gaps, this gives the user a working escape hatch when
    the verifier flags a value the user knows is correct (synonym /
    paraphrase recovery)."""

    def _spec_with_fabricated_volume(self):
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, LocationRef,
            Provenance, ProtocolSpec, ProvenancedVolume,
        )
        # The "fabricated" detection is downstream of this test — for the
        # apply-side test we just construct a Provenanced* whose
        # cited_text IS in the spec but whose review_status will be
        # mutated by the override action.
        instr_prov = InstructionProvenance(
            source="instruction", cited_text="50uL of sample", confidence=1.0,
        )
        comp = CompositionProvenance(
            step_cited_text="t", parameters_cited_texts=["t"],
            parameters_reasoning="t", grounding=["instruction"],
            confidence=1.0,
        )
        return ProtocolSpec(summary="t", steps=[ExtractedStep(
            order=1, action="transfer",
            volume=ProvenancedVolume(value=50.0, unit="uL", exact=True,
                                      provenance=instr_prov),
            source=LocationRef(description="rack", well="A1",
                                description_provenance=instr_prov, wells_provenance=instr_prov),
            destination=LocationRef(description="plate", well="B1",
                                     description_provenance=instr_prov, wells_provenance=instr_prov),
            composition_provenance=comp,
        )])

    def test_override_action_does_not_modify_value(self):
        # ADR-0012: the existing value stays; only review_status updates.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = self._spec_with_fabricated_volume()
        original_volume_obj = spec.steps[0].volume
        original_value = spec.steps[0].volume.value
        g = gap("g1", field_path="steps[0].volume", kind="fabricated")
        res = Resolution(action="override", new_value=None,
                         user_action_provenance="user_overrode_fabrication")
        default_apply_resolution(spec, g, res, suggestion=None)
        # Same Pydantic model instance; value unchanged.
        assert spec.steps[0].volume is original_volume_obj
        assert spec.steps[0].volume.value == original_value

    def test_override_action_stamps_user_overrode_fabrication(self):
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = self._spec_with_fabricated_volume()
        g = gap("g1", field_path="steps[0].volume", kind="fabricated")
        res = Resolution(action="override", new_value=None,
                         user_action_provenance="user_overrode_fabrication")
        default_apply_resolution(spec, g, res, suggestion=None)
        prov = spec.steps[0].volume.provenance
        assert prov.review_status == "user_overrode_fabrication"

    def test_review_status_literal_accepts_user_overrode_fabrication(self):
        # Schema-level: Provenance.review_status accepts the new value.
        from nl2protocol.models.spec import Provenance
        prov = InstructionProvenance(
            source="instruction", cited_text="100uL",
            confidence=1.0, review_status="user_overrode_fabrication",
        )
        assert prov.review_status == "user_overrode_fabrication"

    def test_resolution_action_literal_accepts_override(self):
        # Schema-level: Resolution.action accepts the new value.
        res = Resolution(action="override", new_value=None,
                         user_action_provenance="user_overrode_fabrication")
        assert res.action == "override"
        assert res.user_action_provenance == "user_overrode_fabrication"


class TestCLIConfirmationHandlerOverride:
    """CLIConfirmationHandler shows [o]verride only for fabrication gaps,
    and `o` / `override` input produces the right Resolution."""

    def _make_handler_with_input(self, response_text):
        from nl2protocol.gap_resolution.handlers import CLIConfirmationHandler

        class StubCM:
            def __init__(self, value):
                self._value = value
            def prompt(self, _text):
                return self._value

        captured = []

        def capture_log(msg):
            captured.append(msg)

        return CLIConfirmationHandler(cm=StubCM(response_text), log=capture_log), captured

    def _gap(self, kind="fabricated"):
        return Gap(
            id="g1", step_order=1, field_path="steps[0].volume",
            kind=kind, current_value="50uL",
            description="claims '50uL of sample' but not in instruction",
            severity="blocker",
        )

    def test_fabrication_gap_prompt_includes_override(self):
        handler, _ = self._make_handler_with_input("o")
        prompt_text = handler._action_prompt(suggestion=None,
                                              gap=self._gap("fabricated"))
        assert "[o]verride" in prompt_text

    def test_non_fabrication_gap_prompt_omits_override(self):
        handler, _ = self._make_handler_with_input("a")
        prompt_text = handler._action_prompt(suggestion=None,
                                              gap=self._gap("missing"))
        assert "[o]verride" not in prompt_text

    def test_user_typing_o_on_fabrication_returns_override_resolution(self):
        handler, _ = self._make_handler_with_input("o")
        res = handler.present(self._gap("fabricated"), suggestion=None)
        assert res.action == "override"
        assert res.user_action_provenance == "user_overrode_fabrication"
        assert res.new_value is None

    def test_user_typing_override_long_form_also_works(self):
        handler, _ = self._make_handler_with_input("override")
        res = handler.present(self._gap("fabricated"), suggestion=None)
        assert res.action == "override"

    def test_user_typing_o_on_non_fabrication_falls_through_to_skip(self):
        # Defensive: if somehow the override option appears for a non-
        # fabrication gap, the handler treats it as skip.
        handler, log = self._make_handler_with_input("o")
        res = handler.present(self._gap("missing"), suggestion=None)
        assert res.action == "skip"
        assert any("override only valid for fabrication" in msg for msg in log)


# ============================================================================
# Fabrication accept_suggestion writes both value and provenance
# ============================================================================

class TestFabricationAcceptWritesNewValue:
    """Regression: when accept_suggestion fires on a fabrication-shaped
    gap (path ends in ``...provenance``) AND the suggester proposed a new
    value, both the value AND the provenance must land. Pre-fix, only the
    provenance was written and ``suggestion.value`` was silently dropped.

    Scope (Fix D): supported value-field counterparts are
      * slot=``provenance`` → atom ``.value`` (Provenanced* fields)
      * slot=``description_provenance`` → LocationRef ``.description``
    For slot=``wells_provenance`` / ``resolved_label_provenance`` the
    branch remains provenance-only; that's tracked separately."""

    def _spec_with_fabricated_locationref(self):
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, LocationRef,
            Provenance, ProtocolSpec, ProvenancedVolume,
        )
        instr_prov = InstructionProvenance(
            source="instruction", cited_text="50uL of sample", confidence=1.0,
        )
        # description_provenance is inferred — the slot the suggester
        # will rewrite when it proposes a new description.
        inferred_desc_prov = InferredProvenance(
            source="inferred",
            positive_reasoning="old reasoning",
            why_not_in_instruction="instruction is ambiguous",
            confidence=0.6,
        )
        comp = CompositionProvenance(
            step_cited_text="t", parameters_cited_texts=["t"],
            parameters_reasoning="t", grounding=["instruction"],
            confidence=1.0,
        )
        return ProtocolSpec(summary="t", steps=[ExtractedStep(
            order=1, action="transfer",
            volume=ProvenancedVolume(value=50.0, unit="uL", exact=True,
                                      provenance=instr_prov),
            source=LocationRef(description="tube rack", well="A1",
                                description_provenance=inferred_desc_prov,
                                wells_provenance=instr_prov),
            destination=LocationRef(description="plate", well="B1",
                                     description_provenance=instr_prov,
                                     wells_provenance=instr_prov),
            composition_provenance=comp,
        )])

    def _spec_with_fabricated_atom(self):
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, LocationRef,
            Provenance, ProtocolSpec, ProvenancedVolume,
        )
        instr_prov = InstructionProvenance(
            source="instruction", cited_text="t", confidence=1.0,
        )
        # The volume's provenance is inferred and the suggester proposes
        # a new value (e.g. correcting a fabricated volume number).
        inferred_vol_prov = InferredProvenance(
            source="inferred",
            positive_reasoning="old reasoning",
            why_not_in_instruction="instruction did not specify",
            confidence=0.6,
        )
        comp = CompositionProvenance(
            step_cited_text="t", parameters_cited_texts=["t"],
            parameters_reasoning="t", grounding=["instruction"],
            confidence=1.0,
        )
        return ProtocolSpec(summary="t", steps=[ExtractedStep(
            order=1, action="transfer",
            volume=ProvenancedVolume(value=50.0, unit="uL", exact=True,
                                      provenance=inferred_vol_prov),
            source=LocationRef(description="rack", well="A1",
                                description_provenance=instr_prov,
                                wells_provenance=instr_prov),
            destination=LocationRef(description="plate", well="B1",
                                     description_provenance=instr_prov,
                                     wells_provenance=instr_prov),
            composition_provenance=comp,
        )])

    def test_locationref_description_rewrite_lands_value_and_prov(self):
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = self._spec_with_fabricated_locationref()
        g = gap("g1",
                field_path="steps[0].source.description_provenance",
                kind="fabricated")
        sug = Suggestion(
            value="fresh tubes in C1-C6",
            provenance_source="inferred",
            positive_reasoning="ICs use fresh tubes per protocol convention",
            why_not_in_instruction="instruction said 'tube rack' ambiguously",
            confidence=0.85,
        )
        res = Resolution(action="accept_suggestion",
                         new_value=sug.value,
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(spec, g, res, suggestion=sug)
        src = spec.steps[0].source
        assert src.description == "fresh tubes in C1-C6"
        assert src.description_provenance.review_status == "user_accepted_suggestion"
        assert src.description_provenance.positive_reasoning == (
            "ICs use fresh tubes per protocol convention"
        )
        # One revision pushed; head holds new value, snapshot holds old.
        assert len(src.prior_revisions) == 1
        assert src.prior_revisions[-1].description == "tube rack"

    def test_locationref_description_rewrite_does_not_touch_wells(self):
        # Scope check: writing the description must not touch the well
        # or its provenance.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = self._spec_with_fabricated_locationref()
        g = gap("g1",
                field_path="steps[0].source.description_provenance",
                kind="fabricated")
        sug = Suggestion(
            value="fresh tubes in C1-C6",
            provenance_source="inferred",
            positive_reasoning="r", why_not_in_instruction="w",
            confidence=0.85,
        )
        res = Resolution(action="accept_suggestion",
                         new_value=sug.value,
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(spec, g, res, suggestion=sug)
        src = spec.steps[0].source
        assert src.well == "A1"
        assert src.wells_provenance.cited_text == ["50uL of sample"]

    def test_atom_value_rewrite_lands_value_and_prov(self):
        # slot=='provenance' on a Provenanced* atom — write both value
        # and a fresh inferred+user_accepted_suggestion provenance.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = self._spec_with_fabricated_atom()
        g = gap("g1",
                field_path="steps[0].volume.provenance",
                kind="fabricated")
        sug = Suggestion(
            value=100.0,
            provenance_source="inferred",
            positive_reasoning="standard WB transfer volume",
            why_not_in_instruction="instruction silent",
            confidence=0.9,
        )
        res = Resolution(action="accept_suggestion",
                         new_value=sug.value,
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(spec, g, res, suggestion=sug)
        vol = spec.steps[0].volume
        assert vol.value == 100.0
        assert vol.provenance.review_status == "user_accepted_suggestion"
        assert vol.provenance.positive_reasoning == "standard WB transfer volume"
        assert len(vol.prior_revisions) == 1
        assert vol.prior_revisions[-1].value == 50.0

    def test_prov_only_fix_when_suggestion_value_is_none(self):
        # When the suggester proposes only better reasoning (value=None),
        # behavior matches the legacy citation-only fix: write the prov,
        # leave value untouched.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = self._spec_with_fabricated_locationref()
        original_desc = spec.steps[0].source.description
        g = gap("g1",
                field_path="steps[0].source.description_provenance",
                kind="fabricated")
        sug = Suggestion(
            value=None,
            provenance_source="inferred",
            positive_reasoning="clarified reasoning",
            why_not_in_instruction="instruction ambiguous",
            confidence=0.7,
        )
        res = Resolution(action="accept_suggestion",
                         new_value=None,
                         user_action_provenance="user_accepted_suggestion")
        default_apply_resolution(spec, g, res, suggestion=sug)
        src = spec.steps[0].source
        assert src.description == original_desc
        assert src.description_provenance.review_status == "user_accepted_suggestion"
        assert src.description_provenance.positive_reasoning == "clarified reasoning"


# ============================================================================
# Orchestrator.run gap_filter param
# ============================================================================

class TestOrchestratorRunGapFilter:
    """`gap_filter` narrows the loop to a sub-set of detected gaps so the
    same Orchestrator wiring can drive scoped pre-passes (e.g. the
    description-fabrication pass that runs before labware matching) and
    the full end-of-pipeline pass."""

    def test_filter_excludes_non_matching_gaps_from_iteration(self):
        # Two gaps detected; filter keeps only one. The other is not
        # presented, not applied, and not counted in the iteration.
        spec = make_spec()
        kept = gap("g_kept", kind="missing",
                   field_path="steps[0].source.description_provenance")
        dropped = gap("g_drop", kind="missing",
                      field_path="steps[0].volume")
        sug = good_suggestion(confidence=0.9)
        orch = Orchestrator(
            detectors=[FakeDetector([[kept, dropped], []])],
            suggesters=[FakeSuggester({"g_kept": sug, "g_drop": sug})],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
            auto_accept_threshold=0.85,
        )
        outcome = orch.run(
            spec, context={},
            gap_filter=lambda g: "description" in g.field_path,
        )
        applied_ids = [aid for aid, _ in spec["applied"]]
        assert "g_kept" in applied_ids
        assert "g_drop" not in applied_ids
        assert outcome.converged

    def test_empty_after_filter_converges_without_running_loop(self):
        # Filter rejects every detected gap → behave as a clean spec
        # (converged, no records, no applies).
        spec = make_spec()
        dropped = gap("g_drop", kind="missing",
                      field_path="steps[0].volume")
        orch = Orchestrator(
            detectors=[FakeDetector([[dropped], []])],
            suggesters=[FakeSuggester({"g_drop": good_suggestion()})],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
            auto_accept_threshold=0.85,
        )
        outcome = orch.run(spec, context={},
                            gap_filter=lambda g: False)
        assert outcome.converged
        assert spec["applied"] == []

    def test_default_no_filter_passes_all_gaps_through(self):
        # Sanity: default call signature (no gap_filter) preserves the
        # full-orchestrator behavior used at the end of the pipeline.
        spec = make_spec()
        gaps_iter1 = [gap("g1", kind="missing")]
        sug = good_suggestion(confidence=0.9)
        orch = Orchestrator(
            detectors=[FakeDetector([gaps_iter1, []])],
            suggesters=[FakeSuggester({"g1": sug})],
            reviewer=None,
            handler=FakeHandler([]),
            apply_resolution=fake_apply,
            auto_accept_threshold=0.85,
        )
        outcome = orch.run(spec, context={})
        assert outcome.converged
        assert ("g1", "filled") in spec["applied"]


# ============================================================================
# default_apply_resolution: contract guard on suggester output shape
# ============================================================================


class TestApplyResolutionContractGuard:
    """When `accept_suggestion` is applied to a top-level step field
    (e.g. step.source, step.volume), the new_value MUST be a Pydantic
    instance of the expected type. Suggesters that return raw dicts /
    ints / strings violate the contract documented at
    orchestrator.default_apply_resolution; the apply layer surfaces
    the violation as a TypeError naming the path, expected type, and
    got type — instead of silently writing a malformed value into the
    spec and crashing two stages later (the bug that motivated the
    structured tool-use path on LLMSpotSuggester)."""

    def _spec(self):
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, ProtocolSpec,
        )
        comp = CompositionProvenance(
            step_cited_text="t", parameters_cited_texts=["t"],
            parameters_reasoning="t", grounding=["instruction"], confidence=1.0,
        )
        return ProtocolSpec(
            summary="t",
            steps=[ExtractedStep(order=1, action="transfer", composition_provenance=comp)],
        )

    def _real_gap_and_resolution(self, field_path: str, new_value):
        from nl2protocol.gap_resolution.types import Gap, Resolution
        gap_obj = Gap(
            id=f"g.{field_path}", step_order=1, field_path=field_path,
            kind="missing", current_value=None,
            description="missing", severity="blocker", metadata={},
        )
        res = Resolution(
            action="accept_suggestion",
            new_value=new_value,
            user_action_provenance="user_accepted_suggestion",
        )
        return gap_obj, res

    def test_raw_dict_for_source_raises_typeerror(self):
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = self._spec()
        gap_obj, res = self._real_gap_and_resolution(
            "steps[0].source", {"labware": "reagent_rack", "well": "A1"},
        )
        with pytest.raises(TypeError) as exc:
            default_apply_resolution(spec, gap_obj, res, suggestion=None)
        msg = str(exc.value)
        assert "steps[0].source" in msg
        assert "LocationRef" in msg
        assert "dict" in msg

    def test_raw_int_for_volume_raises_typeerror(self):
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = self._spec()
        gap_obj, res = self._real_gap_and_resolution("steps[0].volume", 100)
        with pytest.raises(TypeError) as exc:
            default_apply_resolution(spec, gap_obj, res, suggestion=None)
        msg = str(exc.value)
        assert "ProvenancedVolume" in msg
        assert "int" in msg

    def test_correctly_typed_location_ref_does_not_raise(self):
        """Fast-path: when the suggester correctly returns a LocationRef,
        the apply layer writes it and does not raise."""
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        from nl2protocol.models.spec import LocationRef, Provenance
        loc = LocationRef(
            description="reagent_rack",
            well="A1",
            description_provenance=InferredProvenance(
                source="inferred",
                positive_reasoning="r",
                why_not_in_instruction="n",
                confidence=0.8,
            ),
            wells_provenance=InferredProvenance(
                source="inferred",
                positive_reasoning="r",
                why_not_in_instruction="n",
                confidence=0.8,
            ),
        )
        spec = self._spec()
        gap_obj, res = self._real_gap_and_resolution("steps[0].source", loc)
        default_apply_resolution(spec, gap_obj, res, suggestion=None)
        assert isinstance(spec.steps[0].source, LocationRef)
        assert spec.steps[0].source.description == "reagent_rack"
