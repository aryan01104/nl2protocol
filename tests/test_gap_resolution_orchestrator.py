"""
Contract tests for the gap-resolution orchestrator (ADR-0008 PR2).

The orchestrator is a state machine: detect → topo-sort → suggest →
review → classify → present → apply → re-detect, max N iterations.

These tests use FAKE detectors / suggesters / handlers / appliers to
isolate the orchestrator's logic. No LLM calls.
"""

from __future__ import annotations

import pytest

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
        # Detector keeps returning the same gap forever (suggester
        # returns None so user is prompted; user keeps skipping).
        gaps = [gap("g1")]
        handler = FakeHandler([
            Resolution(action="skip", new_value=None,
                       user_action_provenance="user_skipped"),
            Resolution(action="skip", new_value=None,
                       user_action_provenance="user_skipped"),
            Resolution(action="skip", new_value=None,
                       user_action_provenance="user_skipped"),
        ])
        orch = Orchestrator(
            detectors=[FakeDetector([gaps, gaps, gaps, gaps])],
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
        Provenance, ProtocolSpec, ProvenancedVolume,
    )

    def _inferred_prov(reasoning="some reasoning",
                       why_not="instruction omitted this"):
        return Provenance(
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
            provenance=_inferred_prov("config lookup", "instruction omits source"),
        ),
        destination=LocationRef(
            description="dst", well="B1",
            provenance=_inferred_prov("dest from context", "instruction omits dest"),
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
        prov = spec.steps[0].source.provenance
        assert prov.review_status == "reviewed_disagree"
        assert prov.reviewer_objection == "instruction line 3 names this source verbatim"

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
        assert spec.steps[0].source.provenance.review_status == "original"
        assert spec.steps[0].destination.provenance.review_status == "original"

    def test_empty_reviews_leaves_spec_untouched(self):
        spec = _real_spec()
        stamp_reviewer_verdicts(spec, reviews={})
        for fname in ("volume", "source", "destination"):
            assert getattr(spec.steps[0], fname).provenance.review_status == "original"

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
        from nl2protocol.models.spec import Provenance, ProvenancedVolume
        return ProvenancedVolume(
            value=value, unit="uL", exact=True,
            provenance=Provenance(
                source="inferred",
                positive_reasoning="suggester proposed this",
                why_not_in_instruction="instruction lacks the volume",
                confidence=0.9,
            ),
        )

    def _suggested_location(self, well="C3"):
        from nl2protocol.models.spec import LocationRef, Provenance
        return LocationRef(
            description="config-found", well=well,
            provenance=Provenance(
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
        # steps[0].destination.wells write — the wells subfield doesn't have
        # its own provenance; the parent (LocationRef) does, and that's what
        # the user action applies to.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        spec = _real_spec()
        new_wells = ["B2", "B3"]
        g = gap("g1", field_path="steps[0].destination.wells")
        res = Resolution(action="edit", new_value=new_wells,
                         user_action_provenance="user_edited")
        default_apply_resolution(spec, g, res, suggestion=None)
        assert spec.steps[0].destination.wells == new_wells
        assert spec.steps[0].destination.provenance.review_status == "user_edited"

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
        Provenance, ProtocolSpec, ProvenancedVolume,
    )
    instr_prov = Provenance(
        source="instruction", cited_text="A1", confidence=1.0,
    )
    res_prov = Provenance(
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
            provenance=instr_prov,
            resolved_label_provenance=res_prov,
        ),
        destination=LocationRef(
            description="rack", well="B1",
            resolved_label="sample_rack",
            provenance=instr_prov,
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
        assert spec.steps[0].source.provenance.review_status == "original"

    def test_user_pick_when_no_prior_resolution_provenance(self):
        # If the resolver didn't run (no resolved_label_provenance set),
        # the apply should construct a fresh Provenance for the user's pick.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, LocationRef,
            Provenance, ProtocolSpec, ProvenancedVolume,
        )
        instr_prov = Provenance(source="instruction", cited_text="A1", confidence=1.0)
        comp = CompositionProvenance(
            step_cited_text="t", parameters_cited_texts=["t"],
            parameters_reasoning="t", grounding=["instruction"], confidence=1.0,
        )
        spec = ProtocolSpec(summary="t", steps=[ExtractedStep(
            order=1, action="transfer",
            volume=ProvenancedVolume(value=10.0, unit="uL", exact=True,
                                      provenance=instr_prov),
            source=LocationRef(description="rack", well="A1",
                                provenance=instr_prov),
            destination=LocationRef(description="plate", well="B1",
                                     provenance=instr_prov),
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
        assert spec.steps[0].source.provenance.review_status == "original"

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
        instr_prov = Provenance(source="instruction", cited_text="A1", confidence=1.0)
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
                provenance=instr_prov,
                resolved_label_provenance=Provenance(
                    source="instruction", cited_text="sample_rack", confidence=1.0,
                ),
            ),
            destination=LocationRef(
                description="plate", well="B1",
                provenance=instr_prov,
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
        instr_prov = Provenance(
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
                                provenance=instr_prov),
            destination=LocationRef(description="plate", well="B1",
                                     provenance=instr_prov),
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
        prov = Provenance(
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
