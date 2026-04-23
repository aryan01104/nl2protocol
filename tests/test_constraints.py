"""
Tests for the constraint checker and well state tracker.

These tests are DETERMINISTIC — no LLM calls, no API keys needed.
They verify that the system correctly detects hardware conflicts,
missing labware, well state violations, and pipette capacity issues.
"""

import json
import pytest
from pathlib import Path

from nl2protocol.constraints import (
    ConstraintChecker, ConstraintCheckResult, WellStateTracker,
    Severity, ViolationType
)
from nl2protocol.extractor import (
    ProtocolSpec, ExtractedStep, ProvenancedVolume, ProvenancedDuration,
    Provenance, CompositionProvenance, LocationRef,
    PostAction, WellContents
)


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def qpcr_config():
    config_path = Path(__file__).parent.parent / "test_cases" / "examples" / "qpcr_standard_curve" / "config.json"
    with open(config_path) as f:
        return json.load(f)


@pytest.fixture
def simple_config():
    config_path = Path(__file__).parent.parent / "test_cases" / "examples" / "simple_transfer" / "config.json"
    with open(config_path) as f:
        return json.load(f)


@pytest.fixture
def serial_dilution_config():
    config_path = Path(__file__).parent.parent / "test_cases" / "examples" / "serial_dilution" / "config.json"
    with open(config_path) as f:
        return json.load(f)


def _prov(source="instruction", reason="test", confidence=1.0):
    """Shorthand for test provenance."""
    return Provenance(source=source, reason=reason, confidence=confidence)


def _comp(grounding=None, justification="test step", confidence=1.0):
    """Shorthand for test composition provenance."""
    return CompositionProvenance(
        justification=justification,
        grounding=grounding or ["instruction"],
        confidence=confidence,
    )


def _vol(value, unit="uL", exact=True, source="instruction"):
    """Shorthand for test volume."""
    return ProvenancedVolume(value=value, unit=unit, exact=exact, provenance=_prov(source=source))


def _dur(value, unit="seconds"):
    """Shorthand for test duration."""
    return ProvenancedDuration(value=value, unit=unit, provenance=_prov())


def make_spec(steps, **kwargs):
    """Helper to create a ProtocolSpec with minimal boilerplate."""
    defaults = {
        "protocol_type": "test",
        "summary": "test protocol",
        "reasoning": "",
        "explicit_volumes": [],
        "initial_contents": [],
        "missing_labware": [],
    }
    defaults.update(kwargs)
    return ProtocolSpec(steps=steps, **defaults)


# ============================================================================
# CONSTRAINT CHECKER: PIPETTE CAPACITY
# ============================================================================

class TestPipetteCapacity:
    """Tests that the constraint checker detects volume/pipette conflicts."""

    def test_mix_volume_exceeds_pipette_capacity(self, qpcr_config):
        """The 50uL mix on p20 problem — the bug that started it all."""
        spec = make_spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=_vol(10.0),
                source=LocationRef(description="reagent_tube_rack", well="A1"),
                destination=LocationRef(description="dilution_strip", well="A1"),
                post_actions=[PostAction(action="mix", repetitions=5,
                                         volume=_vol(50.0))],
                tip_strategy="new_tip_each",
                composition_provenance=_comp(),
            )
        ])
        checker = ConstraintChecker(qpcr_config)
        result = checker.check_all(spec)

        assert result.has_errors
        assert len(result.errors) == 1
        v = result.errors[0]
        assert v.violation_type == ViolationType.PIPETTE_CAPACITY
        assert "50.0" in v.what
        assert "p20" in v.what

    def test_volume_within_pipette_range_no_error(self, simple_config):
        """A 100uL transfer with p300 (range 20-300) should pass clean."""
        spec = make_spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=_vol(100.0),
                source=LocationRef(description="source_plate", well="A1"),
                destination=LocationRef(description="dest_plate", well="A1"),
                composition_provenance=_comp(),
            )
        ])
        checker = ConstraintChecker(simple_config)
        result = checker.check_all(spec)

        assert not result.has_errors

    def test_volume_exceeds_all_pipettes(self, qpcr_config):
        """1500uL is beyond both p20 (max 20) and p300 (max 300)."""
        spec = make_spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=_vol(1500.0),
                source=LocationRef(description="reagent_tube_rack", well="A1"),
                destination=LocationRef(description="dilution_strip", well="A1"),
                composition_provenance=_comp(),
            )
        ])
        checker = ConstraintChecker(qpcr_config)
        result = checker.check_all(spec)

        assert result.has_errors
        assert any("1500" in v.what for v in result.errors)

    def test_inferred_volume_still_checked(self, qpcr_config):
        """Inferred volumes are still checked — the robot physically can't handle 1500uL."""
        spec = make_spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=_vol(1500.0, source="inferred"),
                source=LocationRef(description="reagent_tube_rack", well="A1"),
                destination=LocationRef(description="dilution_strip", well="A1"),
                composition_provenance=_comp(),
            )
        ])
        checker = ConstraintChecker(qpcr_config)
        result = checker.check_all(spec)

        # Physical constraints apply regardless of provenance
        assert result.has_errors


# ============================================================================
# CONSTRAINT CHECKER: MISSING LABWARE
# ============================================================================

class TestMissingLabware:
    """Tests that missing labware is caught before pipeline proceeds."""

    def test_missing_labware_flagged(self, qpcr_config):
        """If LLM reports missing labware, constraint checker surfaces it."""
        spec = make_spec(
            [ExtractedStep(order=1, action="delay", duration=_dur(1, unit="minutes"), composition_provenance=_comp())],
            missing_labware=["sample_plate", "waste_reservoir"]
        )
        checker = ConstraintChecker(qpcr_config)
        result = checker.check_all(spec)

        assert result.has_errors
        assert len(result.errors) == 2
        assert all(v.violation_type == ViolationType.LABWARE_NOT_FOUND for v in result.errors)

    def test_no_missing_labware_passes(self, qpcr_config):
        """Empty missing_labware list means no errors from this check."""
        spec = make_spec(
            [ExtractedStep(order=1, action="delay", duration=_dur(1, unit="minutes"), composition_provenance=_comp())],
            missing_labware=[]
        )
        checker = ConstraintChecker(qpcr_config)
        result = checker.check_all(spec)

        labware_errors = [v for v in result.errors if v.violation_type == ViolationType.LABWARE_NOT_FOUND]
        assert len(labware_errors) == 0


# ============================================================================
# CONSTRAINT CHECKER: LABWARE RESOLUTION
# ============================================================================

class TestLabwareResolution:
    """Tests that unresolvable labware descriptions are caught."""

    def test_unresolvable_description_error(self, qpcr_config):
        """A description that matches nothing in config raises error."""
        spec = make_spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=_vol(10.0),
                source=LocationRef(description="centrifuge_vial", well="A1"),
                destination=LocationRef(description="dilution_strip", well="A1"),
                composition_provenance=_comp(),
            )
        ])
        checker = ConstraintChecker(qpcr_config)
        result = checker.check_all(spec)

        labware_errors = [v for v in result.errors if v.violation_type == ViolationType.LABWARE_NOT_FOUND]
        assert len(labware_errors) >= 1
        assert "centrifuge_vial" in labware_errors[0].what

    def test_matching_description_passes(self, simple_config):
        """Descriptions that match config labels don't raise errors."""
        spec = make_spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=_vol(100.0),
                source=LocationRef(description="source_plate", well="A1"),
                destination=LocationRef(description="dest_plate", well="A1"),
                composition_provenance=_comp(),
            )
        ])
        checker = ConstraintChecker(simple_config)
        result = checker.check_all(spec)

        labware_errors = [v for v in result.errors if v.violation_type == ViolationType.LABWARE_NOT_FOUND]
        assert len(labware_errors) == 0


# ============================================================================
# CONSTRAINT CHECKER: MODULE AVAILABILITY
# ============================================================================

class TestModuleAvailability:
    """Tests that module commands require matching modules in config."""

    def test_temperature_command_without_module(self, simple_config):
        """set_temperature without a temperature module in config is an error."""
        spec = make_spec([
            ExtractedStep(order=1, action="set_temperature", note="37°C", composition_provenance=_comp())
        ])
        checker = ConstraintChecker(simple_config)
        result = checker.check_all(spec)

        module_errors = [v for v in result.errors if v.violation_type == ViolationType.MODULE_NOT_FOUND]
        assert len(module_errors) == 1
        assert "temperature" in module_errors[0].what

    def test_temperature_command_with_module(self, qpcr_config):
        """set_temperature with a temperature module in config passes."""
        spec = make_spec([
            ExtractedStep(order=1, action="set_temperature", note="4°C", composition_provenance=_comp())
        ])
        checker = ConstraintChecker(qpcr_config)
        result = checker.check_all(spec)

        module_errors = [v for v in result.errors if v.violation_type == ViolationType.MODULE_NOT_FOUND]
        assert len(module_errors) == 0


# ============================================================================
# CONSTRAINT CHECKER: TIP SUFFICIENCY
# ============================================================================

class TestTipSufficiency:
    """Tests that tip usage estimates generate warnings when exceeding supply."""

    def test_many_transfers_warns_about_tips(self, simple_config):
        """96+ new-tip transfers with only 1 tiprack should warn."""
        # 100 individual transfers = 100 tips, but only 96 in the rack
        spec = make_spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=_vol(100.0),
                source=LocationRef(description="source_plate", wells=[f"A{i}" for i in range(1, 13)]),
                destination=LocationRef(description="dest_plate", wells=[f"A{i}" for i in range(1, 13)]),
                replicates=9,
                tip_strategy="new_tip_each",
                composition_provenance=_comp(),
            )
        ])
        checker = ConstraintChecker(simple_config)
        result = checker.check_all(spec)

        tip_warnings = [v for v in result.warnings if v.violation_type == ViolationType.TIP_INSUFFICIENT]
        assert len(tip_warnings) == 1


# ============================================================================
# WELL STATE TRACKER
# ============================================================================

class TestWellStateTracker:
    """Tests that the well state tracker detects logical errors."""

    def _dummy_step(self):
        return ExtractedStep(order=1, action="delay", duration=_dur(1, unit="seconds"), composition_provenance=_comp())

    def test_aspirate_from_empty_well_warns(self, simple_config):
        """Aspirating from a well that was never dispensed into produces a warning."""
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)

        tracker.aspirate("source_plate", "A1", 100.0)
        assert len(tracker.warnings) == 1
        assert "empty" in tracker.warnings[0]

    def test_aspirate_from_stocked_well_no_warning(self, simple_config):
        """Aspirating from a well with initial contents doesn't warn."""
        spec = make_spec([self._dummy_step()], initial_contents=[
            WellContents(labware="source_plate", well="A1", substance="DNA")
        ])
        tracker = WellStateTracker(simple_config, spec)

        tracker.aspirate("source_plate", "A1", 100.0)
        assert len(tracker.warnings) == 0

    def test_dispense_then_aspirate_no_warning(self, simple_config):
        """Normal flow: dispense into well, then aspirate from it."""
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)

        tracker.dispense("source_plate", "A1", 200.0, "buffer")
        tracker.aspirate("source_plate", "A1", 100.0)
        assert len(tracker.warnings) == 0
        assert tracker.get_volume("source_plate", "A1") == 100.0

    def test_aspirate_more_than_available_warns(self, simple_config):
        """Aspirating more than what's in the well produces a warning."""
        spec = make_spec([self._dummy_step()], initial_contents=[
            WellContents(labware="source_plate", well="A1", substance="DNA", volume_ul=50.0)
        ])
        tracker = WellStateTracker(simple_config, spec)

        tracker.aspirate("source_plate", "A1", 100.0)
        assert len(tracker.warnings) == 1
        assert "only has" in tracker.warnings[0]

    def test_well_overflow_warns(self, simple_config):
        """Dispensing beyond well capacity produces a warning."""
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)

        # corning_96_wellplate_360ul_flat has ~360uL capacity
        tracker.dispense("source_plate", "A1", 400.0, "buffer")
        assert len(tracker.warnings) == 1
        assert "capacity" in tracker.warnings[0]

    def test_substance_tracking(self, simple_config):
        """Tracker records what substances are in each well."""
        spec = make_spec([self._dummy_step()], initial_contents=[
            WellContents(labware="source_plate", well="A1", substance="DNA standard")
        ])
        tracker = WellStateTracker(simple_config, spec)

        tracker.dispense("source_plate", "A1", 50.0, "buffer")
        substances = tracker.get_substances("source_plate", "A1")
        assert "DNA standard" in substances
        assert "buffer" in substances


# ============================================================================
# VOLUME VALIDATION (validate_schema_against_spec)
# ============================================================================

class TestVolumeValidation:
    """Tests that user-specified volumes are preserved through generation."""

    def test_explicit_volumes_must_appear_in_schema(self, serial_dilution_config):
        """Volumes from instruction text (explicit_volumes) must all appear in schema."""
        from nl2protocol.extractor import SemanticExtractor
        from nl2protocol.parser import enrich_config_with_wells

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="transfer",
                    volume=_vol(100.0),
                    source=LocationRef(description="reservoir", well="A1"),
                    destination=LocationRef(description="plate", well="A1"),
                    composition_provenance=_comp(),
                )
            ],
            explicit_volumes=[100.0]
        )

        enriched = enrich_config_with_wells(serial_dilution_config)
        schema = SemanticExtractor.spec_to_schema(spec, enriched)
        mismatches = SemanticExtractor.validate_schema_against_spec(spec, schema)

        assert len(mismatches) == 0

    def test_inferred_mix_volume_not_treated_as_hard_constraint(self, qpcr_config):
        """50uL mix volume (not in explicit_volumes) should NOT cause validation failure."""
        from nl2protocol.extractor import SemanticExtractor
        from nl2protocol.parser import enrich_config_with_wells

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="transfer",
                    volume=_vol(10.0),
                    source=LocationRef(description="reagent_tube_rack", well="A1"),
                    destination=LocationRef(description="dilution_strip", well="A1"),
                    post_actions=[PostAction(action="mix", repetitions=5,
                                             volume=_vol(50.0))],
                    composition_provenance=_comp(),
                )
            ],
            explicit_volumes=[10.0],  # 50.0 is NOT here — it was inferred
            initial_contents=[
                WellContents(labware="reagent_tube_rack", well="A1", substance="DNA")
            ]
        )

        enriched = enrich_config_with_wells(qpcr_config)
        schema = SemanticExtractor.spec_to_schema(spec, enriched)
        mismatches = SemanticExtractor.validate_schema_against_spec(spec, schema)

        # The old code would fail here with "50.0uL not found in schema"
        # Now it passes because 50.0 is not in explicit_volumes
        assert len(mismatches) == 0


# ============================================================================
# SERIAL DILUTION CORRECTNESS
# ============================================================================

class TestSerialDilution:
    """Tests that serial dilution generates the correct transfer chain."""

    def test_serial_dilution_chain_complete(self, serial_dilution_config):
        """Serial dilution from A1 through B1-H1 should produce 7 sequential transfers."""
        from nl2protocol.extractor import SemanticExtractor
        from nl2protocol.parser import enrich_config_with_wells

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="serial_dilution",
                    volume=_vol(100.0),
                    source=LocationRef(description="reservoir", well="A1"),
                    destination=LocationRef(description="plate", well_range="A1-A6"),
                    tip_strategy="new_tip_each",
                    composition_provenance=_comp(),
                )
            ],
            initial_contents=[
                WellContents(labware="reservoir", well="A1", substance="stock solution")
            ]
        )

        enriched = enrich_config_with_wells(serial_dilution_config)
        schema = SemanticExtractor.spec_to_schema(spec, enriched)

        # Filter to just transfer commands
        transfers = [c for c in schema.commands if c.command_type == "transfer"]

        # Should be 6 transfers: A1→A1, A1→A2, A2→A3, A3→A4, A4→A5, A5→A6
        assert len(transfers) == 6

        # First transfer should be from source (reservoir) to first dest well
        assert transfers[0].source_labware == "reservoir"
        assert transfers[0].source_well == "A1"
        assert transfers[0].dest_well == "A1"

        # Subsequent transfers should be within the destination plate
        assert transfers[1].source_well == "A1"
        assert transfers[1].dest_well == "A2"
        assert transfers[2].source_well == "A2"
        assert transfers[2].dest_well == "A3"

    def test_serial_dilution_no_spurious_distribute(self, serial_dilution_config):
        """Serial dilution should NOT generate a distribute command."""
        from nl2protocol.extractor import SemanticExtractor
        from nl2protocol.parser import enrich_config_with_wells

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="serial_dilution",
                    volume=_vol(100.0),
                    source=LocationRef(description="reservoir", well="A1"),
                    destination=LocationRef(description="plate", well_range="A1-A6"),
                    tip_strategy="new_tip_each",
                    composition_provenance=_comp(),
                )
            ],
            initial_contents=[
                WellContents(labware="reservoir", well="A1", substance="stock")
            ]
        )

        enriched = enrich_config_with_wells(serial_dilution_config)
        schema = SemanticExtractor.spec_to_schema(spec, enriched)

        distributes = [c for c in schema.commands if c.command_type == "distribute"]
        assert len(distributes) == 0, "Serial dilution should not auto-distribute diluent"


# ============================================================================
# PROVENANCE DISPLAY
# ============================================================================

class TestProvenance:
    """Tests that the confirmation formatter shows provenance correctly."""

    def test_inferred_volume_tagged(self):
        """Inferred volumes should display [INFERRED] tag."""
        from nl2protocol.extractor import SemanticExtractor

        spec = make_spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=_vol(100.0, source="inferred"),
                source=LocationRef(description="plate_a", well="A1"),
                destination=LocationRef(description="plate_b", well="A1"),
                composition_provenance=_comp(),
            )
        ])

        output = SemanticExtractor.format_for_confirmation(spec)
        assert "[INFERRED]" in output

    def test_explicit_volume_no_tag(self):
        """Explicit volumes (not inferred, not approximate) get no tag."""
        from nl2protocol.extractor import SemanticExtractor

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="transfer",
                    volume=_vol(100.0),
                    source=LocationRef(description="plate_a", well="A1"),
                    destination=LocationRef(description="plate_b", well="A1"),
                    composition_provenance=_comp(),
                )
            ],
            explicit_volumes=[100.0]
        )

        output = SemanticExtractor.format_for_confirmation(spec)
        assert "100.0uL" in output
        assert "[INFERRED]" not in output.split("100.0uL")[0].split("\n")[-1]

    def test_post_action_volume_provenance_tagged(self):
        """Post-action volumes show their provenance source in the tag."""
        from nl2protocol.extractor import SemanticExtractor

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="transfer",
                    volume=_vol(10.0),
                    source=LocationRef(description="plate_a", well="A1"),
                    destination=LocationRef(description="plate_b", well="A1"),
                    post_actions=[PostAction(action="mix", repetitions=5,
                                             volume=_vol(50.0, source="domain_default", exact=False))],
                    composition_provenance=_comp(),
                )
            ],
            explicit_volumes=[10.0]
        )

        output = SemanticExtractor.format_for_confirmation(spec)
        assert "50.0uL [DOMAIN_DEFAULT]" in output
