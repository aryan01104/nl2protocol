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

from nl2protocol.models.spec import InstructionProvenance, InferredProvenance, validate_provenance
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
    InferredProvenance,
    InstructionProvenance,
    LocationRef,
    ProvenanceBase,
    ProtocolSpec,
    ProvenancedString,
    ProvenancedVolume,
)


# ============================================================================
# FIXTURES
# ============================================================================

def _instr_prov(text: str = "test") -> ProvenanceBase:
    return InstructionProvenance(source="instruction", cited_text=text, confidence=1.0)


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
                source=LocationRef(description="src", well="A1", description_provenance=_instr_prov(), wells_provenance=_instr_prov()),
                destination=LocationRef(description="dst", well="B1", description_provenance=_instr_prov(), wells_provenance=_instr_prov()),
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
                source=LocationRef(description="src", well="A1", description_provenance=_instr_prov(), wells_provenance=_instr_prov()),
                destination=LocationRef(description="dst", well="B1", description_provenance=_instr_prov(), wells_provenance=_instr_prov()),
                composition_provenance=_comp(),
            ),
        ])
        gaps_1 = MissingFieldsDetector().detect(spec, context={})
        gaps_2 = MissingFieldsDetector().detect(spec, context={})
        ids_1 = sorted(g.id for g in gaps_1)
        ids_2 = sorted(g.id for g in gaps_2)
        assert ids_1 == ids_2

    def test_mix_missing_location_routes_to_destination_not_unknown(self):
        # A single-location action (mix) with no source/destination routes its
        # "missing location" gap to a real field (destination) — not the old
        # unmappable ".unknown" placeholder that crashed the apply layer.
        spec = _spec([
            ExtractedStep(
                order=1, action="mix",
                volume=ProvenancedVolume(value=100.0, unit="uL", exact=True,
                                         provenance=_instr_prov()),
                repetitions=3,
                composition_provenance=_comp(),
            ),
        ])
        gaps = MissingFieldsDetector().detect(spec, context={})
        loc = next((g for g in gaps if "location" in g.description), None)
        assert loc is not None
        assert loc.field_path == "steps[0].destination"
        assert not any(".unknown" in g.field_path or g.field_path == "unknown" for g in gaps)


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
        # Every cite grounds its value: the labware cite contains the
        # labware label, the wells cite contains the well, the volume
        # cite contains the volume. No fabrication warnings.
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=ProvenancedVolume(
                    value=100.0, unit="uL", exact=True,
                    provenance=InstructionProvenance(source="instruction",
                                          cited_text="100uL", confidence=1.0),
                ),
                source=LocationRef(
                    description="tube rack", well="A1",
                    description_provenance=_instr_prov("tube rack"),
                    wells_provenance=_instr_prov("A1"),
                ),
                destination=LocationRef(
                    description="tube rack", well="B1",
                    description_provenance=_instr_prov("tube rack"),
                    wells_provenance=_instr_prov("B1"),
                ),
                composition_provenance=_comp(),
            ),
        ])
        gaps = ProvenanceWarningDetector().detect(
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
                    provenance=InstructionProvenance(source="instruction",
                                          cited_text="999uL", confidence=1.0),
                ),
                source=LocationRef(description="tube rack", well="A1",
                                   description_provenance=_instr_prov("from A1"), wells_provenance=_instr_prov("from A1")),
                destination=LocationRef(description="tube rack", well="B1",
                                        description_provenance=_instr_prov("to B1"), wells_provenance=_instr_prov("to B1")),
                composition_provenance=_comp(),
            ),
        ])
        gaps = ProvenanceWarningDetector().detect(
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
                    provenance=InferredProvenance(source="inferred",
                                          positive_reasoning="half of 100uL total",
                                          confidence=0.6),
                ),
                composition_provenance=_comp(),
            ),
        ])
        gaps = ProvenanceWarningDetector().detect(
            spec,
            context={"instruction": "Mix at half the total volume of 100uL.", "config": {}},
        )
        # Inferred values produce NO gaps — confidence is the orchestrator's
        # concern, not the detector's.
        assert all(g.kind != "low_confidence" for g in gaps)

    @pytest.mark.parametrize("terminal_status", [
        "user_confirmed",
        "user_edited",
        "user_accepted_suggestion",
        "user_overrode_fabrication",
        "reviewed_agree",
    ])
    def test_terminal_review_status_skips_fabrication_check(self, extractor, terminal_status):
        # A Provenance whose source='instruction' carries cited_text that
        # would FAIL the substring check, but whose review_status indicates
        # a terminal user/reviewer action. The verifier must skip the
        # cited_text checks — otherwise the orchestrator's gap loop
        # re-raises a gap the user already resolved, bouncing across
        # iterations until the iteration cap.
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=ProvenancedVolume(
                    value=999.0, unit="uL", exact=True,
                    provenance=InstructionProvenance(
                        source="instruction",
                        cited_text="999uL",        # not present in the instruction below
                        review_status=terminal_status,
                        confidence=1.0,
                    ),
                ),
                source=LocationRef(
                    description="tube rack", well="A1",
                    description_provenance=_instr_prov("from A1"),
                    wells_provenance=_instr_prov("from A1"),
                ),
                destination=LocationRef(
                    description="tube rack", well="B1",
                    description_provenance=_instr_prov("to B1"),
                    wells_provenance=_instr_prov("to B1"),
                ),
                composition_provenance=_comp(),
            ),
        ])
        gaps = ProvenanceWarningDetector().detect(
            spec,
            context={"instruction": "Transfer some liquid from A1 to B1.", "config": {}},
        )
        volume_fabrications = [
            g for g in gaps
            if g.kind == "fabricated"
            and g.field_path == "steps[0].volume.provenance"
        ]
        assert volume_fabrications == [], (
            f"verifier re-flagged a {terminal_status} provenance — "
            f"terminal short-circuit not respected"
        )

    def test_original_status_still_raises_fabrication(self, extractor):
        # Counter-check: same shape but review_status='original' MUST still
        # raise a fabrication gap. Confirms the terminal-status short-circuit
        # didn't accidentally suppress all fabrications.
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=ProvenancedVolume(
                    value=999.0, unit="uL", exact=True,
                    provenance=InstructionProvenance(
                        source="instruction",
                        cited_text="999uL",
                        review_status="original",
                        confidence=1.0,
                    ),
                ),
                source=LocationRef(
                    description="tube rack", well="A1",
                    description_provenance=_instr_prov("from A1"),
                    wells_provenance=_instr_prov("from A1"),
                ),
                destination=LocationRef(
                    description="tube rack", well="B1",
                    description_provenance=_instr_prov("to B1"),
                    wells_provenance=_instr_prov("to B1"),
                ),
                composition_provenance=_comp(),
            ),
        ])
        gaps = ProvenanceWarningDetector().detect(
            spec,
            context={"instruction": "Transfer some liquid from A1 to B1.", "config": {}},
        )
        volume_fabrications = [
            g for g in gaps
            if g.kind == "fabricated"
            and g.field_path == "steps[0].volume.provenance"
        ]
        assert len(volume_fabrications) == 1

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
                    provenance=InferredProvenance(source="inferred",
                                          positive_reasoning="inferred",
                                          confidence=0.5),
                ),
                composition_provenance=_comp(),
            ),
        ])
        gaps = ProvenanceWarningDetector().detect(
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
                    description_provenance=_instr_prov("A1, A2, A7, A8"), wells_provenance=_instr_prov("A1, A2, A7, A8"),
                ),
                destination=LocationRef(
                    description="sample_rack",
                    well="B1",
                    resolved_label="sample_rack",
                    description_provenance=_instr_prov("B1"), wells_provenance=_instr_prov("B1"),
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

    # --- Bug-2 dedupe contract ---

    def _spec_two_steps_same_oor_wells(self):
        # Two transfer steps both asking for A7+A8 on the same 24-tube rack —
        # same logical problem (rack only has A1-D6); should produce ONE Gap
        # covering both steps.
        return _spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=ProvenancedVolume(value=50.0, unit="uL", exact=True,
                                         provenance=_instr_prov()),
                source=LocationRef(
                    description="sample_rack",
                    wells=["A1", "A7", "A8"],
                    resolved_label="sample_rack",
                    description_provenance=_instr_prov("A1, A7, A8"), wells_provenance=_instr_prov("A1, A7, A8"),
                ),
                destination=LocationRef(
                    description="sample_rack", well="B1",
                    resolved_label="sample_rack",
                    description_provenance=_instr_prov("B1"), wells_provenance=_instr_prov("B1"),
                ),
                composition_provenance=_comp(),
            ),
            ExtractedStep(
                order=2, action="transfer",
                volume=ProvenancedVolume(value=50.0, unit="uL", exact=True,
                                         provenance=_instr_prov()),
                source=LocationRef(
                    description="sample_rack",
                    wells=["A2", "A7", "A8"],
                    resolved_label="sample_rack",
                    description_provenance=_instr_prov("A2, A7, A8"), wells_provenance=_instr_prov("A2, A7, A8"),
                ),
                destination=LocationRef(
                    description="sample_rack", well="B2",
                    resolved_label="sample_rack",
                    description_provenance=_instr_prov("B2"), wells_provenance=_instr_prov("B2"),
                ),
                composition_provenance=_comp(),
            ),
        ])

    def test_dedupes_identical_well_invalid_across_steps(self):
        # Bug-2: two steps with the same out-of-range wells on the same
        # labware → ONE Gap (not two). The user answers once.
        spec = self._spec_two_steps_same_oor_wells()
        gaps = ConstraintViolationDetector().detect(
            spec, context={"config": self._CONFIG})
        well_gaps = [g for g in gaps
                     if g.metadata.get("violation_type") == "well_invalid"]
        assert len(well_gaps) == 1
        # affected_steps lists both source steps.
        assert sorted(well_gaps[0].metadata["affected_steps"]) == [1, 2]
        # affected_paths lists both source.wells paths.
        assert len(well_gaps[0].metadata["affected_paths"]) == 2
        # Description includes the multi-step prefix.
        assert "Affects 2 steps" in well_gaps[0].description

    def test_does_not_dedupe_different_labware_with_same_wells(self):
        # False-dupe defense: two steps with the same offending well names
        # but on DIFFERENT labware are different problems (different valid
        # ranges, different fixes). Must NOT be grouped.
        # We use well A20 which is well-name-pattern valid (row A, col 20)
        # but invalid on both a 24-tube rack (max col 6) AND a 96-plate
        # (max col 12), so the same offending wells list ["A20"] surfaces
        # on both labware — different labware key, so two Gaps.
        config_two = {
            "pipettes": {"left": {"model": "p300_single_gen2"}},
            "labware": {
                "sample_rack": {
                    "load_name": "opentrons_24_tuberack_eppendorf_2ml_safelock_snapcap",
                    "deck_slot": "1",
                },
                "wide_plate": {
                    "load_name": "opentrons_96_wellplate_200ul_pcr_full_skirt",
                    "deck_slot": "2",
                },
            },
        }
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=ProvenancedVolume(value=10.0, unit="uL", exact=True,
                                         provenance=_instr_prov()),
                source=LocationRef(
                    description="sample_rack", wells=["A20"],
                    resolved_label="sample_rack",
                    description_provenance=_instr_prov("A20"), wells_provenance=_instr_prov("A20"),
                ),
                destination=LocationRef(
                    description="sample_rack", well="B1",
                    resolved_label="sample_rack",
                    description_provenance=_instr_prov("B1"), wells_provenance=_instr_prov("B1"),
                ),
                composition_provenance=_comp(),
            ),
            ExtractedStep(
                order=2, action="transfer",
                volume=ProvenancedVolume(value=10.0, unit="uL", exact=True,
                                         provenance=_instr_prov()),
                source=LocationRef(
                    description="wide_plate", wells=["A20"],
                    resolved_label="wide_plate",
                    description_provenance=_instr_prov("A20"), wells_provenance=_instr_prov("A20"),
                ),
                destination=LocationRef(
                    description="wide_plate", well="B1",
                    resolved_label="wide_plate",
                    description_provenance=_instr_prov("B1"), wells_provenance=_instr_prov("B1"),
                ),
                composition_provenance=_comp(),
            ),
        ])
        gaps = ConstraintViolationDetector().detect(
            spec, context={"config": config_two})
        well_gaps = [g for g in gaps
                     if g.metadata.get("violation_type") == "well_invalid"]
        # Two distinct labware → two Gaps (the labware key in _dedupe_key
        # keeps them separate even though invalid_wells is identical).
        assert len(well_gaps) == 2

    def test_dedupes_labware_not_found_by_description(self):
        # Two steps both reference a labware whose description isn't in
        # config → one Gap covering both steps. Config has TWO labware
        # so the Phase 5 capability fallback can't rescue uniquely
        # (A1 fits both → ambiguous → original LABWARE_NOT_FOUND path).
        bad_config = {
            "pipettes": {"left": {"model": "p300_single_gen2"}},
            "labware": {
                "real_rack": {
                    "load_name": "opentrons_24_tuberack_eppendorf_2ml_safelock_snapcap",
                    "deck_slot": "1",
                },
                "spare_rack": {
                    "load_name": "opentrons_24_tuberack_eppendorf_2ml_safelock_snapcap",
                    "deck_slot": "2",
                },
            },
        }
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=ProvenancedVolume(value=10.0, unit="uL", exact=True,
                                         provenance=_instr_prov()),
                source=LocationRef(description="ghost_rack", well="A1",
                                    description_provenance=_instr_prov("A1"), wells_provenance=_instr_prov("A1")),
                destination=LocationRef(description="real_rack", well="B1",
                                         resolved_label="real_rack",
                                         description_provenance=_instr_prov("B1"), wells_provenance=_instr_prov("B1")),
                composition_provenance=_comp(),
            ),
            ExtractedStep(
                order=2, action="transfer",
                volume=ProvenancedVolume(value=10.0, unit="uL", exact=True,
                                         provenance=_instr_prov()),
                source=LocationRef(description="ghost_rack", well="A2",
                                    description_provenance=_instr_prov("A2"), wells_provenance=_instr_prov("A2")),
                destination=LocationRef(description="real_rack", well="B2",
                                         resolved_label="real_rack",
                                         description_provenance=_instr_prov("B2"), wells_provenance=_instr_prov("B2")),
                composition_provenance=_comp(),
            ),
        ])
        gaps = ConstraintViolationDetector().detect(
            spec, context={"config": bad_config})
        labware_gaps = [g for g in gaps
                        if g.metadata.get("violation_type") == "labware_not_found"]
        # Two steps reference 'ghost_rack' as source → ONE Gap.
        assert len(labware_gaps) == 1
        assert sorted(labware_gaps[0].metadata["affected_steps"]) == [1, 2]


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
                description_provenance=_instr_prov("rack A1"), wells_provenance=_instr_prov("rack A1"),
            ),
            destination=LocationRef(
                description=destination_desc, well="B1",
                resolved_label=destination_resolved,
                description_provenance=_instr_prov("plate B1"), wells_provenance=_instr_prov("plate B1"),
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


# ============================================================================
# NamespaceSplitDetector — emits ambiguous Gaps when wells partition by
# letter prefix into ≥2 racks each fitting a distinct config labware
# ============================================================================

from nl2protocol.gap_resolution import NamespaceSplitDetector


_THREE_RACK_CONFIG = {
    "labware": {
        "rack_a": {"load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap", "slot": "1"},
        "rack_b": {"load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap", "slot": "2"},
        "rack_c": {"load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap", "slot": "3"},
    }
}


# Row-disjoint synthetic shapes for the positive split case. Real
# Opentrons labware is always-A-rooted rectangles which can't trigger
# the strict algorithm — see the resolver tests for the same monkey-
# patch pattern. The load_names match the Opentrons category convention
# (`opentrons_<count>_tuberack_*`) so the type filter for "tube rack"
# keeps both.
_DISJOINT_SHAPES = {
    "opentrons_8_tuberack_top_thing":
        ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"],
    "opentrons_8_tuberack_bot_thing":
        ["C1", "C2", "C3", "C4", "D1", "D2", "D3", "D4"],
}
_DISJOINT_CONFIG = {
    "labware": {
        "top": {"load_name": "opentrons_8_tuberack_top_thing", "slot": "1"},
        "bot": {"load_name": "opentrons_8_tuberack_bot_thing", "slot": "2"},
    }
}


def _patch_disjoint_shapes(monkeypatch):
    def fake_well_info(load_name):
        if load_name not in _DISJOINT_SHAPES:
            raise ValueError(f"unknown {load_name}")
        return {"valid_wells": _DISJOINT_SHAPES[load_name]}
    monkeypatch.setattr(
        "nl2protocol.models.labware.get_well_info", fake_well_info,
    )


class TestNamespaceSplitDetector:
    """Detects A/B/C-prefix partitions on a single description's well
    set when no single labware fits but each subgroup fits some labware."""

    def _western_blot_step(self, well_range, description="tube rack",
                            order=1):
        return ExtractedStep(
            order=order, action="transfer", composition_provenance=_comp(),
            source=LocationRef(
                description=description, well_range=well_range,
                description_provenance=_instr_prov("rack"),
                wells_provenance=_instr_prov(well_range),
            ),
        )

    def test_partition_across_row_disjoint_shapes_emits_one_gap(
        self, monkeypatch,
    ):
        _patch_disjoint_shapes(monkeypatch)
        # Wells {A1, B1, C1, D1} on row-disjoint shapes: top has A/B,
        # bot has C/D. Aggregate fits neither; each subgroup fits one.
        steps = [
            ExtractedStep(order=1, action="transfer",
                          composition_provenance=_comp(),
                          source=LocationRef(
                              description="tube rack",
                              wells=["A1", "B1", "C1", "D1"],
                              description_provenance=_instr_prov("ABCD"),
                              wells_provenance=_instr_prov("A1 B1 C1 D1"))),
        ]
        spec = _spec(steps)
        gaps = NamespaceSplitDetector().detect(
            spec, {"config": _DISJOINT_CONFIG})
        assert len(gaps) == 1
        g = gaps[0]
        assert g.kind == "ambiguous"
        assert g.severity == "blocker"
        assert g.metadata["subkind"] == "namespace_split"
        assert g.metadata["description"] == "tube rack"
        assert set(g.metadata["partition"].keys()) == {"A", "B", "C", "D"}

    def test_single_prefix_no_gap(self):
        spec = _spec([self._western_blot_step("A1-A4")])
        assert NamespaceSplitDetector().detect(
            spec, {"config": _THREE_RACK_CONFIG}) == []

    def test_partition_exists_but_no_fitting_labware_no_gap(self):
        # Two prefixes (A, B) against tiprack-only config — type filter
        # drops every candidate per subgroup → no detector hit.
        steps = [
            self._western_blot_step("A1-A4"),
            ExtractedStep(order=2, action="transfer",
                          composition_provenance=_comp(),
                          source=LocationRef(
                              description="tube rack", wells=["B1", "B2"],
                              description_provenance=_instr_prov("B"),
                              wells_provenance=_instr_prov("B1 B2"))),
        ]
        cfg = {"labware": {
            "tiprack_20": {"load_name": "opentrons_96_tiprack_20ul", "slot": "1"},
        }}
        spec = _spec(steps)
        assert NamespaceSplitDetector().detect(spec, {"config": cfg}) == []

    def test_missing_config_in_context_returns_empty(self):
        spec = _spec([self._western_blot_step("A1-A4")])
        assert NamespaceSplitDetector().detect(spec, context={}) == []

    def test_aggregate_fitting_single_labware_no_gap(self):
        # {A1-A4, B1, B2} all fits a single 24-rack — capability filter
        # leaves a survivor → no namespace-split gap fires.
        steps = [
            self._western_blot_step("A1-A4", order=1),
            self._western_blot_step("B1-B2", order=2),
        ]
        spec = _spec(steps)
        assert NamespaceSplitDetector().detect(
            spec, {"config": _THREE_RACK_CONFIG}) == []
