"""
Property-based tests for ConstraintChecker.check_all (the orchestrator).

Each property is one Post: clause from check_all's docstring contract,
re-stated as a universal: "for any spec satisfying Pre, this holds."

Built on Hypothesis strategies (defined inline below) that generate valid
ProtocolSpec instances and lab_config dicts. The Pydantic validators on
ProtocolSpec enforce additional constraints (step ordering 1..N, well
patterns, etc.) that the strategies must respect.
"""

import copy
from typing import List, Optional

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from nl2protocol.models.spec import (
    CompositionProvenance,
    ExtractedStep,
    LocationRef,
    Provenance,
    ProtocolSpec,
    ProvenancedVolume,
)
from nl2protocol.validation.constraints import (
    ConstraintChecker,
    Severity,
    ViolationType,
)


# Same suppression as test_constraint_properties.py — mutmut runs each test
# repeatedly which Hypothesis flags as differing executors.
settings.register_profile(
    "mutmut_compatible",
    suppress_health_check=[HealthCheck.differing_executors],
    deadline=None,  # Pydantic construction is slow; don't fail on per-example timing
)
settings.load_profile("mutmut_compatible")


# ============================================================================
# Strategies — composable building blocks for ProtocolSpec
# ============================================================================

# Wells matching the canonical regex `^[A-P](1[0-9]|2[0-4]|[1-9])$`.
WELL_NAME_REGEX = r"^[A-P](1[0-9]|2[0-4]|[1-9])$"


def well_names():
    return st.from_regex(WELL_NAME_REGEX, fullmatch=True)


# Wells inside a standard 96-well grid (rows A-H, cols 1-12). Used when we
# want generated steps to pass well-validity checks against a 96-well plate.
def wells_in_96_grid():
    return st.builds(
        lambda r, c: f"{r}{c}",
        r=st.sampled_from(list("ABCDEFGH")),
        c=st.integers(min_value=1, max_value=12).map(str),
    )


# Wells OUTSIDE a 96-well grid but matching the regex (rows I-P or cols 13-24).
def wells_outside_96_grid():
    return st.one_of(
        # Row outside A-H
        st.builds(
            lambda r, c: f"{r}{c}",
            r=st.sampled_from(list("IJKLMNOP")),
            c=st.integers(min_value=1, max_value=24).map(str),
        ),
        # Column outside 1-12
        st.builds(
            lambda r, c: f"{r}{c}",
            r=st.sampled_from(list("ABCDEFGHIJKLMNOP")),
            c=st.integers(min_value=13, max_value=24).map(str),
        ),
    )


def provenance():
    """Generate a Provenance valid under the new schema:
    cited_text REQUIRED for instruction-sourced; reasoning REQUIRED for
    domain_default/inferred-sourced. 'config' is no longer a valid source
    for extractor-emitted provenance (see ADR-0005).
    """
    return st.one_of(
        st.builds(
            Provenance,
            source=st.just("instruction"),
            cited_text=st.text(min_size=1, max_size=30),
            reasoning=st.none(),
            confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        ),
        st.builds(
            Provenance,
            source=st.sampled_from(["domain_default", "inferred"]),
            cited_text=st.none(),
            reasoning=st.text(min_size=1, max_size=30),
            confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        ),
    )


def composition_provenance():
    """Generate a CompositionProvenance valid under the new schema (ADR-0005):
    step_cited_text + parameters_cited_texts + parameters_reasoning REQUIRED;
    step_reasoning REQUIRED when grounding includes 'domain_default';
    grounding MUST include 'instruction' ('config' is invalid).
    """
    text_strat = st.text(min_size=1, max_size=30)
    return st.one_of(
        # Pure instruction-grounded
        st.builds(
            CompositionProvenance,
            step_cited_text=text_strat,
            step_reasoning=st.none(),
            parameters_cited_texts=st.lists(text_strat, min_size=1, max_size=3),
            parameters_reasoning=text_strat,
            grounding=st.just(["instruction"]),
            confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        ),
        # Compound grounding (instruction + domain_default) — step_reasoning required
        st.builds(
            CompositionProvenance,
            step_cited_text=text_strat,
            step_reasoning=text_strat,
            parameters_cited_texts=st.lists(text_strat, min_size=1, max_size=3),
            parameters_reasoning=text_strat,
            grounding=st.just(["instruction", "domain_default"]),
            confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
        ),
    )


def provenanced_volume(value_strat=None):
    if value_strat is None:
        value_strat = st.floats(min_value=0.1, max_value=10000.0, allow_nan=False, allow_infinity=False)
    return st.builds(
        ProvenancedVolume,
        value=value_strat,
        unit=st.just("uL"),
        exact=st.booleans(),
        provenance=provenance(),
    )


def location_ref(description_strat=None, well_strat=None, resolved=True):
    if description_strat is None:
        description_strat = st.sampled_from(["source_plate", "dest_plate"])
    if well_strat is None:
        well_strat = wells_in_96_grid()

    return st.builds(
        LocationRef,
        description=description_strat,
        well=well_strat,
        resolved_label=description_strat if resolved else st.none(),
        provenance=provenance(),
    )


def transfer_step(order, volume_strat=None, well_strat=None):
    """An ExtractedStep that triggers pipette/labware/well checks."""
    return st.builds(
        ExtractedStep,
        order=st.just(order),
        action=st.just("transfer"),
        volume=provenanced_volume(volume_strat),
        source=location_ref(
            description_strat=st.just("source_plate"),
            well_strat=well_strat,
        ),
        destination=location_ref(
            description_strat=st.just("dest_plate"),
            well_strat=well_strat,
        ),
        composition_provenance=composition_provenance(),
    )


@st.composite
def protocol_spec(draw, n_steps_max=4, volume_strat=None, well_strat=None):
    """A ProtocolSpec with N transfer steps, sequential orders 1..N."""
    n_steps = draw(st.integers(min_value=1, max_value=n_steps_max))
    steps = [
        draw(transfer_step(i, volume_strat=volume_strat, well_strat=well_strat))
        for i in range(1, n_steps + 1)
    ]
    return ProtocolSpec(summary="generated spec", steps=steps)


# ============================================================================
# Lab config strategies
# ============================================================================

PIPETTE_MODELS = [
    "p10_single_gen2", "p20_single_gen2", "p50_single_gen2",
    "p300_single_gen2", "p1000_single_gen2",
]


def labware_block():
    return st.fixed_dictionaries({
        "source_plate": st.just({"load_name": "corning_96_wellplate_360ul_flat", "slot": "1"}),
        "dest_plate":   st.just({"load_name": "corning_96_wellplate_360ul_flat", "slot": "2"}),
        "tiprack":      st.just({"load_name": "opentrons_96_tiprack_300ul", "slot": "3"}),
    })


def lab_config(pipette_models=None):
    if pipette_models is None:
        pipette_models = PIPETTE_MODELS
    return st.builds(
        lambda labware, model: {
            "labware": labware,
            "pipettes": {"left": {"model": model, "tipracks": ["tiprack"]}},
        },
        labware=labware_block(),
        model=st.sampled_from(pipette_models),
    )


# A config with ONLY p20 (range [1, 20] uL) — used to force out-of-range volumes.
def p20_only_config():
    return st.builds(
        lambda labware: {
            "labware": labware,
            "pipettes": {"left": {"model": "p20_single_gen2", "tipracks": ["tiprack"]}},
        },
        labware=labware_block(),
    )


# ============================================================================
# Property #1 — determinism
# ============================================================================

class TestCheckAllDeterminism:
    """Same input twice → same violations (Post: pure, deterministic)."""

    @given(spec=protocol_spec(), config=lab_config())
    @settings(max_examples=50)
    def test_check_all_returns_same_violations_on_repeat(self, spec, config):
        checker = ConstraintChecker(config)
        r1 = checker.check_all(spec)
        r2 = checker.check_all(spec)

        assert len(r1.violations) == len(r2.violations)
        for v1, v2 in zip(r1.violations, r2.violations):
            assert (v1.violation_type, v1.severity, v1.step, v1.what) == \
                   (v2.violation_type, v2.severity, v2.step, v2.what)


# ============================================================================
# Property #2 — spec purity (no mutation of input spec)
# ============================================================================

class TestCheckAllSpecPurity:
    """Input spec is not mutated by check_all (Post: does not mutate `spec`)."""

    @given(spec=protocol_spec(), config=lab_config())
    @settings(max_examples=50)
    def test_check_all_does_not_mutate_spec(self, spec, config):
        snapshot = spec.model_dump_json()
        ConstraintChecker(config).check_all(spec)
        assert spec.model_dump_json() == snapshot


# ============================================================================
# Property #3 — config purity
# ============================================================================

class TestCheckAllConfigPurity:
    """self.config is not mutated by check_all."""

    @given(spec=protocol_spec(), config=lab_config())
    @settings(max_examples=50)
    def test_check_all_does_not_mutate_config(self, spec, config):
        import json
        snapshot = json.dumps(config, sort_keys=True)
        ConstraintChecker(config).check_all(spec)
        assert json.dumps(config, sort_keys=True) == snapshot


# ============================================================================
# Property #4 — every violation has all six structured fields populated
# ============================================================================

class TestCheckAllViolationStructure:
    """Every violation in any result has the full 6-field structure."""

    @given(spec=protocol_spec(), config=lab_config())
    @settings(max_examples=50)
    def test_every_violation_is_fully_structured(self, spec, config):
        result = ConstraintChecker(config).check_all(spec)
        for v in result.violations:
            assert v.violation_type is not None
            assert v.severity in {Severity.ERROR, Severity.WARNING, Severity.INFO}
            assert isinstance(v.step, int) and v.step >= 1
            assert isinstance(v.what, str) and v.what
            assert isinstance(v.why, str) and v.why
            assert isinstance(v.suggestion, str) and v.suggestion


# ============================================================================
# Property #5 — volume violation completeness
# ============================================================================

class TestCheckAllVolumeCompleteness:
    """If every step volume exceeds the only pipette's max range, there is at
    least one PIPETTE_CAPACITY violation. With p20 only ([1, 20] uL), any
    volume > 20 uL is uncoverable — must be flagged.
    """

    @given(
        spec=protocol_spec(volume_strat=st.floats(min_value=21.0, max_value=10000.0,
                                                   allow_nan=False, allow_infinity=False)),
        config=p20_only_config(),
    )
    @settings(max_examples=50)
    def test_oversized_volumes_always_produce_capacity_violation(self, spec, config):
        result = ConstraintChecker(config).check_all(spec)
        capacity_violations = [
            v for v in result.violations
            if v.violation_type == ViolationType.PIPETTE_CAPACITY
        ]
        assert len(capacity_violations) >= 1


# ============================================================================
# Property #6 — volume violation soundness
# ============================================================================

class TestCheckAllVolumeSoundness:
    """If a pipette covers every step volume, no PIPETTE_CAPACITY violation
    is produced. With p1000 only ([100, 1000] uL), any volume in [100, 1000]
    uL must NOT be flagged for capacity.
    """

    @given(
        spec=protocol_spec(volume_strat=st.floats(min_value=100.0, max_value=1000.0,
                                                   allow_nan=False, allow_infinity=False)),
    )
    @settings(max_examples=50)
    def test_in_range_volumes_produce_no_capacity_violations(self, spec):
        config = {
            "labware": {
                "source_plate": {"load_name": "corning_96_wellplate_360ul_flat", "slot": "1"},
                "dest_plate":   {"load_name": "corning_96_wellplate_360ul_flat", "slot": "2"},
                "tiprack":      {"load_name": "opentrons_96_tiprack_1000ul", "slot": "3"},
            },
            "pipettes": {"left": {"model": "p1000_single_gen2", "tipracks": ["tiprack"]}},
        }
        result = ConstraintChecker(config).check_all(spec)
        capacity_violations = [
            v for v in result.violations
            if v.violation_type == ViolationType.PIPETTE_CAPACITY
        ]
        assert capacity_violations == []


# ============================================================================
# Property #7 — well-validity completeness
# ============================================================================

class TestCheckAllWellValidityCompleteness:
    """Any step whose source/destination references a well outside the
    labware grid (rows I-P or cols 13-24, given a 96-well plate) must produce
    at least one WELL_INVALID violation.
    """

    @given(
        spec=protocol_spec(well_strat=wells_outside_96_grid()),
        config=lab_config(),
    )
    @settings(max_examples=50)
    def test_out_of_grid_well_always_flags_invalid(self, spec, config):
        result = ConstraintChecker(config).check_all(spec)
        well_violations = [
            v for v in result.violations
            if v.violation_type == ViolationType.WELL_INVALID
        ]
        # Each step has source AND destination, both with out-of-grid wells.
        # Expect at least one WELL_INVALID violation per step (could be more).
        assert len(well_violations) >= len(spec.steps)
