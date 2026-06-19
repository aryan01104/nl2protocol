"""
Tests for discard-to-trash lowering in spec_to_schema.

DETERMINISTIC — no LLM calls, no API keys. They verify that a transfer whose
destination names waste/trash is lowered to "aspirate into the tip, drop the
tip in the OT-2 fixed trash" rather than a dispense into a configured container
(the bug where supernatant was dumped back into the ethanol reservoir).
"""

import json
import pytest
from pathlib import Path

from nl2protocol.constants import TRASH_LABEL, is_discard_description
from nl2protocol.extraction import (
    CompleteProtocolSpec, ExtractedStep, ProvenancedVolume,
    CompositionProvenance, LocationRef,
    spec_to_schema,
)
from nl2protocol.models.spec import InstructionProvenance
from nl2protocol.extraction.resolver import LabwareMatcher
from nl2protocol.models import Transfer, Aspirate, PickUpTip, DropTip


@pytest.fixture
def config():
    config_path = Path(__file__).parent.parent / "test_cases" / "examples" / "simple_transfer" / "config.json"
    with open(config_path) as f:
        return json.load(f)


def _prov():
    return InstructionProvenance(source="instruction", cited_text="test cited text", confidence=1.0)


def _loc(**kwargs):
    kwargs.setdefault("description_provenance", _prov())
    if any(kwargs.get(k) for k in ("well", "wells", "well_range")):
        kwargs.setdefault("wells_provenance", _prov())
    return LocationRef(**kwargs)


def _comp():
    return CompositionProvenance(
        step_cited_text="test step",
        parameters_cited_texts=["test step"],
        parameters_reasoning="test step",
        grounding=["instruction"],
        confidence=1.0,
    )


def _vol(value):
    return ProvenancedVolume(value=value, unit="uL", exact=True, provenance=_prov())


def _discard_step(resolved_label=TRASH_LABEL):
    return ExtractedStep(
        order=1, action="transfer", volume=_vol(180), substance=None,
        source=_loc(description="source_plate", well="A1",
                    resolved_label="source_plate"),
        destination=_loc(description="waste", resolved_label=resolved_label),
        composition_provenance=_comp(),
    )


def _schema(steps, config):
    spec = CompleteProtocolSpec(
        steps=steps, protocol_type="test", summary="t", reasoning="",
        initial_contents=[],
    )
    schema, _, _ = spec_to_schema(spec, config)
    return schema


class TestDiscardLowering:
    def test_discard_emits_aspirate_then_drop_to_trash(self, config):
        """A discard transfer lowers to PickUpTip → Aspirate → DropTip (trash)."""
        schema = _schema([_discard_step(resolved_label=TRASH_LABEL)], config)
        types = [c.command_type for c in schema.commands]
        assert types == ["pick_up_tip", "aspirate", "drop_tip"]

        aspirate = next(c for c in schema.commands if isinstance(c, Aspirate))
        assert aspirate.labware == "source_plate"
        assert aspirate.well == "A1"
        assert aspirate.volume == 180

        drop = next(c for c in schema.commands if isinstance(c, DropTip))
        assert drop.labware is None  # bare drop_tip() → OT-2 fixed trash

    def test_discard_never_dispenses_into_a_container(self, config):
        """No Transfer/Dispense into a configured well for a discard step."""
        schema = _schema([_discard_step(resolved_label=TRASH_LABEL)], config)
        assert not any(isinstance(c, Transfer) for c in schema.commands)
        assert not any(c.command_type == "dispense" for c in schema.commands)

    def test_discard_inferred_from_description_without_resolved_label(self, config):
        """Even if the assignments flow left resolved_label unset, a 'waste'
        destination still lowers to the trash (schema_builder fallback)."""
        schema = _schema([_discard_step(resolved_label=None)], config)
        types = [c.command_type for c in schema.commands]
        assert types == ["pick_up_tip", "aspirate", "drop_tip"]
        assert not any(isinstance(c, Transfer) for c in schema.commands)


class TestResolverRoutesDiscard:
    def test_resolver_routes_waste_to_trash(self, config):
        """The resolver maps a 'waste' destination to TRASH_LABEL, not a reagent."""
        matcher = LabwareMatcher(client=None, config=config)
        spec = CompleteProtocolSpec(
            steps=[_discard_step(resolved_label=None)],
            protocol_type="test", summary="t", reasoning="", initial_contents=[],
        )
        suggestions = matcher.suggest(spec)
        assert "waste" in suggestions
        assert suggestions["waste"].suggested_label == TRASH_LABEL
        assert suggestions["waste"].branch == "discard"


class TestIsDiscardDescription:
    @pytest.mark.parametrize("text", ["waste", "trash", "Discard", " liquid waste "])
    def test_positive(self, text):
        assert is_discard_description(text)

    @pytest.mark.parametrize("text", ["", None, "ethanol_reservoir", "sample_plate"])
    def test_negative(self, text):
        assert not is_discard_description(text)
