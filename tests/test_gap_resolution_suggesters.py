"""
Contract tests for the deterministic suggesters (ADR-0008 PR2).

Each suggester has its own class. Tests pin the inputs that should produce
a Suggestion (positive) and the inputs that should produce None (the
suggester's domain-of-applicability boundary).

LLMSpotSuggester + IndependentReviewSuggester get their own test files
since they need canned-LLM-response infrastructure.
"""

from __future__ import annotations

import pytest

from nl2protocol.gap_resolution import Gap
from nl2protocol.gap_resolution.suggesters import (
    CarryoverSuggester,
    ConfigLookupSuggester,
    RegexFromNoteSuggester,
    WellCapacitySuggester,
    WellRangeClipSuggester,
)
from nl2protocol.models.spec import (
    CompositionProvenance,
    ExtractedStep,
    LocationRef,
    Provenance,
    ProtocolSpec,
    ProvenancedDuration,
    ProvenancedString,
    ProvenancedTemperature,
    ProvenancedVolume,
    WellContents,
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


def _gap(field_path: str, kind: str = "missing", step_order: int = 1,
         description: str = "", current_value=None) -> Gap:
    return Gap(
        id=f"step{step_order}.{field_path.split('.')[-1]}",
        step_order=step_order,
        field_path=field_path,
        kind=kind,
        current_value=current_value,
        description=description,
        severity="blocker",
    )


# ============================================================================
# ConfigLookupSuggester
# ============================================================================

class TestConfigLookupSuggester:
    """Substance → source location via config.labware.contents."""

    def test_finds_substance_in_config_labware(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(
                order=1, action="transfer",
                substance=ProvenancedString(value="buffer", provenance=_instr_prov("buffer")),
                source=None,
                destination=LocationRef(description="dest", well="A1",
                                        provenance=_instr_prov("A1")),
                composition_provenance=_comp(),
            ),
        ])
        config = {"labware": {"reagent_rack": {"contents": {"B3": "buffer"}}}}
        gap = _gap("steps[0].source", description="missing source for buffer")
        s = ConfigLookupSuggester().suggest(spec=spec, gap=gap,
                                             context={"config": config})
        assert s is not None
        assert s.value.well == "B3"
        assert "reagent_rack" in s.value.description
        assert s.provenance_source == "deterministic"
        assert "buffer" in s.positive_reasoning
        assert s.confidence == 0.9

    def test_returns_none_when_substance_not_in_config(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(
                order=1, action="transfer",
                substance=ProvenancedString(value="rare_reagent",
                                            provenance=_instr_prov("rare_reagent")),
                source=None,
                destination=LocationRef(description="dest", well="A1",
                                        provenance=_instr_prov("A1")),
                composition_provenance=_comp(),
            ),
        ])
        config = {"labware": {"reagent_rack": {"contents": {"B3": "buffer"}}}}
        gap = _gap("steps[0].source")
        assert ConfigLookupSuggester().suggest(spec=spec, gap=gap,
                                                context={"config": config}) is None

    def test_returns_none_for_non_source_gap(self):
        # ConfigLookup only handles `.source` gaps.
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="comment", note="x",
                          composition_provenance=_comp())
        ])
        gap = _gap("steps[0].volume")
        assert ConfigLookupSuggester().suggest(spec=spec, gap=gap,
                                                context={"config": {}}) is None

    def test_returns_none_when_step_has_no_substance(self):
        # If the step doesn't name a substance, lookup has no key.
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(
                order=1, action="transfer",
                volume=ProvenancedVolume(value=10.0, unit="uL", exact=True,
                                         provenance=_instr_prov()),
                source=None,
                destination=LocationRef(description="dest", well="A1",
                                        provenance=_instr_prov("A1")),
                composition_provenance=_comp(),
            ),
        ])
        gap = _gap("steps[0].source")
        assert ConfigLookupSuggester().suggest(
            spec=spec, gap=gap,
            context={"config": {"labware": {"r": {"contents": {"A1": "buffer"}}}}}
        ) is None


# ============================================================================
# CarryoverSuggester
# ============================================================================

class TestCarryoverSuggester:
    """wait_for_temperature inherits from prior set_temperature."""

    def test_inherits_from_immediately_prior_set_temperature(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="set_temperature",
                          temperature=ProvenancedTemperature(
                              value=4.0, provenance=_instr_prov("4°C")),
                          composition_provenance=_comp()),
            ExtractedStep(order=2, action="wait_for_temperature",
                          temperature=None,
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[1].temperature", step_order=2)
        s = CarryoverSuggester().suggest(spec=spec, gap=gap, context={})
        assert s is not None
        assert s.value.value == 4.0
        assert s.confidence == 0.95
        assert "4.0" in s.positive_reasoning

    def test_picks_most_recent_set_temperature(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="set_temperature",
                          temperature=ProvenancedTemperature(
                              value=4.0, provenance=_instr_prov("4°C")),
                          composition_provenance=_comp()),
            ExtractedStep(order=2, action="set_temperature",
                          temperature=ProvenancedTemperature(
                              value=42.0, provenance=_instr_prov("42°C")),
                          composition_provenance=_comp()),
            ExtractedStep(order=3, action="wait_for_temperature",
                          temperature=None,
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[2].temperature", step_order=3)
        s = CarryoverSuggester().suggest(spec=spec, gap=gap, context={})
        assert s is not None
        assert s.value.value == 42.0

    def test_returns_none_when_no_prior_set_temperature(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="wait_for_temperature",
                          temperature=None,
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[0].temperature")
        assert CarryoverSuggester().suggest(spec=spec, gap=gap, context={}) is None

    def test_returns_none_for_non_wait_step(self):
        # Carryover only applies to wait_for_temperature gaps.
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="set_temperature",
                          temperature=ProvenancedTemperature(
                              value=4.0, provenance=_instr_prov("4°C")),
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[0].temperature")
        assert CarryoverSuggester().suggest(spec=spec, gap=gap, context={}) is None


# ============================================================================
# WellCapacitySuggester
# ============================================================================

class TestWellCapacitySuggester:
    """Initial-volume defaults from labware well capacity."""

    def test_proposes_capacity_for_eppendorf(self):
        spec = ProtocolSpec(summary="t",
                            steps=[ExtractedStep(order=1, action="comment",
                                                 note="x",
                                                 composition_provenance=_comp())],
                            initial_contents=[
                                WellContents(labware="sample_rack", well="A1",
                                             substance="buffer", volume_ul=None)
                            ])
        config = {"labware": {"sample_rack": {
            "load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
        }}}
        gap = Gap(id="ic0.volume_ul", step_order=None,
                  field_path="initial_contents[0].volume_ul",
                  kind="missing", current_value=None,
                  description="initial volume null", severity="suggestion")
        s = WellCapacitySuggester().suggest(spec=spec, gap=gap,
                                             context={"config": config})
        assert s is not None
        assert s.value == 1500.0
        assert s.confidence == 0.7
        assert "sample_rack" in s.positive_reasoning

    def test_proposes_capacity_for_96_wellplate(self):
        spec = ProtocolSpec(summary="t",
                            steps=[ExtractedStep(order=1, action="comment",
                                                 note="x",
                                                 composition_provenance=_comp())],
                            initial_contents=[
                                WellContents(labware="assay_plate", well="A1",
                                             substance="media", volume_ul=None)
                            ])
        config = {"labware": {"assay_plate": {
            "load_name": "corning_96_wellplate_360ul_flat",
        }}}
        gap = Gap(id="ic0.volume_ul", step_order=None,
                  field_path="initial_contents[0].volume_ul",
                  kind="missing", current_value=None,
                  description="x", severity="suggestion")
        s = WellCapacitySuggester().suggest(spec=spec, gap=gap,
                                             context={"config": config})
        assert s is not None
        assert s.value == 300.0

    def test_returns_none_for_non_initial_contents_gap(self):
        spec = ProtocolSpec(summary="t",
                            steps=[ExtractedStep(order=1, action="comment",
                                                 note="x",
                                                 composition_provenance=_comp())])
        gap = _gap("steps[0].volume")  # different path shape
        assert WellCapacitySuggester().suggest(spec=spec, gap=gap,
                                                context={"config": {}}) is None


# ============================================================================
# RegexFromNoteSuggester
# ============================================================================

class TestRegexFromNoteSuggester:
    """Extract duration from pause step notes."""

    def test_parses_minutes_from_note(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="pause",
                          note="incubate 5 minutes at room temp",
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[0].duration", description="missing duration")
        s = RegexFromNoteSuggester().suggest(spec=spec, gap=gap, context={})
        assert s is not None
        assert s.value.value == 5
        assert s.value.unit == "minutes"

    def test_parses_seconds_from_note(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="pause",
                          note="hold for 45 s briefly",
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[0].duration")
        s = RegexFromNoteSuggester().suggest(spec=spec, gap=gap, context={})
        assert s is not None
        assert s.value.value == 45
        assert s.value.unit == "seconds"

    def test_returns_none_when_note_has_no_duration(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="pause",
                          note="user transfers tubes manually",
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[0].duration")
        assert RegexFromNoteSuggester().suggest(spec=spec, gap=gap, context={}) is None

    def test_returns_none_for_non_pause_step(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="comment", note="wait 5 minutes",
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[0].duration")
        assert RegexFromNoteSuggester().suggest(spec=spec, gap=gap, context={}) is None


# ============================================================================
# WellRangeClipSuggester
# ============================================================================

class TestWellRangeClipSuggester:
    """Constraint violation: wells exceed labware range → propose clipping."""

    def test_proposes_clipped_wells_from_constraint_message(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="comment", note="x",
                          composition_provenance=_comp())
        ])
        gap = Gap(
            id="step1.constraint",
            step_order=1,
            field_path="steps[0].destination.wells",
            kind="constraint_violation",
            current_value=["A7", "A8"],
            description=(
                "Step 1: Wells ['A7', 'A8'] do not exist on 'sample_rack' "
                "(opentrons_24_tuberack). Valid well range: A1-D6."
            ),
            severity="blocker",
        )
        s = WellRangeClipSuggester().suggest(spec=spec, gap=gap, context={})
        assert s is not None
        assert "A1" in s.value
        assert "D6" in s.value
        # 24-position rack: A1-A6, B1-B6, C1-C6, D1-D6 = 24 wells
        assert len(s.value) == 24
        assert s.confidence == 0.7

    def test_returns_none_for_non_constraint_gap(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="comment", note="x",
                          composition_provenance=_comp())
        ])
        gap = _gap("steps[0].volume")
        assert WellRangeClipSuggester().suggest(spec=spec, gap=gap, context={}) is None

    def test_returns_none_when_description_doesnt_match(self):
        # Description doesn't have the expected pattern; can't propose a fix.
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="comment", note="x",
                          composition_provenance=_comp())
        ])
        gap = Gap(id="x", step_order=1, field_path="x",
                  kind="constraint_violation", current_value=None,
                  description="some other constraint problem",
                  severity="blocker")
        assert WellRangeClipSuggester().suggest(spec=spec, gap=gap, context={}) is None
