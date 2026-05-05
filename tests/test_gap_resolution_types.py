"""
Contract tests for the gap_resolution type definitions (ADR-0008 / ADR-0009).

PR1 skeleton: pure type tests. No detector or suggester behavior tested
here — those live in test_gap_resolution_detectors.py.

Each test pins one clause of one type's contract. When these break, the
contract changed; intentional or accidental should be obvious from the
diff.
"""

from __future__ import annotations

import pytest

from nl2protocol.gap_resolution import (
    Gap,
    Resolution,
    ReviewResult,
    Suggestion,
)


# ============================================================================
# Gap
# ============================================================================

class TestGap:
    """Gap is a frozen dataclass — immutable record of a deficiency."""

    def test_constructs_with_required_fields(self):
        g = Gap(
            id="step3.source",
            step_order=3,
            field_path="steps[2].source",
            kind="missing",
            current_value=None,
            description="missing source location",
            severity="blocker",
        )
        assert g.id == "step3.source"
        assert g.kind == "missing"
        assert g.severity == "blocker"
        assert g.metadata == {}  # default empty dict

    def test_metadata_dict_is_per_instance_default(self):
        # Default factory pattern — two Gaps must not share the same dict.
        g1 = Gap(id="a", step_order=1, field_path="x", kind="missing",
                 current_value=None, description="d", severity="blocker")
        g2 = Gap(id="b", step_order=2, field_path="y", kind="missing",
                 current_value=None, description="d", severity="blocker")
        assert g1.metadata is not g2.metadata

    def test_step_order_can_be_none_for_spec_level_gaps(self):
        # Initial-contents and cross-check warnings have no step.
        g = Gap(id="initial.A1.volume", step_order=None,
                field_path="initial_contents[0].volume_ul",
                kind="missing", current_value=None,
                description="initial volume null",
                severity="suggestion")
        assert g.step_order is None

    def test_gap_is_frozen(self):
        g = Gap(id="a", step_order=1, field_path="x", kind="missing",
                current_value=None, description="d", severity="blocker")
        with pytest.raises(Exception):  # FrozenInstanceError
            g.id = "changed"  # type: ignore


# ============================================================================
# Suggestion
# ============================================================================

class TestSuggestion:
    """Suggestion validates confidence range and non-empty positive reasoning."""

    def test_constructs_with_required_fields(self):
        s = Suggestion(
            value=100.0,
            provenance_source="deterministic",
            positive_reasoning="well capacity from labware spec",
            why_not_in_instruction=None,
            confidence=0.9,
        )
        assert s.value == 100.0
        assert s.confidence == 0.9

    def test_confidence_below_zero_raises(self):
        with pytest.raises(ValueError, match="confidence"):
            Suggestion(value=1, provenance_source="inferred",
                       positive_reasoning="x", why_not_in_instruction="y",
                       confidence=-0.1)

    def test_confidence_above_one_raises(self):
        with pytest.raises(ValueError, match="confidence"):
            Suggestion(value=1, provenance_source="inferred",
                       positive_reasoning="x", why_not_in_instruction="y",
                       confidence=1.01)

    def test_empty_positive_reasoning_raises(self):
        with pytest.raises(ValueError, match="positive_reasoning"):
            Suggestion(value=1, provenance_source="inferred",
                       positive_reasoning="", why_not_in_instruction="y",
                       confidence=0.5)

    def test_whitespace_only_positive_reasoning_raises(self):
        # The validator strips before checking — pure whitespace is "empty".
        with pytest.raises(ValueError, match="positive_reasoning"):
            Suggestion(value=1, provenance_source="inferred",
                       positive_reasoning="   \n  ",
                       why_not_in_instruction="y", confidence=0.5)

    def test_why_not_in_instruction_can_be_none_for_deterministic(self):
        # Deterministic suggesters (config lookup, capacity default) often
        # have a tautological negative reasoning that's not informative.
        s = Suggestion(value=1500.0, provenance_source="deterministic",
                       positive_reasoning="labware well capacity is 1500uL",
                       why_not_in_instruction=None, confidence=1.0)
        assert s.why_not_in_instruction is None


# ============================================================================
# ReviewResult
# ============================================================================

class TestReviewResult:
    """ReviewResult enforces the objection-iff-disagreement invariant."""

    def test_both_confirmed_no_objection_constructs_cleanly(self):
        r = ReviewResult(field_path="step1.volume",
                         confirms_positive=True, confirms_negative=True,
                         objection=None)
        assert r.objection is None

    def test_disagreement_requires_objection(self):
        with pytest.raises(ValueError, match="objection"):
            ReviewResult(field_path="step1.volume",
                         confirms_positive=False, confirms_negative=True,
                         objection=None)

    def test_partial_disagreement_requires_objection(self):
        with pytest.raises(ValueError, match="objection"):
            ReviewResult(field_path="step1.volume",
                         confirms_positive=True, confirms_negative=False,
                         objection=None)

    def test_objection_with_full_agreement_raises(self):
        # Objection on a fully-confirmed claim is internally inconsistent.
        with pytest.raises(ValueError, match="objection"):
            ReviewResult(field_path="step1.volume",
                         confirms_positive=True, confirms_negative=True,
                         objection="something")

    def test_disagreement_with_objection_constructs(self):
        r = ReviewResult(field_path="step1.volume",
                         confirms_positive=False, confirms_negative=True,
                         objection="instruction line 5 names the source explicitly")
        assert r.objection == "instruction line 5 names the source explicitly"


# ============================================================================
# Resolution
# ============================================================================

class TestResolution:
    """Resolution carries the user's decision + the user-action provenance
    that gets stamped into the spec."""

    def test_accept_suggestion_constructs(self):
        r = Resolution(action="accept_suggestion", new_value=100.0,
                       user_action_provenance="user_accepted_suggestion")
        assert r.action == "accept_suggestion"
        assert r.new_value == 100.0

    def test_edit_action_carries_new_value(self):
        r = Resolution(action="edit", new_value=200.0,
                       user_action_provenance="user_edited")
        assert r.action == "edit"
        assert r.new_value == 200.0

    def test_skip_action_can_have_none_value(self):
        r = Resolution(action="skip", new_value=None,
                       user_action_provenance="user_skipped")
        assert r.new_value is None

    def test_abort_action(self):
        r = Resolution(action="abort", new_value=None,
                       user_action_provenance="user_aborted")
        assert r.action == "abort"
