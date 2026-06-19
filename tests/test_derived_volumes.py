"""
Tests for volumes derived from tracked well contents (VolumeBasis).

DETERMINISTIC — no LLM calls. A volume carrying a `basis` is resolved at build
time to `fraction × current well contents`, so physically-coupled volumes (add
vs. mix vs. remove) stay consistent: you can't mix or remove more than was added.
"""

import json
import pytest
from pathlib import Path

from nl2protocol.extraction import (
    CompleteProtocolSpec, ExtractedStep, ProvenancedVolume,
    CompositionProvenance, LocationRef, spec_to_schema,
)
from nl2protocol.models.spec import VolumeBasis, InstructionProvenance
from nl2protocol.models import Transfer, Mix
from nl2protocol.gap_resolution.suggesters import WellContentsVolumeSuggester
from nl2protocol.gap_resolution.types import Gap


@pytest.fixture
def config():
    p = Path(__file__).parent.parent / "test_cases" / "examples" / "simple_transfer" / "config.json"
    with open(p) as f:
        return json.load(f)


def _prov():
    return InstructionProvenance(source="instruction", cited_text="x", confidence=1.0)


def _comp():
    return CompositionProvenance(
        step_cited_text="x", parameters_cited_texts=["x"],
        parameters_reasoning="x", grounding=["instruction"], confidence=1.0,
    )


def _vol(value, basis=None):
    return ProvenancedVolume(value=value, unit="uL", exact=True, provenance=_prov(), basis=basis)


def _loc(**k):
    k.setdefault("description_provenance", _prov())
    if any(k.get(x) for x in ("well", "wells", "well_range")):
        k.setdefault("wells_provenance", _prov())
    return LocationRef(**k)


def _build(steps, config):
    spec = CompleteProtocolSpec(
        steps=steps, protocol_type="t", summary="t", reasoning="", initial_contents=[],
    )
    schema, _, _ = spec_to_schema(spec, config)
    return schema


# A 3-step coupling: add 30 to dest_plate A1, then a mix and a removal that both
# read that well. Used by several tests below.
def _coupled_steps(mix_basis, out_basis):
    return [
        ExtractedStep(  # add 30 uL into dest_plate A1
            order=1, action="transfer", volume=_vol(30),
            source=_loc(description="source_plate", well="A1", resolved_label="source_plate"),
            destination=_loc(description="dest_plate", well="A1", resolved_label="dest_plate"),
            composition_provenance=_comp()),
        ExtractedStep(  # mix the well — volume derived
            order=2, action="mix", volume=_vol(1.0, basis=mix_basis),
            destination=_loc(description="dest_plate", well="A1", resolved_label="dest_plate"),
            composition_provenance=_comp()),
        ExtractedStep(  # remove it back out — volume derived
            order=3, action="transfer", volume=_vol(1.0, basis=out_basis),
            source=_loc(description="dest_plate", well="A1", resolved_label="dest_plate"),
            destination=_loc(description="source_plate", well="A1", resolved_label="source_plate"),
            composition_provenance=_comp()),
    ]


class TestBuildTimeResolution:
    def test_mix_volume_is_fraction_of_well_contents(self, config):
        """basis(destination, 0.8) on a well holding 30 → Mix(volume=24)."""
        schema = _build(_coupled_steps(
            mix_basis=VolumeBasis(location="destination", fraction=0.8),
            out_basis=VolumeBasis(location="source", fraction=1.0),
        ), config)
        mix = next(c for c in schema.commands if isinstance(c, Mix))
        assert mix.volume == 24.0

    def test_removal_takes_all_of_the_well(self, config):
        """basis(source, 1.0) on a well holding 30 → transfer volume 30."""
        schema = _build(_coupled_steps(
            mix_basis=VolumeBasis(location="destination", fraction=0.8),
            out_basis=VolumeBasis(location="source", fraction=1.0),
        ), config)
        # The removal is the last transfer (out of dest_plate).
        removal = [c for c in schema.commands if isinstance(c, Transfer)][-1]
        assert removal.volume == 30.0

    def test_derived_volumes_never_exceed_what_was_added(self, config):
        """The whole point: no derived volume exceeds the 30 µL added."""
        schema = _build(_coupled_steps(
            mix_basis=VolumeBasis(location="destination", fraction=0.8),
            out_basis=VolumeBasis(location="source", fraction=1.0),
        ), config)
        for c in schema.commands:
            if isinstance(c, Mix):
                assert c.volume <= 30.0
            if isinstance(c, Transfer) and c.source_labware == "dest_plate":
                assert c.volume <= 30.0

    def test_literal_volume_without_basis_is_unchanged(self, config):
        """A plain literal volume (basis=None) passes through untouched."""
        schema = _build([
            ExtractedStep(order=1, action="transfer", volume=_vol(42),
                source=_loc(description="source_plate", well="A1", resolved_label="source_plate"),
                destination=_loc(description="dest_plate", well="A1", resolved_label="dest_plate"),
                composition_provenance=_comp()),
        ], config)
        t = next(c for c in schema.commands if isinstance(c, Transfer))
        assert t.volume == 42.0

    def test_basis_falls_back_to_literal_when_well_empty(self, config):
        """If the basis well was never filled, the literal value stands."""
        schema = _build([
            ExtractedStep(order=1, action="mix", volume=_vol(25.0,
                basis=VolumeBasis(location="destination", fraction=0.8)),
                destination=_loc(description="dest_plate", well="A1", resolved_label="dest_plate"),
                composition_provenance=_comp()),
        ], config)
        mix = next(c for c in schema.commands if isinstance(c, Mix))
        assert mix.volume == 25.0  # well empty → no derivation, placeholder stands


class TestWellContentsVolumeSuggester:
    def _gap(self, step_idx):
        return Gap(id=f"g{step_idx}", kind="missing",
                   field_path=f"steps[{step_idx}].volume",
                   step_order=step_idx + 1, description="missing volume",
                   current_value=None, severity="blocker")

    def _spec(self, steps):
        return CompleteProtocolSpec(steps=steps, protocol_type="t", summary="t",
                                    reasoning="", initial_contents=[])

    def test_fires_on_resuspend_mix(self):
        sug = WellContentsVolumeSuggester()
        step = ExtractedStep(order=1, action="mix", volume=_vol(1.0),
            destination=_loc(description="dest_plate", well="A1", resolved_label="dest_plate"),
            composition_provenance=_comp())
        out = sug.suggest(self._gap(0), self._spec([step]), {})
        assert out is not None
        assert out.value.basis.location == "destination"
        assert out.value.basis.fraction == 0.8

    def test_fires_on_supernatant_removal(self):
        sug = WellContentsVolumeSuggester()
        from nl2protocol.models.spec import ProvenancedString
        step = ExtractedStep(order=1, action="transfer", volume=_vol(1.0),
            substance=ProvenancedString(value="clear supernatant", provenance=_prov()),
            source=_loc(description="dest_plate", well="A1", resolved_label="dest_plate"),
            destination=_loc(description="source_plate", well="A1", resolved_label="source_plate"),
            composition_provenance=_comp())
        out = sug.suggest(self._gap(0), self._spec([step]), {})
        assert out is not None
        assert out.value.basis.location == "source"
        assert out.value.basis.fraction == 1.0

    def test_declines_fresh_reagent_addition(self):
        """An 'elution buffer' add is NOT a well reference — leave it to the LLM."""
        sug = WellContentsVolumeSuggester()
        from nl2protocol.models.spec import ProvenancedString
        step = ExtractedStep(order=1, action="transfer", volume=_vol(1.0),
            substance=ProvenancedString(value="elution buffer", provenance=_prov()),
            source=_loc(description="source_plate", well="A1", resolved_label="source_plate"),
            destination=_loc(description="dest_plate", well="A1", resolved_label="dest_plate"),
            composition_provenance=_comp())
        assert sug.suggest(self._gap(0), self._spec([step]), {}) is None

    def test_declines_non_volume_gap(self):
        sug = WellContentsVolumeSuggester()
        step = ExtractedStep(order=1, action="mix", volume=_vol(1.0),
            destination=_loc(description="dest_plate", well="A1", resolved_label="dest_plate"),
            composition_provenance=_comp())
        g = Gap(id="g", kind="missing", field_path="steps[0].destination",
                step_order=1, description="missing destination", current_value=None,
                severity="blocker")
        assert sug.suggest(g, self._spec([step]), {}) is None
