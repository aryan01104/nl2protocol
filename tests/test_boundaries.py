"""
tests/test_boundaries.py — explicit boundary-value tests at exact edges.

Centralizes boundary-value tests for behaviors with a documented numeric
threshold or edge — pipette ranges, well-state tolerance constants, Pydantic
field constraints. Hand-written to fill specific gaps surfaced by mutation
testing where existing tests skipped the *exact* edge value.

Most boundary coverage already lives in:
  - tests/test_constraints.py — TestPipetteCapacity, TestGetPipetteForVolume
  - tests/test_spec_validators.py — TestCoerceReplicates, TestValidateStepOrdering
  - tests/property/ — random sampling biases toward boundaries

This module fills the few exact-edge cases those tests skip. See
docs/TESTING.md "Boundary inventory" for the full map of which boundary is
tested where.
"""

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from nl2protocol.models.spec import (
    CompositionProvenance, ExtractedStep, Provenance, ProtocolSpec,
    WellContents, ProvenancedDuration,
)
from nl2protocol.validation.constraints import (
    PIPETTE_SPECS, WellState, WellStateTracker,
    get_pipette_for_volume, get_pipette_range,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def simple_config():
    config_path = Path(__file__).parent.parent / "test_cases" / "examples" / "simple_transfer" / "config.json"
    with open(config_path) as f:
        return json.load(f)


def _prov():
    return Provenance(source="instruction", reason="test", confidence=1.0)


def _comp():
    return CompositionProvenance(justification="test", grounding=["instruction"], confidence=1.0)


def _dummy_step():
    return ExtractedStep(
        order=1, action="delay",
        duration=ProvenancedDuration(value=1, unit="seconds", provenance=_prov()),
        composition_provenance=_comp(),
    )


def _spec(initial_contents=None):
    return ProtocolSpec(
        summary="boundary test", steps=[_dummy_step()],
        initial_contents=initial_contents or [],
    )


# ============================================================================
# 0.01 uL tolerance — the < vs <= boundary that survived mutation testing.
# Existing tests check 0.005 (clearly below) and 0.02 (clearly above) but
# skip the exact threshold value, leaving a mutation gap.
# ============================================================================

class TestWellHasLiquidAtExactThreshold:
    """well_has_liquid uses `> 0.01` — at EXACTLY 0.01uL, must return False.
    Mutating `>` to `>=` would flip this to True, so this test kills that mutant.
    """

    def test_well_at_exactly_001_ul_reports_no_liquid(self, simple_config):
        spec = _spec(initial_contents=[
            WellContents(labware="source_plate", well="A1", substance="trace", volume_ul=0.01)
        ])
        tracker = WellStateTracker(simple_config, spec)
        assert tracker.well_has_liquid("source_plate", "A1") is False


class TestWellStateRemoveAtExactToleranceBoundary:
    """WellState.remove uses `volume > volume_ul + 0.01` — at EXACTLY +0.01,
    returns True (the tolerance is inclusive on the equality side).
    Existing tests skip this exact value.
    """

    def test_remove_at_exact_tolerance_boundary_returns_true(self):
        ws = WellState(volume_ul=10.0)
        # remove(10 + 0.01) hits the exact tolerance boundary — must succeed.
        assert ws.remove(10.01) is True
        # And clamps to 0 (within-tolerance overdraw).
        assert ws.volume_ul == pytest.approx(0.0, abs=1e-9)


# ============================================================================
# Pipette range edges via get_pipette_for_volume — exact min/max already
# covered, but smallest-float just below min and just above max are not.
# Uses math.nextafter to produce the smallest representable float at the edge.
# ============================================================================

class TestPipetteRangeFloatEdges:
    """nextafter-based tests for the smallest float just outside each pipette
    range. Catches mutants that flip `<=` to `<` at the inclusive endpoint.
    """

    @pytest.mark.parametrize("spec,min_uL", [(s, lo) for s, (lo, _) in PIPETTE_SPECS.items()])
    def test_smallest_float_below_min_not_covered(self, spec, min_uL):
        config = {"pipettes": {"left": {"model": f"{spec}_single_gen2"}}}
        just_below = math.nextafter(min_uL, 0.0)
        assert get_pipette_for_volume(just_below, config) is None

    @pytest.mark.parametrize("spec,max_uL", [(s, hi) for s, (_, hi) in PIPETTE_SPECS.items()])
    def test_smallest_float_above_max_not_covered(self, spec, max_uL):
        config = {"pipettes": {"left": {"model": f"{spec}_single_gen2"}}}
        just_above = math.nextafter(max_uL, math.inf)
        assert get_pipette_for_volume(just_above, config) is None


# ============================================================================
# Pydantic confidence boundaries — Provenance.confidence is ge=0.0, le=1.0.
# Test exactly at the inclusive endpoints AND just outside.
# ============================================================================

class TestProvenanceConfidenceBoundaries:
    """Pydantic confidence constraints (ge=0.0, le=1.0) — exact-edge tests."""

    def test_confidence_zero_accepted(self):
        p = Provenance(source="instruction", reason="t", confidence=0.0)
        assert p.confidence == 0.0

    def test_confidence_one_accepted(self):
        p = Provenance(source="instruction", reason="t", confidence=1.0)
        assert p.confidence == 1.0

    def test_confidence_just_above_one_rejected(self):
        with pytest.raises(ValidationError):
            Provenance(source="instruction", reason="t", confidence=1.0001)

    def test_confidence_just_below_zero_rejected(self):
        with pytest.raises(ValidationError):
            Provenance(source="instruction", reason="t", confidence=-0.0001)


# ============================================================================
# Pydantic volume boundaries — ProvenancedVolume.value is gt=0.
# Test exactly 0 (rejected) and the smallest positive float (accepted).
# ============================================================================

class TestProvenancedVolumeBoundaries:
    """ProvenancedVolume.value has gt=0 — strictly positive only."""

    def test_volume_zero_rejected(self):
        from nl2protocol.models.spec import ProvenancedVolume
        with pytest.raises(ValidationError):
            ProvenancedVolume(value=0.0, unit="uL", exact=True, provenance=_prov())

    def test_smallest_positive_volume_accepted(self):
        from nl2protocol.models.spec import ProvenancedVolume
        # Smallest representable positive float — must satisfy gt=0
        smallest = math.nextafter(0.0, 1.0)
        v = ProvenancedVolume(value=smallest, unit="uL", exact=True, provenance=_prov())
        assert v.value == smallest
