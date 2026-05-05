"""
Tests for the constraint checker and well state tracker.

These tests are DETERMINISTIC — no LLM calls, no API keys needed.
They verify that the system correctly detects hardware conflicts,
missing labware, well state violations, and pipette capacity issues.
"""

import json
import pytest
from pathlib import Path

from nl2protocol.validation.constraints import (
    ConstraintChecker, ConstraintCheckResult, WellStateTracker, WellState,
    Severity, ViolationType,
    get_pipette_range, get_pipette_for_volume, get_all_pipette_ranges,
    PIPETTE_SPECS,
)
from nl2protocol.extraction import (
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


def _prov(source="instruction", text="test cited text", confidence=1.0):
    """Shorthand for test provenance. `text` is cited_text for instruction-sourced,
    reasoning otherwise."""
    if source == "instruction":
        return Provenance(source=source, cited_text=text, confidence=confidence)
    return Provenance(source=source, reasoning=text, confidence=confidence)


def _loc(**kwargs):
    """LocationRef with default test provenance — added when LocationRef.provenance
    became required at the field level (ADR-0007). Lets pre-existing test
    fixtures construct LocationRefs without each call having to spell out
    a fresh provenance object."""
    kwargs.setdefault("provenance", _prov())
    return LocationRef(**kwargs)


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


def make_spec(steps, **kwargs):
    """Helper to create a ProtocolSpec with minimal boilerplate."""
    defaults = {
        "protocol_type": "test",
        "summary": "test protocol",
        "reasoning": "",
        "explicit_volumes": [],
        "initial_contents": [],
    }
    defaults.update(kwargs)
    return ProtocolSpec(steps=steps, **defaults)


# ============================================================================
# get_pipette_range — contract tests
# Each test maps to one clause of the function's docstring contract.
# ============================================================================

class TestGetPipetteRange:
    """Contract tests for get_pipette_range. One test per docstring clause."""

    # Post: returns the spec's range when model.lower() starts with "<spec>_"
    def test_returns_range_for_each_recognized_spec(self):
        for spec, expected in PIPETTE_SPECS.items():
            model = f"{spec}_single_gen2"
            assert get_pipette_range(model) == expected, f"failed on {model}"

    # Post: matching is case-insensitive (uses model.lower())
    def test_matching_is_case_insensitive(self):
        assert get_pipette_range("P300_SINGLE_GEN2") == PIPETTE_SPECS["p300"]

    # Post: returns None when no recognized spec matches
    def test_returns_none_for_unrecognized_model(self):
        assert get_pipette_range("p9999_single_gen2") is None
        assert get_pipette_range("not_a_pipette") is None

    # Post: matches "<spec>_" prefix, NOT arbitrary substring.
    # This is the regression test for the p10-vs-p1000 collision: the old
    # substring-based code returned (1.0, 10.0) for any p1000 model because
    # "p10" appears at the start of "p1000". The contract specifies prefix-
    # plus-underscore matching to disambiguate.
    def test_p1000_does_not_collide_with_p10(self):
        assert get_pipette_range("p1000_single_flex") == PIPETTE_SPECS["p1000"]
        assert get_pipette_range("p1000_multi_gen3") == PIPETTE_SPECS["p1000"]

    # Post: prefix must be followed by underscore — bare spec without
    # channels/generation does not match.
    def test_bare_spec_without_underscore_returns_none(self):
        assert get_pipette_range("p300") is None

    # Raises: AttributeError when model is not a string
    def test_raises_attribute_error_on_non_string(self):
        with pytest.raises(AttributeError):
            get_pipette_range(None)


# ============================================================================
# get_pipette_for_volume — contract tests
# ============================================================================

class TestGetPipetteForVolume:
    """Contract tests for get_pipette_for_volume. One test per docstring clause."""

    # Post: returns mount when its pipette range covers volume
    def test_returns_mount_whose_range_covers_volume(self):
        config = {"pipettes": {"left": {"model": "p300_single_gen2"}}}
        assert get_pipette_for_volume(150.0, config) == "left"

    # Post: range is inclusive at both ends
    def test_range_endpoints_are_inclusive(self):
        config = {"pipettes": {"left": {"model": "p300_single_gen2"}}}  # [20, 300]
        assert get_pipette_for_volume(20.0, config) == "left"
        assert get_pipette_for_volume(300.0, config) == "left"

    # Post: when multiple pipettes cover, first in dict-insertion order wins
    def test_first_in_insertion_order_wins(self):
        # Both p300 [20,300] and p1000 [100,1000] cover 200uL.
        # Dict order is left first, then right.
        config_left_first = {
            "pipettes": {
                "left": {"model": "p300_single_gen2"},
                "right": {"model": "p1000_single_flex"},
            }
        }
        assert get_pipette_for_volume(200.0, config_left_first) == "left"

        # Reverse insertion order: right is now first.
        config_right_first = {
            "pipettes": {
                "right": {"model": "p1000_single_flex"},
                "left": {"model": "p300_single_gen2"},
            }
        }
        assert get_pipette_for_volume(200.0, config_right_first) == "right"

    # Post: returns None when no pipette range covers volume
    def test_returns_none_when_volume_outside_all_ranges(self):
        config = {"pipettes": {"left": {"model": "p20_single_gen2"}}}  # [1, 20]
        assert get_pipette_for_volume(500.0, config) is None
        assert get_pipette_for_volume(0.5, config) is None

    # Post: returns None when "pipettes" key is missing
    def test_returns_none_when_pipettes_key_missing(self):
        assert get_pipette_for_volume(100.0, {}) is None

    # Post: returns None when "pipettes" is empty
    def test_returns_none_when_pipettes_empty(self):
        assert get_pipette_for_volume(100.0, {"pipettes": {}}) is None

    # Post: pipettes with unrecognized models are skipped
    def test_unrecognized_model_skipped(self):
        config = {"pipettes": {"left": {"model": "garbage_pipette_model"}}}
        assert get_pipette_for_volume(100.0, config) is None

    # Raises: KeyError when a pipette entry is missing "model"
    def test_raises_key_error_when_model_missing(self):
        config = {"pipettes": {"left": {"tipracks": []}}}  # no "model" key
        with pytest.raises(KeyError):
            get_pipette_for_volume(100.0, config)


# ============================================================================
# get_all_pipette_ranges — contract tests
# ============================================================================

class TestGetAllPipetteRanges:
    """Contract tests for get_all_pipette_ranges. One test per docstring clause."""

    # Post: returns {mount: range} for every recognized pipette
    def test_returns_range_for_every_recognized_mount(self):
        config = {
            "pipettes": {
                "left": {"model": "p300_single_gen2"},
                "right": {"model": "p1000_single_flex"},
            }
        }
        result = get_all_pipette_ranges(config)
        assert result == {
            "left": PIPETTE_SPECS["p300"],
            "right": PIPETTE_SPECS["p1000"],
        }

    # Post: returns {} when "pipettes" key missing
    def test_returns_empty_when_pipettes_key_missing(self):
        assert get_all_pipette_ranges({}) == {}

    # Post: returns {} when "pipettes" is empty
    def test_returns_empty_when_pipettes_empty(self):
        assert get_all_pipette_ranges({"pipettes": {}}) == {}

    # Post: unrecognized models are silently omitted (NOT included as None)
    def test_unrecognized_model_silently_omitted(self):
        config = {
            "pipettes": {
                "left": {"model": "p300_single_gen2"},
                "right": {"model": "garbage_model"},
            }
        }
        result = get_all_pipette_ranges(config)
        assert result == {"left": PIPETTE_SPECS["p300"]}
        assert "right" not in result  # not present at all, not a None entry

    # Post: every model unrecognized → returns {}
    def test_returns_empty_when_all_models_unrecognized(self):
        config = {"pipettes": {"left": {"model": "garbage"}, "right": {"model": "junk"}}}
        assert get_all_pipette_ranges(config) == {}

    # Raises: KeyError when a pipette entry lacks "model"
    def test_raises_key_error_when_model_missing(self):
        config = {"pipettes": {"left": {"tipracks": []}}}
        with pytest.raises(KeyError):
            get_all_pipette_ranges(config)

    # Side effects: does not mutate the input config
    def test_does_not_mutate_input(self):
        config = {"pipettes": {"left": {"model": "p300_single_gen2"}}}
        snapshot = json.dumps(config, sort_keys=True)
        get_all_pipette_ranges(config)
        assert json.dumps(config, sort_keys=True) == snapshot


# ============================================================================
# WellState.add — contract tests
# ============================================================================

class TestWellStateAdd:
    """Contract tests for WellState.add. One test per docstring clause."""

    # Post: volume_ul increments by exactly volume (positive case)
    def test_volume_ul_increments_by_exact_amount(self):
        ws = WellState()
        ws.add(50.0, "water")
        assert ws.volume_ul == 50.0
        ws.add(25.0, "water")
        assert ws.volume_ul == 75.0

    # Post: adding 0 leaves volume_ul unchanged
    def test_adding_zero_leaves_volume_unchanged(self):
        ws = WellState(volume_ul=10.0)
        ws.add(0.0, "water")
        assert ws.volume_ul == 10.0

    # Post: non-empty new substance is appended at the end
    def test_new_substance_appended(self):
        ws = WellState()
        ws.add(10.0, "water")
        ws.add(10.0, "buffer")
        assert ws.substances == ["water", "buffer"]

    # Post: empty substance is NOT appended (falsy guard)
    def test_empty_substance_not_appended(self):
        ws = WellState()
        ws.add(10.0, "")
        assert ws.substances == []
        assert ws.volume_ul == 10.0  # but volume still added

    # Post: duplicate substance is NOT appended again
    def test_duplicate_substance_not_appended(self):
        ws = WellState()
        ws.add(10.0, "water")
        ws.add(10.0, "water")
        assert ws.substances == ["water"]

    # Post: substance dedup is case-sensitive (exact string equality)
    def test_substance_dedup_is_case_sensitive(self):
        ws = WellState()
        ws.add(10.0, "Water")
        ws.add(10.0, "water")
        assert ws.substances == ["Water", "water"]

    # Post: no overflow check — caller is responsible
    def test_no_overflow_check(self):
        ws = WellState()
        ws.add(1_000_000.0, "absurd_volume")  # well past any well capacity
        assert ws.volume_ul == 1_000_000.0  # accepted without complaint


# ============================================================================
# WellState.remove — contract tests
# ============================================================================

class TestWellStateRemove:
    """Contract tests for WellState.remove. One test per docstring clause."""

    # Post: returns True and decrements when sufficient
    def test_returns_true_and_decrements_when_sufficient(self):
        ws = WellState(volume_ul=100.0)
        ok = ws.remove(30.0)
        assert ok is True
        assert ws.volume_ul == 70.0

    # Post: returns False and leaves volume_ul unchanged when insufficient
    def test_returns_false_and_unchanged_when_insufficient(self):
        ws = WellState(volume_ul=10.0)
        ok = ws.remove(50.0)
        assert ok is False
        assert ws.volume_ul == 10.0

    # Post: tolerance — remove(volume_ul + 0.005) succeeds (within 0.01)
    def test_within_tolerance_overdraw_succeeds(self):
        ws = WellState(volume_ul=10.0)
        ok = ws.remove(10.005)
        assert ok is True

    # Post: tolerance — remove(volume_ul + 0.5) fails (outside 0.01)
    def test_outside_tolerance_overdraw_fails(self):
        ws = WellState(volume_ul=10.0)
        ok = ws.remove(10.5)
        assert ok is False
        assert ws.volume_ul == 10.0

    # Post: within-tolerance overdraw clamps volume_ul to exactly 0
    def test_within_tolerance_overdraw_clamps_to_zero(self):
        ws = WellState(volume_ul=10.0)
        ws.remove(10.005)  # within tolerance
        assert ws.volume_ul == 0.0  # not negative

    # Post: substances is never mutated by remove (success or failure)
    def test_substances_never_mutated(self):
        ws = WellState(volume_ul=100.0)
        ws.add(50.0, "water")
        ws.add(50.0, "buffer")
        snapshot = list(ws.substances)

        ws.remove(30.0)              # success
        assert ws.substances == snapshot

        ws.remove(10_000.0)          # failure
        assert ws.substances == snapshot

    # Post: remove(0) returns True and leaves state unchanged
    def test_remove_zero_is_noop_and_returns_true(self):
        ws = WellState(volume_ul=10.0)
        ws.add(0.0, "water")
        ok = ws.remove(0.0)
        assert ok is True
        assert ws.volume_ul == 10.0


# ============================================================================
# ConstraintChecker.check_all — orchestration contract tests
# (Per-check tests live in TestPipetteCapacity, TestLabwareResolution, etc.
#  These tests target the orchestration promises: empty iff clean, multi-step
#  accumulation, no double-reporting, structured-field completeness, purity.)
# ============================================================================

class TestCheckAllOrchestration:
    """Contract tests for ConstraintChecker.check_all orchestration."""

    def _clean_step(self, simple_config):
        """A step that should pass every check against simple_config."""
        return ExtractedStep(
            order=1, action="transfer",
            volume=_vol(100.0),  # within p300 range
            source=_loc(description="source_plate", well="A1", resolved_label="source_plate"),
            destination=_loc(description="dest_plate", well="A1", resolved_label="dest_plate"),
            composition_provenance=_comp(),
        )

    # Post: empty violations list iff every check passes
    def test_clean_spec_returns_empty_violations(self, simple_config):
        spec = make_spec([self._clean_step(simple_config)])
        result = ConstraintChecker(simple_config).check_all(spec)
        assert result.violations == []

    # Post: result.bool() / has_errors / errors / warnings are coherent
    def test_clean_spec_result_is_truthy_and_no_errors(self, simple_config):
        spec = make_spec([self._clean_step(simple_config)])
        result = ConstraintChecker(simple_config).check_all(spec)
        assert bool(result) is True
        assert result.has_errors is False
        assert result.errors == []

    # Post: multi-step spec accumulates violations from each step
    def test_multistep_violations_accumulate(self, simple_config):
        # Two steps, each requesting a volume that exceeds the only pipette
        bad_step_1 = ExtractedStep(
            order=1, action="transfer", volume=_vol(50000.0),  # 50mL — exceeds anything
            source=_loc(description="source_plate", well="A1"),
            destination=_loc(description="dest_plate", well="A1"),
            composition_provenance=_comp(),
        )
        bad_step_2 = ExtractedStep(
            order=2, action="transfer", volume=_vol(50000.0),
            source=_loc(description="source_plate", well="B1"),
            destination=_loc(description="dest_plate", well="B1"),
            composition_provenance=_comp(),
        )
        spec = make_spec([bad_step_1, bad_step_2])
        result = ConstraintChecker(simple_config).check_all(spec)

        steps_with_violations = {v.step for v in result.violations}
        assert 1 in steps_with_violations
        assert 2 in steps_with_violations

    # Post: every violation has all six structured fields populated
    def test_every_violation_has_all_structured_fields(self, simple_config):
        bad_step = ExtractedStep(
            order=1, action="transfer", volume=_vol(50000.0),
            source=_loc(description="source_plate", well="A1"),
            destination=_loc(description="dest_plate", well="A1"),
            composition_provenance=_comp(),
        )
        result = ConstraintChecker(simple_config).check_all(make_spec([bad_step]))
        assert len(result.violations) >= 1

        for v in result.violations:
            assert v.violation_type is not None
            assert v.severity is not None
            assert isinstance(v.step, int)
            assert v.what  # non-empty string
            assert v.why
            assert v.suggestion

    # Post: same spec → same result (deterministic / pure)
    def test_check_all_is_deterministic(self, simple_config):
        spec = make_spec([self._clean_step(simple_config)])
        checker = ConstraintChecker(simple_config)
        r1 = checker.check_all(spec)
        r2 = checker.check_all(spec)
        # Compare violation lists field-by-field
        assert len(r1.violations) == len(r2.violations)
        for v1, v2 in zip(r1.violations, r2.violations):
            assert (v1.violation_type, v1.severity, v1.step, v1.what) == \
                   (v2.violation_type, v2.severity, v2.step, v2.what)

    # Side effects: does not mutate spec
    def test_does_not_mutate_spec(self, simple_config):
        spec = make_spec([self._clean_step(simple_config)])
        snapshot = spec.model_dump_json()
        ConstraintChecker(simple_config).check_all(spec)
        assert spec.model_dump_json() == snapshot

    # Side effects: does not mutate self.config
    def test_does_not_mutate_config(self, simple_config):
        spec = make_spec([self._clean_step(simple_config)])
        snapshot = json.dumps(simple_config, sort_keys=True)
        ConstraintChecker(simple_config).check_all(spec)
        assert json.dumps(simple_config, sort_keys=True) == snapshot


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
                source=_loc(description="reagent_tube_rack", well="A1"),
                destination=_loc(description="dilution_strip", well="A1"),
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
                source=_loc(description="source_plate", well="A1"),
                destination=_loc(description="dest_plate", well="A1"),
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
                source=_loc(description="reagent_tube_rack", well="A1"),
                destination=_loc(description="dilution_strip", well="A1"),
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
                source=_loc(description="reagent_tube_rack", well="A1"),
                destination=_loc(description="dilution_strip", well="A1"),
                composition_provenance=_comp(),
            )
        ])
        checker = ConstraintChecker(qpcr_config)
        result = checker.check_all(spec)

        # Physical constraints apply regardless of provenance
        assert result.has_errors


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
                source=_loc(description="centrifuge_vial", well="A1"),
                destination=_loc(description="dilution_strip", well="A1"),
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
                source=_loc(description="source_plate", well="A1"),
                destination=_loc(description="dest_plate", well="A1"),
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

    def test_many_transfers_errors_about_tips(self, simple_config):
        """96+ new-tip transfers with only 1 tiprack should error."""
        # 108 individual transfers = 108 tips, but only 96 in the rack
        spec = make_spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=_vol(100.0),
                source=_loc(description="source_plate", wells=[f"A{i}" for i in range(1, 13)]),
                destination=_loc(description="dest_plate", wells=[f"A{i}" for i in range(1, 13)]),
                replicates=9,
                tip_strategy="new_tip_each",
                composition_provenance=_comp(),
            )
        ])
        checker = ConstraintChecker(simple_config)
        result = checker.check_all(spec)

        tip_errors = [v for v in result.errors if v.violation_type == ViolationType.TIP_INSUFFICIENT]
        assert len(tip_errors) == 1


# ============================================================================
# WellStateTracker.dispense — contract-gap tests
# (Existing TestWellStateTracker covers the basic positive cases. These fill
#  the gaps the contract surfaces: falsy-labware no-op, cumulative summing,
#  no-warning negative case, default-volume note in overflow warning.)
# ============================================================================

class TestWellStateTrackerDispense:
    """Contract-gap tests for WellStateTracker.dispense."""

    def _dummy_step(self):
        return ExtractedStep(order=1, action="delay", duration=_dur(1, unit="seconds"), composition_provenance=_comp())

    # Post: cumulative dispenses sum exactly (delegates to WellState.add)
    def test_cumulative_dispenses_sum(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)

        tracker.dispense("source_plate", "A1", 50.0, "buffer")
        tracker.dispense("source_plate", "A1", 25.0, "buffer")
        tracker.dispense("source_plate", "A1", 75.0, "buffer")
        assert tracker.get_volume("source_plate", "A1") == 150.0

    # Post: no overflow warning when volume stays within capacity
    def test_no_overflow_warning_within_capacity(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)

        # corning_96_wellplate_360ul_flat capacity ≈ 360uL
        tracker.dispense("source_plate", "A1", 200.0, "buffer")
        capacity_warnings = [w for w in tracker.warnings if "capacity" in w]
        assert len(capacity_warnings) == 0

    # Defensive path: dispense to a labware not in config should not crash.
    # The well-capacity lookup returns None gracefully for unknown labware,
    # so no overflow check fires but the dispense itself still records.
    # This test was added after mutation testing surfaced that the defensive
    # `{}` default in _get_well_capacity was untested through the public API.
    def test_dispense_to_unknown_labware_does_not_crash(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)

        tracker.dispense("never_in_config", "A1", 100.0, "buffer")

        assert tracker.get_volume("never_in_config", "A1") == 100.0
        # No overflow warning because capacity is unknown for this labware
        capacity_warnings = [w for w in tracker.warnings if "capacity" in w]
        assert len(capacity_warnings) == 0

    # Post: overflow warning includes default-volume NOTE for assumed-volume wells
    def test_overflow_warning_includes_default_volume_note(self, simple_config):
        # Well A1 is in initial_contents WITHOUT a volume → tracker assumes capacity
        spec = make_spec([self._dummy_step()], initial_contents=[
            WellContents(labware="source_plate", well="A1", substance="DNA"),  # no volume_ul
        ])
        tracker = WellStateTracker(simple_config, spec)

        # Snapshot warnings from __init__ (the assumed-volume warning)
        before = list(tracker.warnings)

        # Dispensing more pushes the well past assumed capacity
        tracker.dispense("source_plate", "A1", 100.0, "buffer")

        # Exactly one new warning should appear (the overflow), and it should
        # carry the assumed-volume NOTE because the well is in _default_volume_wells.
        new_warnings = tracker.warnings[len(before):]
        assert len(new_warnings) == 1
        overflow = new_warnings[0]
        assert "exceeding" in overflow.lower()
        assert "assumed" in overflow.lower() or "no volume" in overflow.lower()


# ============================================================================
# WellStateTracker.aspirate — contract-gap tests
# (Existing TestWellStateTracker covers basic empty-well-warns / sufficient-
#  well-no-warning / over-aspirate-warns. These fill the contract gaps:
#  return value, deduplication of warnings, state-unchanged-on-failure.)
# ============================================================================

class TestWellStateTrackerAspirate:
    """Contract-gap tests for WellStateTracker.aspirate."""

    def _dummy_step(self):
        return ExtractedStep(order=1, action="delay", duration=_dur(1, unit="seconds"), composition_provenance=_comp())

    # Post: returns True when sufficient
    def test_returns_true_when_sufficient(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        tracker.dispense("source_plate", "A1", 100.0, "buffer")

        assert tracker.aspirate("source_plate", "A1", 30.0) is True

    # Post: returns False when well is empty (no prior dispense)
    def test_returns_false_when_empty(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)

        assert tracker.aspirate("source_plate", "A1", 50.0) is False

    # Post: returns False when insufficient
    def test_returns_false_when_insufficient(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        tracker.dispense("source_plate", "A1", 30.0, "buffer")

        assert tracker.aspirate("source_plate", "A1", 100.0) is False

    # Post: state unchanged on False (volume not decremented on failure)
    def test_state_unchanged_on_failure(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        tracker.dispense("source_plate", "A1", 30.0, "buffer")

        tracker.aspirate("source_plate", "A1", 100.0)  # fails
        assert tracker.get_volume("source_plate", "A1") == 30.0  # unchanged

    # Post: state decremented exactly on True
    def test_state_decremented_exactly_on_success(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        tracker.dispense("source_plate", "A1", 100.0, "buffer")

        tracker.aspirate("source_plate", "A1", 30.0)
        assert tracker.get_volume("source_plate", "A1") == 70.0

    # Post: warnings are deduplicated per (labware, well)
    def test_warnings_deduplicated_per_well(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)

        # Three failed aspirates on the same empty well → only ONE warning
        tracker.aspirate("source_plate", "A1", 50.0)
        tracker.aspirate("source_plate", "A1", 25.0)
        tracker.aspirate("source_plate", "A1", 10.0)
        assert len(tracker.warnings) == 1

    # Post: different wells get separate warnings
    def test_warnings_not_deduplicated_across_wells(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)

        tracker.aspirate("source_plate", "A1", 50.0)
        tracker.aspirate("source_plate", "B1", 50.0)
        assert len(tracker.warnings) == 2


# ============================================================================
# WellStateTracker.get_volume / get_substances / well_has_liquid — contract tests
# ============================================================================

class TestWellStateTrackerInspectors:
    """Contract tests for the read-only inspector methods."""

    def _dummy_step(self):
        return ExtractedStep(order=1, action="delay", duration=_dur(1, unit="seconds"), composition_provenance=_comp())

    # get_volume — Post: returns tracked volume after dispense
    def test_get_volume_after_dispense(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        tracker.dispense("source_plate", "A1", 75.0, "buffer")
        assert tracker.get_volume("source_plate", "A1") == 75.0

    # get_volume — Post: returns 0.0 for unseen labware
    def test_get_volume_unseen_labware_returns_zero(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        assert tracker.get_volume("never_seen_labware", "A1") == 0.0

    # get_volume — Post: returns 0.0 for known labware but unseen well
    def test_get_volume_known_labware_unseen_well(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        tracker.dispense("source_plate", "A1", 50.0, "buffer")  # well A1 only
        assert tracker.get_volume("source_plate", "H12") == 0.0  # H12 unseen

    # get_substances — Post: returns substances after dispense
    def test_get_substances_after_dispense(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        tracker.dispense("source_plate", "A1", 50.0, "buffer")
        tracker.dispense("source_plate", "A1", 25.0, "DNA")
        assert tracker.get_substances("source_plate", "A1") == ["buffer", "DNA"]

    # get_substances — Post: returns [] for unseen well
    def test_get_substances_unseen_returns_empty(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        assert tracker.get_substances("source_plate", "A1") == []

    # get_substances — Post: returns a copy; mutating the result does NOT
    # affect the tracker's internal state. (Was previously a leaky reference;
    # fixed to return a shallow copy.)
    def test_get_substances_returns_independent_copy(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        tracker.dispense("source_plate", "A1", 50.0, "buffer")

        returned = tracker.get_substances("source_plate", "A1")
        returned.append("test_marker")  # mutate the returned list
        # Tracker's internal state must be unaffected.
        assert "test_marker" not in tracker.get_substances("source_plate", "A1")

    # well_has_liquid — Post: True after dispense
    def test_well_has_liquid_true_after_dispense(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        tracker.dispense("source_plate", "A1", 50.0, "buffer")
        assert tracker.well_has_liquid("source_plate", "A1") is True

    # well_has_liquid — Post: False for unseen well
    def test_well_has_liquid_false_for_unseen(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        assert tracker.well_has_liquid("source_plate", "A1") is False

    # well_has_liquid — Post: tolerance threshold (0.01)
    def test_well_has_liquid_tolerance_threshold(self, simple_config):
        spec = make_spec([self._dummy_step()], initial_contents=[])
        tracker = WellStateTracker(simple_config, spec)
        tracker.dispense("source_plate", "A1", 0.005, "trace")
        assert tracker.well_has_liquid("source_plate", "A1") is False  # below 0.01

        tracker.dispense("source_plate", "B1", 0.02, "trace")
        assert tracker.well_has_liquid("source_plate", "B1") is True   # above 0.01


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
        """Aspirating from a well with initial contents doesn't warn about emptiness."""
        spec = make_spec([self._dummy_step()], initial_contents=[
            WellContents(labware="source_plate", well="A1", substance="DNA")
        ])
        tracker = WellStateTracker(simple_config, spec)

        tracker.aspirate("source_plate", "A1", 100.0)
        empty_warnings = [w for w in tracker.warnings if "empty" in w]
        assert len(empty_warnings) == 0

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
        from nl2protocol.extraction import SemanticExtractor
        from nl2protocol.config import enrich_config_with_wells

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="transfer",
                    volume=_vol(100.0),
                    source=_loc(description="reservoir", well="A1"),
                    destination=_loc(description="plate", well="A1"),
                    composition_provenance=_comp(),
                )
            ],
            explicit_volumes=[100.0]
        )

        enriched = enrich_config_with_wells(serial_dilution_config)
        schema, _, _ = SemanticExtractor.spec_to_schema(spec, enriched)
        mismatches = SemanticExtractor.validate_schema_against_spec(spec, schema)

        assert len(mismatches) == 0

    def test_inferred_mix_volume_not_treated_as_hard_constraint(self, qpcr_config):
        """50uL mix volume (not in explicit_volumes) should NOT cause validation failure."""
        from nl2protocol.extraction import SemanticExtractor
        from nl2protocol.config import enrich_config_with_wells

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="transfer",
                    volume=_vol(10.0),
                    source=_loc(description="reagent_tube_rack", well="A1"),
                    destination=_loc(description="dilution_strip", well="A1"),
                    post_actions=[PostAction(action="mix", repetitions=5,
                                             volume=_vol(50.0, source="inferred"))],
                    composition_provenance=_comp(),
                )
            ],
            explicit_volumes=[10.0],  # 50.0 is NOT here — it was inferred
            initial_contents=[
                WellContents(labware="reagent_tube_rack", well="A1", substance="DNA")
            ]
        )

        enriched = enrich_config_with_wells(qpcr_config)
        schema, _, _ = SemanticExtractor.spec_to_schema(spec, enriched)
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
        from nl2protocol.extraction import SemanticExtractor
        from nl2protocol.config import enrich_config_with_wells

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="serial_dilution",
                    volume=_vol(100.0),
                    source=_loc(description="reservoir", well="A1"),
                    destination=_loc(description="plate", well_range="A1-A6"),
                    tip_strategy="new_tip_each",
                    composition_provenance=_comp(),
                )
            ],
            initial_contents=[
                WellContents(labware="reservoir", well="A1", substance="stock solution")
            ]
        )

        enriched = enrich_config_with_wells(serial_dilution_config)
        schema, _, _ = SemanticExtractor.spec_to_schema(spec, enriched)

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
        from nl2protocol.extraction import SemanticExtractor
        from nl2protocol.config import enrich_config_with_wells

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="serial_dilution",
                    volume=_vol(100.0),
                    source=_loc(description="reservoir", well="A1"),
                    destination=_loc(description="plate", well_range="A1-A6"),
                    tip_strategy="new_tip_each",
                    composition_provenance=_comp(),
                )
            ],
            initial_contents=[
                WellContents(labware="reservoir", well="A1", substance="stock")
            ]
        )

        enriched = enrich_config_with_wells(serial_dilution_config)
        schema, _, _ = SemanticExtractor.spec_to_schema(spec, enriched)

        distributes = [c for c in schema.commands if c.command_type == "distribute"]
        assert len(distributes) == 0, "Serial dilution should not auto-distribute diluent"


# ============================================================================
# PROVENANCE DISPLAY
# ============================================================================

class TestProvenance:
    """Tests that the confirmation formatter shows provenance correctly."""

    def test_inferred_volume_tagged(self):
        """Inferred volumes appear in full mode with [inferred] provenance."""
        from nl2protocol.extraction import SemanticExtractor

        spec = make_spec([
            ExtractedStep(
                order=1, action="transfer",
                volume=_vol(100.0, source="inferred"),
                source=_loc(description="plate_a", well="A1"),
                destination=_loc(description="plate_b", well="A1"),
                composition_provenance=_comp(),
            )
        ])

        output = SemanticExtractor.format_for_confirmation(spec, full=True)
        assert "inferred" in output

    def test_explicit_volume_no_tag(self):
        """Explicit volumes (not inferred, not approximate) get no tag."""
        from nl2protocol.extraction import SemanticExtractor

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="transfer",
                    volume=_vol(100.0),
                    source=_loc(description="plate_a", well="A1"),
                    destination=_loc(description="plate_b", well="A1"),
                    composition_provenance=_comp(),
                )
            ],
            explicit_volumes=[100.0]
        )

        output = SemanticExtractor.format_for_confirmation(spec)
        assert "100.0uL" in output
        assert "[INFERRED]" not in output.split("100.0uL")[0].split("\n")[-1]

    def test_post_action_volume_provenance_tagged(self):
        """Post-action volumes show their provenance source in full mode."""
        from nl2protocol.extraction import SemanticExtractor

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="transfer",
                    volume=_vol(10.0),
                    source=_loc(description="plate_a", well="A1"),
                    destination=_loc(description="plate_b", well="A1"),
                    post_actions=[PostAction(action="mix", repetitions=5,
                                             volume=_vol(50.0, source="domain_default", exact=False))],
                    composition_provenance=_comp(),
                )
            ],
            explicit_volumes=[10.0]
        )

        output = SemanticExtractor.format_for_confirmation(spec, full=True)
        assert "50.0uL" in output
        assert "domain_default" in output

    def test_post_action_rendered_once_in_threshold_mode(self):
        """Regression: _format_step_line and format_for_confirmation used
        to both render '-> mix Nx at VuL' independently, producing duplicate
        lines. _format_step_line is now the single source of truth."""
        from nl2protocol.extraction import SemanticExtractor

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="transfer",
                    volume=_vol(2.0),
                    source=_loc(description="src", well="A1"),
                    destination=_loc(description="dst", well="A1"),
                    post_actions=[PostAction(action="mix", repetitions=3,
                                             volume=_vol(10.0))],
                    composition_provenance=_comp(),
                )
            ],
            explicit_volumes=[2.0]
        )
        output = SemanticExtractor.format_for_confirmation(spec)
        # The post-action description must appear exactly once.
        assert output.count("-> mix") == 1
        assert "mix 3x at 10.0uL" in output

    def test_tip_strategy_surfaces_in_step_line(self):
        """Regression: _format_step_line did not surface tip_strategy,
        so the Stage 8 validator couldn't see it."""
        from nl2protocol.extraction import SemanticExtractor

        spec = make_spec(
            [
                ExtractedStep(
                    order=1, action="transfer",
                    volume=_vol(2.0),
                    source=_loc(description="src", well="A1"),
                    destination=_loc(description="dst", well="A1"),
                    tip_strategy="new_tip_each",
                    composition_provenance=_comp(),
                )
            ],
            explicit_volumes=[2.0]
        )
        output = SemanticExtractor.format_for_confirmation(spec)
        assert "new_tip_each" in output
