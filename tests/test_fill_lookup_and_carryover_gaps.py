"""
Contract tests for `extractor.fill_lookup_and_carryover_gaps`.

Per ADR-0006, this function fills exactly two kinds of deterministic gaps:
  1. LOOKUP — substance → source location via config labware contents.
  2. CARRYOVER — wait_for_temperature inheriting from prior set_temperature.

Both kinds attach `Provenance(source="inferred", reasoning=..., confidence=...)`
so the report can surface what was filled and why. This file pins the
contract so the function can't silently drift.
"""

from __future__ import annotations

import pytest

from nl2protocol.extraction import SemanticExtractor
from nl2protocol.models.spec import (
    CompositionProvenance,
    ExtractedStep,
    LocationRef,
    Provenance,
    ProvenancedString,
    ProvenancedTemperature,
    ProtocolSpec,
)


# ============================================================================
# Test fixtures
# ============================================================================

def _comp() -> CompositionProvenance:
    """Minimal valid composition_provenance for any step."""
    return CompositionProvenance(
        step_cited_text="test step",
        parameters_cited_texts=["test step"],
        parameters_reasoning="test cohesion",
        grounding=["instruction"],
        confidence=1.0,
    )


def _instr_prov(text: str = "test cite") -> Provenance:
    return Provenance(source="instruction", cited_text=text, confidence=1.0)


def _spec(steps: list) -> ProtocolSpec:
    return ProtocolSpec(summary="test", steps=steps)


# ============================================================================
# CARRYOVER — wait_for_temperature inherits from prior set_temperature
# ============================================================================

class TestCarryoverGap:
    """wait_for_temperature with null temperature inherits from the most
    recent set_temperature step's value, with inferred provenance attached."""

    def test_carryover_fills_temperature_from_prior_set(self):
        spec = _spec([
            ExtractedStep(
                order=1, action="set_temperature",
                temperature=ProvenancedTemperature(value=4.0, provenance=_instr_prov("4°C")),
                composition_provenance=_comp(),
            ),
            ExtractedStep(
                order=2, action="wait_for_temperature",
                temperature=None,
                composition_provenance=_comp(),
            ),
        ])
        filled, fills = SemanticExtractor.fill_lookup_and_carryover_gaps(spec, config={})
        wait_step = filled.steps[1]
        assert wait_step.temperature is not None
        assert wait_step.temperature.value == 4.0
        assert wait_step.temperature.provenance.source == "inferred"
        # Reasoning names the prior step value so the ▴ tooltip can show it.
        assert "4.0" in wait_step.temperature.provenance.reasoning
        assert "set_temperature" in wait_step.temperature.provenance.reasoning
        # And a human-readable fill log is returned.
        assert any("wait_for_temperature" in f for f in fills)

    def test_carryover_uses_most_recent_set_temperature(self):
        # Two set_temperature steps; the wait should inherit the LATER one (42°C),
        # not the earlier (4°C). This matches the action's semantics: "wait
        # until module reaches its CURRENT target."
        spec = _spec([
            ExtractedStep(
                order=1, action="set_temperature",
                temperature=ProvenancedTemperature(value=4.0, provenance=_instr_prov("4°C")),
                composition_provenance=_comp(),
            ),
            ExtractedStep(
                order=2, action="set_temperature",
                temperature=ProvenancedTemperature(value=42.0, provenance=_instr_prov("42°C")),
                composition_provenance=_comp(),
            ),
            ExtractedStep(
                order=3, action="wait_for_temperature",
                temperature=None,
                composition_provenance=_comp(),
            ),
        ])
        filled, _ = SemanticExtractor.fill_lookup_and_carryover_gaps(spec, config={})
        assert filled.steps[2].temperature.value == 42.0

    def test_carryover_skips_when_no_prior_set_temperature(self):
        # A wait_for_temperature with no prior set_temperature has nothing
        # to inherit from. Leave it untouched — strict CompleteProtocolSpec
        # validation will catch it later with a clear error.
        spec = _spec([
            ExtractedStep(
                order=1, action="wait_for_temperature",
                temperature=None,
                composition_provenance=_comp(),
            ),
        ])
        filled, fills = SemanticExtractor.fill_lookup_and_carryover_gaps(spec, config={})
        assert filled.steps[0].temperature is None
        assert fills == []

    def test_carryover_does_not_overwrite_explicit_temperature(self):
        # If the LLM did populate temperature on wait_for_temperature, leave it.
        spec = _spec([
            ExtractedStep(
                order=1, action="set_temperature",
                temperature=ProvenancedTemperature(value=4.0, provenance=_instr_prov("4°C")),
                composition_provenance=_comp(),
            ),
            ExtractedStep(
                order=2, action="wait_for_temperature",
                temperature=ProvenancedTemperature(value=37.0, provenance=_instr_prov("37°C")),
                composition_provenance=_comp(),
            ),
        ])
        filled, _ = SemanticExtractor.fill_lookup_and_carryover_gaps(spec, config={})
        # Explicit value preserved — NOT overwritten by the carryover.
        assert filled.steps[1].temperature.value == 37.0
        assert filled.steps[1].temperature.provenance.source == "instruction"


# ============================================================================
# LOOKUP — substance → source location via config labware contents
# ============================================================================

class TestLookupGap:
    """A step with substance but no source gets its source filled from the
    config's labware contents, with inferred provenance attached."""

    @staticmethod
    def _config_with_substance(substance: str, labware_label: str = "reagent_rack",
                                well: str = "A1") -> dict:
        return {
            "labware": {
                labware_label: {
                    "load_name": "opentrons_24_tuberack",
                    "contents": {well: substance},
                },
            },
        }

    def test_lookup_fills_source_when_substance_matches_config(self):
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                substance=ProvenancedString(value="buffer", provenance=_instr_prov("buffer")),
                source=None,
                destination=LocationRef(description="dest_plate", well="A1",
                                        provenance=_instr_prov("dest_plate A1")),
                composition_provenance=_comp(),
            ),
        ])
        filled, fills = SemanticExtractor.fill_lookup_and_carryover_gaps(
            spec, config=self._config_with_substance("buffer")
        )
        src = filled.steps[0].source
        assert src is not None
        assert src.well == "A1"
        # Per ADR-0007: lookup fills MUST attach provenance — the historical
        # bug was leaving provenance=None so the renderer showed silent black text.
        assert src.provenance is not None
        assert src.provenance.source == "inferred"
        assert "buffer" in src.provenance.reasoning
        assert "reagent_rack" in src.provenance.reasoning
        assert any("buffer" in f for f in fills)

    def test_lookup_skips_when_no_config_match(self):
        # Substance not present in config — leave source null, no fill.
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                substance=ProvenancedString(value="rare_reagent", provenance=_instr_prov("rare_reagent")),
                source=None,
                destination=LocationRef(description="dest_plate", well="A1",
                                        provenance=_instr_prov("dest_plate A1")),
                composition_provenance=_comp(),
            ),
        ])
        filled, fills = SemanticExtractor.fill_lookup_and_carryover_gaps(
            spec, config=self._config_with_substance("buffer")
        )
        assert filled.steps[0].source is None
        assert fills == []

    def test_lookup_skips_when_source_already_set(self):
        # If the LLM populated source, lookup must not overwrite it.
        spec = _spec([
            ExtractedStep(
                order=1, action="transfer",
                substance=ProvenancedString(value="buffer", provenance=_instr_prov("buffer")),
                source=LocationRef(description="custom_rack", well="C5",
                                   provenance=_instr_prov("custom_rack C5")),
                destination=LocationRef(description="dest_plate", well="A1",
                                        provenance=_instr_prov("dest_plate A1")),
                composition_provenance=_comp(),
            ),
        ])
        filled, _ = SemanticExtractor.fill_lookup_and_carryover_gaps(
            spec, config=self._config_with_substance("buffer")
        )
        # Explicit source preserved.
        assert filled.steps[0].source.description == "custom_rack"
        assert filled.steps[0].source.well == "C5"


# ============================================================================
# INVARIANT — function does not mutate the input spec
# ============================================================================

class TestNoMutationOnInput:
    def test_input_spec_is_unchanged_after_call(self):
        # The function deep-copies; original spec must remain in its
        # pre-call state (otherwise pipeline state logs would diverge
        # from what stage 2 emitted).
        spec = _spec([
            ExtractedStep(
                order=1, action="set_temperature",
                temperature=ProvenancedTemperature(value=4.0, provenance=_instr_prov("4°C")),
                composition_provenance=_comp(),
            ),
            ExtractedStep(
                order=2, action="wait_for_temperature",
                temperature=None,
                composition_provenance=_comp(),
            ),
        ])
        original_dump = spec.model_dump()
        SemanticExtractor.fill_lookup_and_carryover_gaps(spec, config={})
        assert spec.model_dump() == original_dump
