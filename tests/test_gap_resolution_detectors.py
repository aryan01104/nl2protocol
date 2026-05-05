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
    ConstraintViolationDetector,
    InitialContentsVolumeDetector,
    LabwareAmbiguityDetector,
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
# InitialContentsVolumeDetector
# ============================================================================

class TestInitialContentsVolumeDetector:
    """Emits one Gap per WellContents entry with null volume_ul.
    Spec-level gaps (step_order=None); field_path matches
    WellCapacitySuggester's regex AND default_apply_resolution's
    initial_contents path-shape."""

    def _spec_with_initial_contents(self, contents):
        from nl2protocol.models.spec import WellContents
        spec = _spec([
            ExtractedStep(order=1, action="comment", note="placeholder",
                          composition_provenance=_comp())
        ])
        spec.initial_contents = [
            WellContents(**c) for c in contents
        ]
        return spec

    def test_no_initial_contents_yields_no_gaps(self):
        spec = _spec([
            ExtractedStep(order=1, action="comment", note="hi",
                          composition_provenance=_comp())
        ])
        gaps = InitialContentsVolumeDetector().detect(spec, context={})
        assert gaps == []

    def test_all_volumes_set_yields_no_gaps(self):
        spec = self._spec_with_initial_contents([
            {"labware": "rack", "well": "A1", "substance": "x", "volume_ul": 100.0},
            {"labware": "rack", "well": "A2", "substance": "y", "volume_ul": 200.0},
        ])
        gaps = InitialContentsVolumeDetector().detect(spec, context={})
        assert gaps == []

    def test_one_null_volume_emits_one_gap(self):
        spec = self._spec_with_initial_contents([
            {"labware": "rack", "well": "A1", "substance": "buffer", "volume_ul": None},
        ])
        gaps = InitialContentsVolumeDetector().detect(spec, context={})
        assert len(gaps) == 1
        g = gaps[0]
        assert g.id == "initial_contents[0]"
        assert g.field_path == "initial_contents[0].volume_ul"
        assert g.step_order is None
        assert g.kind == "missing"
        assert g.severity == "blocker"
        assert "buffer" in g.description
        assert "A1" in g.description

    def test_multiple_null_volumes_emit_multiple_gaps_with_correct_indices(self):
        # Ensure field_path indices match the position in initial_contents,
        # not the position among null entries.
        spec = self._spec_with_initial_contents([
            {"labware": "rack", "well": "A1", "substance": "x", "volume_ul": 100.0},  # idx 0, set
            {"labware": "rack", "well": "A2", "substance": "y", "volume_ul": None},   # idx 1, null
            {"labware": "rack", "well": "A3", "substance": "z", "volume_ul": 50.0},   # idx 2, set
            {"labware": "rack", "well": "A4", "substance": "w", "volume_ul": None},   # idx 3, null
        ])
        gaps = InitialContentsVolumeDetector().detect(spec, context={})
        assert len(gaps) == 2
        ids = sorted(g.id for g in gaps)
        assert ids == ["initial_contents[1]", "initial_contents[3]"]

    def test_gap_id_is_stable_across_runs(self):
        spec = self._spec_with_initial_contents([
            {"labware": "rack", "well": "A1", "substance": "buffer", "volume_ul": None},
        ])
        gaps_1 = InitialContentsVolumeDetector().detect(spec, context={})
        gaps_2 = InitialContentsVolumeDetector().detect(spec, context={})
        assert sorted(g.id for g in gaps_1) == sorted(g.id for g in gaps_2)


# ============================================================================
# ConstraintViolationDetector
# ============================================================================

class TestConstraintViolationDetector:
    """Wraps ConstraintChecker.check_all. ERROR-severity violations
    become Gaps with kind=constraint_violation, severity=blocker;
    WARNING/INFO severity is dropped (informational only)."""

    # Minimal lab config sufficient for ConstraintChecker to run. The
    # check_well_validity path needs a labware with a known load_name so
    # `get_well_info` can compare wells against the labware's geometry.
    _CONFIG = {
        "pipettes": {
            "left": {"model": "p300_single_gen2"},
        },
        "labware": {
            "sample_rack": {
                "load_name": "opentrons_24_tuberack_eppendorf_2ml_safelock_snapcap",
                "deck_slot": "1",
            },
        },
    }

    def _spec_with_oor_wells(self):
        # 24-tube rack is 4 rows x 6 cols (A1..D6). A7+ are out of range.
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=ProvenancedVolume(value=50.0, unit="uL", exact=True,
                                         provenance=_instr_prov()),
                source=LocationRef(
                    description="sample_rack",
                    wells=["A1", "A2", "A7", "A8"],
                    resolved_label="sample_rack",
                    provenance=_instr_prov("A1, A2, A7, A8"),
                ),
                destination=LocationRef(
                    description="sample_rack",
                    well="B1",
                    resolved_label="sample_rack",
                    provenance=_instr_prov("B1"),
                ),
                composition_provenance=_comp(),
            ),
        ])
        return spec

    def test_no_config_in_context_yields_no_gaps(self):
        # Defensive: orchestrator-with-no-config simply skips constraint
        # detection rather than crashing.
        spec = _spec([
            ExtractedStep(order=1, action="comment", note="hi",
                          composition_provenance=_comp())
        ])
        gaps = ConstraintViolationDetector().detect(spec, context={})
        assert gaps == []

    def test_clean_spec_yields_no_gaps(self):
        spec = _spec([
            ExtractedStep(order=1, action="comment", note="hi",
                          composition_provenance=_comp())
        ])
        gaps = ConstraintViolationDetector().detect(
            spec, context={"config": self._CONFIG})
        assert gaps == []

    def test_well_invalid_emits_constraint_violation_gap(self):
        spec = self._spec_with_oor_wells()
        gaps = ConstraintViolationDetector().detect(
            spec, context={"config": self._CONFIG})
        # WellRangeClipSuggester relies on the description containing both the
        # offending wells and the valid range — pin both.
        assert any(g.kind == "constraint_violation" for g in gaps)
        oor = next(g for g in gaps if g.kind == "constraint_violation"
                   and "do not exist" in g.description)
        assert oor.severity == "blocker"
        assert oor.step_order == 1
        assert "A7" in oor.description or "A8" in oor.description
        assert "Valid well range" in oor.description

    def test_well_invalid_field_path_targets_offending_ref(self):
        # The detector walks the step to determine which ref carries the
        # invalid wells, so default_apply_resolution lands on the right side.
        spec = self._spec_with_oor_wells()
        gaps = ConstraintViolationDetector().detect(
            spec, context={"config": self._CONFIG})
        oor = next(g for g in gaps if g.kind == "constraint_violation"
                   and "do not exist" in g.description)
        # Source carries A7/A8 (the violation); destination is B1 (valid).
        assert oor.field_path == "steps[0].source.wells"

    def test_gap_id_is_stable_across_runs(self):
        spec = self._spec_with_oor_wells()
        ids_1 = sorted(g.id for g in ConstraintViolationDetector().detect(
            spec, context={"config": self._CONFIG}))
        ids_2 = sorted(g.id for g in ConstraintViolationDetector().detect(
            spec, context={"config": self._CONFIG}))
        assert ids_1 == ids_2

    def test_metadata_carries_violation_type_and_values(self):
        spec = self._spec_with_oor_wells()
        gaps = ConstraintViolationDetector().detect(
            spec, context={"config": self._CONFIG})
        oor = next(g for g in gaps if g.kind == "constraint_violation"
                   and "do not exist" in g.description)
        assert oor.metadata.get("violation_type") == "well_invalid"
        assert "invalid_wells" in oor.metadata.get("values", {})


# ============================================================================
# LabwareAmbiguityDetector
# ============================================================================

class TestLabwareAmbiguityDetector:
    """Walks spec.steps[*].source/destination; emits a Gap per LocationRef
    where resolved_label is None. Refs the resolver picked are skipped
    (their pick is verified by the reviewer pass on
    resolved_label_provenance, not by re-prompting)."""

    def _step_with_locations(self, source_resolved=None, destination_resolved=None,
                              source_desc="rack", destination_desc="plate"):
        return ExtractedStep(
            order=1, action="transfer",
            volume=ProvenancedVolume(value=10.0, unit="uL", exact=True,
                                     provenance=_instr_prov()),
            source=LocationRef(
                description=source_desc, well="A1",
                resolved_label=source_resolved,
                provenance=_instr_prov("rack A1"),
            ),
            destination=LocationRef(
                description=destination_desc, well="B1",
                resolved_label=destination_resolved,
                provenance=_instr_prov("plate B1"),
            ),
            composition_provenance=_comp(),
        )

    def test_all_resolved_yields_no_gaps(self):
        spec = _spec([self._step_with_locations(
            source_resolved="sample_rack",
            destination_resolved="output_plate",
        )])
        gaps = LabwareAmbiguityDetector().detect(spec, context={})
        assert gaps == []

    def test_one_unresolved_emits_one_gap(self):
        spec = _spec([self._step_with_locations(
            source_resolved=None,           # ambiguous — resolver returned null
            destination_resolved="plate1",  # resolved — skip
        )])
        gaps = LabwareAmbiguityDetector().detect(spec, context={})
        assert len(gaps) == 1
        g = gaps[0]
        assert g.kind == "ambiguous"
        assert g.severity == "blocker"
        assert g.step_order == 1
        assert g.field_path == "steps[0].source.resolved_label"
        assert g.id == "labware.step1.source"
        assert g.metadata == {"description": "rack", "role": "source"}

    def test_both_unresolved_emit_two_gaps(self):
        spec = _spec([self._step_with_locations(
            source_resolved=None,
            destination_resolved=None,
        )])
        gaps = LabwareAmbiguityDetector().detect(spec, context={})
        assert len(gaps) == 2
        ids = {g.id for g in gaps}
        assert ids == {"labware.step1.source", "labware.step1.destination"}

    def test_step_with_no_location_refs_skipped(self):
        # A pause step has no source/destination — detector must not crash
        # or emit spurious Gaps.
        spec = _spec([
            ExtractedStep(
                order=1, action="pause", note="hold for 5min",
                composition_provenance=_comp(),
            ),
        ])
        gaps = LabwareAmbiguityDetector().detect(spec, context={})
        assert gaps == []

    def test_gap_id_is_stable_across_runs(self):
        spec = _spec([self._step_with_locations(source_resolved=None)])
        ids_1 = sorted(g.id for g in LabwareAmbiguityDetector().detect(spec, context={}))
        ids_2 = sorted(g.id for g in LabwareAmbiguityDetector().detect(spec, context={}))
        assert ids_1 == ids_2


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
