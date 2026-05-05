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
