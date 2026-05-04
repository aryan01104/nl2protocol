"""
Tests for failure mode scenarios.

Two categories:
  - DETERMINISTIC: Test constraint checker with hand-crafted specs. No API key needed.
  - LLM-DEPENDENT: Test full pipeline with real instructions. Requires ANTHROPIC_API_KEY.
    Run with: pytest --run-llm tests/test_failure_modes.py

Each test case corresponds to a directory in test_cases/failure_modes/ with
instruction.txt and config.json that can also be run manually:
    python -m nl2protocol -i test_cases/failure_modes/<case>/instruction.txt \\
                          -c test_cases/failure_modes/<case>/config.json
"""

import json
import os
import pytest
from pathlib import Path

from nl2protocol.validation.constraints import (
    ConstraintChecker, ViolationType, Severity, WellStateTracker
)
from nl2protocol.extraction import (
    ProtocolSpec, ExtractedStep, ProvenancedVolume, ProvenancedDuration,
    Provenance, CompositionProvenance, LocationRef,
    PostAction, WellContents, SemanticExtractor
)
from nl2protocol.validation.validate_config import validate_config


# ============================================================================
# HELPERS
# ============================================================================

FAILURE_DIR = Path(__file__).parent.parent / "test_cases" / "failure_modes"


def load_config(case_name: str) -> dict:
    with open(FAILURE_DIR / case_name / "config.json") as f:
        return json.load(f)


def load_instruction(case_name: str) -> str:
    with open(FAILURE_DIR / case_name / "instruction.txt") as f:
        return f.read().strip()


def make_spec(steps, **kwargs):
    defaults = {
        "protocol_type": "test",
        "summary": "test",
        "reasoning": "",
        "explicit_volumes": [],
        "initial_contents": [],
    }
    defaults.update(kwargs)
    return ProtocolSpec(steps=steps, **defaults)


def _prov(source="instruction", text="test cited text", confidence=1.0):
    """Shorthand for test provenance. `text` becomes cited_text for instruction-sourced,
    reasoning for non-instruction-sourced."""
    if source == "instruction":
        return Provenance(source=source, cited_text=text, confidence=confidence)
    return Provenance(source=source, reasoning=text, confidence=confidence)


def _comp(grounding=None, label="test step", confidence=1.0):
    """Shorthand for test composition provenance."""
    grounding = grounding or ["instruction"]
    kwargs = dict(
        step_cited_text=label,
        parameters_cited_texts=[label],
        parameters_reasoning=label,
        grounding=grounding,
        confidence=confidence,
    )
    if "domain_default" in grounding:
        kwargs["step_reasoning"] = "test domain expansion reasoning"
    return CompositionProvenance(**kwargs)


def _vol(value, unit="uL", exact=True, source="instruction"):
    """Shorthand for test volume."""
    return ProvenancedVolume(value=value, unit=unit, exact=exact, provenance=_prov(source=source))


def _dur(value, unit="seconds"):
    """Shorthand for test duration."""
    return ProvenancedDuration(value=value, unit=unit, provenance=_prov())


def _dummy():
    return ExtractedStep(order=1, action="delay", duration=_dur(1, unit="seconds"), composition_provenance=_comp())


# Load .env so API key is available in test environment
from dotenv import load_dotenv
load_dotenv()

# Mark for tests that need LLM
requires_llm = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="Requires ANTHROPIC_API_KEY"
)


# ============================================================================
# 1. PIPETTE INSUFFICIENT FOR INSTRUCTION
# ============================================================================

class TestPipetteInsufficient:
    """Config only has p20, but instruction requires 500uL transfer."""

    def test_500ul_exceeds_p20(self):
        config = load_config("pipette_insufficient")
        spec = make_spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=_vol(5.0),
                source=LocationRef(description="source_plate", well="A1"),
                destination=LocationRef(description="dest_plate", well="B1"),
                composition_provenance=_comp(),
            ),
            ExtractedStep(
                order=2, action="transfer",
                volume=_vol(500.0),
                source=LocationRef(description="reservoir", well="A1"),
                destination=LocationRef(description="dest_plate", well="B1"),
                composition_provenance=_comp(),
            ),
        ])
        result = ConstraintChecker(config).check_all(spec)

        assert result.has_errors
        errors = [v for v in result.errors if v.violation_type == ViolationType.PIPETTE_CAPACITY]
        assert len(errors) >= 1
        assert any("500" in e.what for e in errors)


# ============================================================================
# 2. LABWARE MISSING FROM CONFIG
# ============================================================================

class TestLabwareMissing:
    """Instruction references a 384-well plate not in config."""

    def test_384_plate_missing(self):
        config = load_config("labware_missing")
        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="distribute",
                    volume=_vol(50.0),
                    source=LocationRef(description="reagent_reservoir", well="A1"),
                    destination=LocationRef(description="assay_plate_384", well_range="A1-P24"),
                    composition_provenance=_comp(),
                )
            ],
        )
        result = ConstraintChecker(config).check_all(spec)


# ============================================================================
# 3. MODULE MISSING FROM CONFIG
# ============================================================================

class TestModuleMissing:
    """Instruction needs temperature module but config has none."""

    def test_temp_module_missing(self):
        config = load_config("module_missing")
        spec = make_spec([
            ExtractedStep(order=1, action="set_temperature", note="37°C", composition_provenance=_comp()),
            ExtractedStep(
                order=2, action="distribute",
                volume=_vol(200.0),
                source=LocationRef(description="reservoir", well="A1"),
                destination=LocationRef(description="culture_plate", well_range="A1-H12"),
                composition_provenance=_comp(),
            ),
        ])
        result = ConstraintChecker(config).check_all(spec)

        module_errors = [v for v in result.errors if v.violation_type == ViolationType.MODULE_NOT_FOUND]
        assert len(module_errors) == 1
        assert "temperature" in module_errors[0].what


# ============================================================================
# 4. COMBINED CONFIG GAPS (multiple problems at once)
# ============================================================================

class TestCombinedConfigGaps:
    """Magnetic bead cleanup: needs mag module, temp module, waste reservoir,
    ethanol reservoir, tube rack, PCR plate — config only has deep well plate."""

    def test_multiple_gaps(self):
        config = load_config("combined_config_gaps")
        spec = make_spec(
            [
                ExtractedStep(order=1, action="set_temperature", note="4°C", composition_provenance=_comp()),
                ExtractedStep(order=2, action="engage_magnets", composition_provenance=_comp()),
                ExtractedStep(
                    order=3, action="aspirate",
                    volume=_vol(150.0),
                    source=LocationRef(description="deep_well_plate", well_range="A1-H1"),
                    composition_provenance=_comp(),
                ),
            ],
        )
        result = ConstraintChecker(config).check_all(spec)

        assert result.has_errors
        # Should have: missing modules (temp + magnetic)
        missing_mod = [v for v in result.errors if v.violation_type == ViolationType.MODULE_NOT_FOUND]
        assert len(missing_mod) == 2


# ============================================================================
# 5. WRONG LABWARE LOAD_NAME
# ============================================================================

class TestWrongLoadname:
    """Config uses load_names that don't exist in Opentrons library."""

    def test_invalid_loadnames_caught_by_config_validator(self):
        config = load_config("wrong_loadname")
        result = validate_config(config)

        # Config validator should flag invalid load_names
        assert not result.valid or len(result.warnings) > 0
        # Check that at least one error/warning mentions the bad load names
        all_messages = [e.message for e in result.errors] + [w.message for w in result.warnings]
        assert any("corning_96_flat_wellplate" in m or "not found" in m.lower()
                    for m in all_messages)


# ============================================================================
# 6. PRIMED WELLS (quantities already in wells)
# ============================================================================

class TestPrimedWells:
    """Plate already has 80uL of cell suspension. Adding 20uL drug should
    result in 100uL total — well state tracker should track this correctly."""

    def test_well_state_tracks_primed_plus_added(self):
        config = load_config("primed_wells")
        wells_with_cells = [f"{r}{c}" for c in range(1, 7) for r in "ABCDEFGH"]
        initial = [
            WellContents(labware="destination_plate", well=w,
                         substance="cell suspension", volume_ul=80.0)
            for w in wells_with_cells
        ]
        spec = make_spec([_dummy()], initial_contents=initial)

        tracker = WellStateTracker(config, spec)

        # Add 20uL drug to A1
        tracker.dispense("destination_plate", "A1", 20.0, "Drug A")
        assert abs(tracker.get_volume("destination_plate", "A1") - 100.0) < 0.01
        assert "cell suspension" in tracker.get_substances("destination_plate", "A1")
        assert "Drug A" in tracker.get_substances("destination_plate", "A1")

    def test_overflow_detected_on_primed_wells(self):
        config = load_config("primed_wells")
        initial = [
            WellContents(labware="destination_plate", well="A1",
                         substance="cell suspension", volume_ul=300.0)
        ]
        spec = make_spec([_dummy()], initial_contents=initial)
        tracker = WellStateTracker(config, spec)

        # Adding 200uL to a well that already has 300uL in a 360uL plate → overflow
        tracker.dispense("destination_plate", "A1", 200.0, "Drug A")
        assert len(tracker.warnings) == 1
        assert "capacity" in tracker.warnings[0]


# ============================================================================
# 7. MISMATCHED PROTOCOL (instruction vs config for different experiments)
# ============================================================================

class TestMismatchedProtocol:
    """Instruction is serial dilution at 100uL, config has p20 (max 20uL)
    and thermocycler (irrelevant). Multiple mismatches."""

    def test_volume_and_labware_mismatch(self):
        config = load_config("mismatched_protocol")
        spec = make_spec([
            ExtractedStep(
                order=1, action="serial_dilution",
                volume=_vol(100.0),
                source=LocationRef(description="drug_stock"),
                destination=LocationRef(description="96_well_plate", well_range="A1-A12"),
                post_actions=[PostAction(action="mix", repetitions=5,
                                         volume=_vol(100.0))],
                composition_provenance=_comp(),
            )
        ])

        result = ConstraintChecker(config).check_all(spec)

        assert result.has_errors
        # Should flag: volume exceeds p20
        pip_errors = [v for v in result.errors if v.violation_type == ViolationType.PIPETTE_CAPACITY]
        assert len(pip_errors) >= 1


# ============================================================================
# LLM-DEPENDENT TESTS
# These test the full pipeline (extraction + constraint checking).
# They require an API key and make real LLM calls.
# Run with: ANTHROPIC_API_KEY=... pytest --run-llm tests/test_failure_modes.py
# ============================================================================

@requires_llm
class TestNonsensicalInstruction:
    """Instruction asks to centrifuge and read absorbance — valid lab work, but
    not liquid handling that an OT-2 can do. Current input validator classifies
    it as PROTOCOL (since it IS a protocol, just not a pipetting one).

    This test documents the gap: we need the system to distinguish between
    'lab protocol' and 'OT-2 liquid handling protocol'. For now, we verify
    the extraction step fails or produces an empty/unusable spec."""

    def test_no_liquid_handling_steps_extracted(self):
        from anthropic import Anthropic

        config = load_config("nonsensical_instruction")
        instruction = load_instruction("nonsensical_instruction")

        client = Anthropic()
        extractor = SemanticExtractor(client)
        spec = extractor.extract(instruction, config)

        if spec is not None:
            # If extraction succeeds, there should be no useful liquid handling steps
            liquid_actions = {"transfer", "distribute", "consolidate", "aspirate",
                              "dispense", "mix", "serial_dilution"}
            liquid_steps = [s for s in spec.steps if s.action in liquid_actions]
            assert len(liquid_steps) == 0, (
                f"Centrifuge/absorbance instruction should not produce liquid handling steps, "
                f"got: {[s.action for s in liquid_steps]}"
            )


@requires_llm
class TestMisspelledInstruction:
    """Heavily misspelled instruction — LLM should still extract correctly."""

    def test_llm_handles_misspellings(self):
        from anthropic import Anthropic

        config = load_config("misspelled_instruction")
        instruction = load_instruction("misspelled_instruction")

        client = Anthropic()
        extractor = SemanticExtractor(client)
        spec = extractor.extract(instruction, config)

        assert spec is not None
        # Should still extract the key volumes despite misspellings
        assert any(s.volume and s.volume.value == 50.0 for s in spec.steps)
        assert any(s.volume and s.volume.value == 5.0 for s in spec.steps)


@requires_llm
class TestEquivalentNames:
    """Instruction uses 'Eppendorf tubes', 'microplate', 'trough' —
    config has 'tube_rack', 'assay_plate', 'reagent_reservoir'.
    LLM should map these correctly."""

    def test_llm_maps_equivalent_names(self):
        from anthropic import Anthropic
        from nl2protocol.extraction import LabwareResolver

        config = load_config("equivalent_names")
        instruction = load_instruction("equivalent_names")

        client = Anthropic()
        extractor = SemanticExtractor(client)
        spec = extractor.extract(instruction, config)

        assert spec is not None

        # Descriptions should be the user's wording (not config labels)
        all_descriptions = []
        for step in spec.steps:
            if step.source:
                all_descriptions.append(step.source.description)
            if step.destination:
                all_descriptions.append(step.destination.description)
        assert len(all_descriptions) >= 2, f"Expected location refs, got none"

        # Labware resolver should map user wording to config labels
        resolver = LabwareResolver(config=config, client=client)
        resolved = resolver.resolve(spec)
        # All LocationRefs should have resolved_label set
        for step in resolved.steps:
            for ref in [step.source, step.destination]:
                if ref:
                    assert ref.resolved_label is not None, (
                        f"Resolver couldn't map '{ref.description}'"
                    )


@requires_llm
class TestCompactInstruction:
    """One-line Bradford assay instruction. Should parse correctly."""

    def test_compact_parses_correctly(self):
        from anthropic import Anthropic

        config = load_config("compact_instruction")
        instruction = load_instruction("compact_instruction")

        client = Anthropic()
        extractor = SemanticExtractor(client)
        spec = extractor.extract(instruction, config)

        assert spec is not None
        assert len(spec.steps) >= 3  # standards + samples + reagent + mix
        # Key volumes from instruction
        assert 10.0 in spec.explicit_volumes
        assert 200.0 in spec.explicit_volumes
        assert 150.0 in spec.explicit_volumes
