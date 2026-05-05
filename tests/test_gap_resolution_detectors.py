"""
Contract tests for the detector adapters (ADR-0008 PR1).

Each adapter wraps an existing detection function. These tests pin that:
  - The adapter produces a Gap for every output of the underlying function
  - Gap fields (id, kind, severity, field_path) match the underlying
    function's output

PR1 has two adapters: MissingFieldsDetector + ProvenanceWarningDetector.
The constraint, initial-volume, and labware-ambiguity detectors are
deferred to PR2.
"""

from __future__ import annotations

import pytest

from nl2protocol.gap_resolution import (
    MissingFieldsDetector,
    ProvenanceWarningDetector,
    detect_all,
)
from nl2protocol.models.spec import (
    CompositionProvenance,
    ExtractedStep,
    LocationRef,
    Provenance,
    ProtocolSpec,
    ProvenancedString,
    ProvenancedVolume,
)


# ============================================================================
# FIXTURES
# ============================================================================

def _instr_prov(text: str = "test") -> Provenance:
    return Provenance(source="instruction", cited_text=text, confidence=1.0)


def _comp() -> CompositionProvenance:
    return CompositionProvenance(
        step_cited_text="test step",
        parameters_cited_texts=["test step"],
        parameters_reasoning="test cohesion",
        grounding=["instruction"],
        confidence=1.0,
    )


def _spec(steps: list) -> ProtocolSpec:
    return ProtocolSpec(summary="test", steps=steps)


# ============================================================================
# MissingFieldsDetector
# ============================================================================

class TestMissingFieldsDetector:
    """Wraps SemanticExtractor.missing_fields. Gap kind=missing, severity=blocker."""

    def test_clean_spec_yields_no_gaps(self):
        # A complete delay step (action=delay needs duration; we omit it
        # to make the test minimal but still pass — actually delay needs
        # duration, so use comment with note instead).
        spec = _spec([
            ExtractedStep(
                order=1, action="comment", note="all good",
                composition_provenance=_comp(),
            )
        ])
        gaps = MissingFieldsDetector().detect(spec, context={})
        assert gaps == []

    def test_missing_volume_on_transfer_emits_blocker_gap(self):
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                source=LocationRef(description="src", well="A1", provenance=_instr_prov()),
                destination=LocationRef(description="dst", well="B1", provenance=_instr_prov()),
                composition_provenance=_comp(),
            ),
        ])
        gaps = MissingFieldsDetector().detect(spec, context={})
        assert len(gaps) >= 1
        volume_gap = next((g for g in gaps if "volume" in g.field_path), None)
        assert volume_gap is not None
        assert volume_gap.kind == "missing"
        assert volume_gap.severity == "blocker"
        assert volume_gap.step_order == 1
        assert volume_gap.id == "step1.volume"

    def test_missing_source_and_destination_emit_separate_gaps(self):
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=ProvenancedVolume(value=50.0, unit="uL", exact=True,
                                         provenance=_instr_prov()),
                composition_provenance=_comp(),
            ),
        ])
        gaps = MissingFieldsDetector().detect(spec, context={})
        # Validator emits one error per missing field; both surface as Gaps.
        kinds = {g.field_path for g in gaps}
        assert any("source" in k for k in kinds)
        assert any("destination" in k for k in kinds)

    def test_gap_id_is_stable_across_runs(self):
        # The orchestrator's iteration logic relies on stable Gap ids to
        # match "same gap as last iteration" vs "new gap surfaced this round."
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                source=LocationRef(description="src", well="A1", provenance=_instr_prov()),
                destination=LocationRef(description="dst", well="B1", provenance=_instr_prov()),
                composition_provenance=_comp(),
            ),
        ])
        gaps_1 = MissingFieldsDetector().detect(spec, context={})
        gaps_2 = MissingFieldsDetector().detect(spec, context={})
        ids_1 = sorted(g.id for g in gaps_1)
        ids_2 = sorted(g.id for g in gaps_2)
        assert ids_1 == ids_2


# ============================================================================
# ProvenanceWarningDetector
# ============================================================================

class TestProvenanceWarningDetector:
    """Wraps SemanticExtractor.verify_provenance_claims. Translates each
    warning to a Gap. Severity routing: fabrication→blocker, low_confidence→
    suggestion, unverified→quality."""

    @pytest.fixture
    def extractor(self):
        # SemanticExtractor takes an Anthropic client. verify_provenance_claims
        # does NOT call the LLM (it's pure text/regex verification), so we
        # pass a dummy stand-in. If the underlying function ever tries to
        # call self.client.messages.create, the test will explode loudly,
        # which is the right failure mode.
        from nl2protocol.extraction import SemanticExtractor
        return SemanticExtractor(client=object())

    def test_clean_spec_yields_no_gaps(self, extractor):
        # Every cite ("100uL", "tube rack", "from A1", "to B1") appears in
        # the instruction → no fabrication warnings.
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=ProvenancedVolume(
                    value=100.0, unit="uL", exact=True,
                    provenance=Provenance(source="instruction",
                                          cited_text="100uL", confidence=1.0),
                ),
                source=LocationRef(description="tube rack", well="A1",
                                   provenance=_instr_prov("from A1")),
                destination=LocationRef(description="tube rack", well="B1",
                                        provenance=_instr_prov("to B1")),
                composition_provenance=_comp(),
            ),
        ])
        spec.explicit_volumes = [100.0]
        gaps = ProvenanceWarningDetector(extractor).detect(
            spec,
            context={"instruction": "Transfer 100uL from tube rack A1 to tube rack B1.", "config": {}},
        )
        assert all(g.kind != "fabricated" for g in gaps)

    def test_fabricated_instruction_volume_emits_blocker_gap(self, extractor):
        # Volume claims source="instruction" with value 999 — but instruction
        # text doesn't contain 999. Underlying verifier flags this as
        # severity="fabrication"; adapter must promote to Gap with
        # kind="fabricated", severity="blocker".
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=ProvenancedVolume(
                    value=999.0, unit="uL", exact=True,
                    provenance=Provenance(source="instruction",
                                          cited_text="999uL", confidence=1.0),
                ),
                source=LocationRef(description="tube rack", well="A1",
                                   provenance=_instr_prov("from A1")),
                destination=LocationRef(description="tube rack", well="B1",
                                        provenance=_instr_prov("to B1")),
                composition_provenance=_comp(),
            ),
        ])
        spec.explicit_volumes = []  # 999 not in extracted volumes
        gaps = ProvenanceWarningDetector(extractor).detect(
            spec,
            context={"instruction": "Transfer some liquid.", "config": {}},
        )
        fabrications = [g for g in gaps if g.kind == "fabricated"]
        assert len(fabrications) >= 1
        for g in fabrications:
            assert g.severity == "blocker"

    def test_low_confidence_inferred_does_not_emit_gap(self, extractor):
        # Per ADR-0008 PR2 architectural correction: low_confidence /
        # unverified warnings from _flag_uncertain_claims are NOT emitted
        # as Gaps. Confidence gating happens at the orchestrator's classify
        # step (auto_accept_threshold + ALWAYS_CONFIRM rules), not at
        # detect time. Emitting these as Gaps caused circular re-detection
        # — suggesters write inferred provenance, detector immediately
        # re-flagged it as a gap, ad infinitum.
        spec = _spec([
            ExtractedStep(
                order=1, action="mix",
                volume=ProvenancedVolume(
                    value=50.0, unit="uL", exact=False,
                    provenance=Provenance(source="inferred",
                                          reasoning="half of 100uL total",
                                          confidence=0.6),
                ),
                composition_provenance=_comp(),
            ),
        ])
        gaps = ProvenanceWarningDetector(extractor).detect(
            spec,
            context={"instruction": "Mix at half the total volume of 100uL.", "config": {}},
        )
        # Inferred values produce NO gaps — confidence is the orchestrator's
        # concern, not the detector's.
        assert all(g.kind != "low_confidence" for g in gaps)

    def test_field_path_includes_step_index(self, extractor):
        # Two-step spec — order must be consecutive. The detector should
        # produce 0-indexed field paths matching the renderer's addressing.
        spec = _spec([
            ExtractedStep(
                order=1, action="comment", note="placeholder first step",
                composition_provenance=_comp(),
            ),
            ExtractedStep(
                order=2, action="mix",
                volume=ProvenancedVolume(
                    value=50.0, unit="uL", exact=False,
                    provenance=Provenance(source="inferred",
                                          reasoning="inferred",
                                          confidence=0.5),
                ),
                composition_provenance=_comp(),
            ),
        ])
        gaps = ProvenanceWarningDetector(extractor).detect(
            spec, context={"instruction": "Mix.", "config": {}},
        )
        # step.order=2 → field_path index = 1 (0-indexed)
        for g in gaps:
            if g.step_order == 2:
                assert "steps[1]" in g.field_path


# ============================================================================
# Registry: detect_all unions detector outputs in order
# ============================================================================

class TestDetectAllRegistry:
    """`detect_all` runs every passed detector and concatenates Gaps in
    detector order; each detector's gaps stay in their own deterministic order."""

    def test_empty_detector_list_returns_empty(self):
        spec = _spec([
            ExtractedStep(order=1, action="comment", note="hi",
                          composition_provenance=_comp())
        ])
        assert detect_all(spec, context={}, detectors=[]) == []

    def test_concatenates_in_detector_order(self):
        # Build a synthetic detector that emits a known Gap.
        from nl2protocol.gap_resolution.types import Gap

        class FakeDetectorA:
            def detect(self, spec, context):
                return [Gap(id="a1", step_order=None, field_path="a",
                            kind="missing", current_value=None,
                            description="fake A", severity="quality")]

        class FakeDetectorB:
            def detect(self, spec, context):
                return [Gap(id="b1", step_order=None, field_path="b",
                            kind="missing", current_value=None,
                            description="fake B", severity="quality")]

        spec = _spec([
            ExtractedStep(order=1, action="comment", note="hi",
                          composition_provenance=_comp())
        ])
        out = detect_all(spec, context={},
                         detectors=[FakeDetectorA(), FakeDetectorB()])
        assert [g.id for g in out] == ["a1", "b1"]
