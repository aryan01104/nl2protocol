"""
Behavior tests for SemanticExtractor._validate_with_repair.

Every rule enforced when validating a ProtocolSpec at the extraction stage
governs the model's own output consistency — none can be caused by the
instruction — so each ValidationError is a repairable extraction slip. These
tests pin that contract: recoverable slips are re-asked and patched; repair is
bounded; and value gaps (volume/location/well) are NOT enforced here.
"""

import json
from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from nl2protocol.extraction.extractor import (
    SemanticExtractor, MAX_SPEC_REPAIR_ATTEMPTS,
)
from nl2protocol.models.spec import (
    ProtocolSpec, ExtractedStep, CompositionProvenance,
    Provenance, ProvenancedVolume,
)


def _stub_client(reply_text: str):
    """An Anthropic client whose messages.create returns one text block."""
    block = MagicMock()
    block.type = "text"
    block.text = reply_text
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock()
    client.messages.create.return_value = resp
    return client


def _domain_step_data() -> dict:
    """A valid one-step ProtocolSpec dict whose step is a domain expansion."""
    spec = ProtocolSpec(
        summary="test",
        steps=[ExtractedStep(
            order=1,
            action="delay",
            composition_provenance=CompositionProvenance(
                step_cited_text="do a Bradford assay",
                step_reasoning="The standard Bradford workflow includes a 5-min incubation.",
                parameters_cited_texts=["do a Bradford assay"],
                parameters_reasoning="canonical Bradford timing",
                grounding=["instruction", "domain_default"],
                confidence=0.8,
            ),
        )],
    )
    return spec.model_dump()


# Post: a domain_default step missing step_reasoning is repaired by one re-ask
# and the resulting spec validates.
def test_repair_fills_missing_step_reasoning():
    good = _domain_step_data()
    broken = deepcopy(good)
    broken["steps"][0]["composition_provenance"]["step_reasoning"] = None

    client = _stub_client(json.dumps(good["steps"][0]))
    spec = SemanticExtractor(client=client)._validate_with_repair(
        broken, "do a Bradford assay")

    assert isinstance(spec, ProtocolSpec)
    assert spec.steps[0].composition_provenance.step_reasoning
    assert client.messages.create.call_count == 1


# Post: a grounding rule failure (grounding omits 'instruction') is also
# repaired — repair is general, not step_reasoning-specific. The instruction
# cannot produce this tag, so it is an extraction slip.
def test_repair_fixes_grounding_missing_instruction():
    good = _domain_step_data()
    broken = deepcopy(good)
    broken["steps"][0]["composition_provenance"]["grounding"] = ["domain_default"]

    client = _stub_client(json.dumps(good["steps"][0]))
    spec = SemanticExtractor(client=client)._validate_with_repair(
        broken, "do a Bradford assay")

    assert "instruction" in spec.steps[0].composition_provenance.grounding
    assert client.messages.create.call_count == 1


# Post: when the re-ask keeps returning a still-invalid entry, repair is
# bounded to MAX_SPEC_REPAIR_ATTEMPTS rounds and then the ValidationError
# surfaces.
def test_repair_gives_up_after_max_attempts():
    good = _domain_step_data()
    still_broken = deepcopy(good["steps"][0])
    still_broken["composition_provenance"]["step_reasoning"] = None
    broken = deepcopy(good)
    broken["steps"][0] = deepcopy(still_broken)

    client = _stub_client(json.dumps(still_broken))
    with pytest.raises(ValidationError):
        SemanticExtractor(client=client)._validate_with_repair(broken, "instr")

    assert client.messages.create.call_count == MAX_SPEC_REPAIR_ATTEMPTS


# Post: value gaps are not enforced at the extraction stage — a mix step with
# no volume validates as a ProtocolSpec with no repair call (those gaps belong
# to CompleteProtocolSpec, after gap resolution).
def test_value_gaps_are_not_repaired_here():
    spec = ProtocolSpec(
        summary="test",
        steps=[ExtractedStep(
            order=1,
            action="mix",
            composition_provenance=CompositionProvenance(
                step_cited_text="mix the well",
                parameters_cited_texts=["mix the well"],
                parameters_reasoning="user stated",
                grounding=["instruction"],
                confidence=1.0,
            ),
        )],
    )
    client = MagicMock()
    result = SemanticExtractor(client=client)._validate_with_repair(
        spec.model_dump(), "mix the well")

    assert isinstance(result, ProtocolSpec)
    client.messages.create.assert_not_called()
