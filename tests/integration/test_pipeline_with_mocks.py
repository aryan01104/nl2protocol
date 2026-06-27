"""
Integration tests — extractor → ConstraintChecker → spec_to_schema → script,
with the Anthropic client mocked at the SemanticExtractor boundary.

These tests answer "does the pipeline shape work end-to-end on the
deterministic stages, given a known LLM output?" without burning API tokens
and without the interactive `_prompt_input` paths in `ProtocolAgent.run_pipeline`.

Scope:
  - Stage 2 (LLM extraction): MOCKED — canned `<reasoning>...</reasoning>
    <spec>{...}</spec>` responses per scenario.
  - Stage 4 (constraint check): real `ConstraintChecker.check_all`.
  - Stage 5a (spec → schema): real `schema_builder.spec_to_schema`.
  - Stage 5b (schema → script): real `generate_python_script`.
  - Stages 1, 3, 6, 7, the full ProtocolAgent.run_pipeline orchestrator,
    and any interactive `_prompt_input` flows are OUT OF SCOPE — those
    couple to UX, not pipeline shape.

Per the slides (lec15 #5 "check state, not process") assertions are on
result state — extracted spec, violations list, generated script
substrings — NOT on `mock.assert_called_with(...)`.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nl2protocol.extraction.extractor import SemanticExtractor
from nl2protocol.extraction.schema_builder import spec_to_schema
from nl2protocol.models.schema import (
    Labware, Module, Pipette, ProtocolSchema, Transfer,
)
from nl2protocol.pipeline import generate_python_script
from nl2protocol.validation.constraints import (
    PhysicalConstraintsChecker, Severity, ViolationType,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def simple_config():
    config_path = Path(__file__).parent.parent.parent / "test_cases" / "examples" / "simple_transfer" / "config.json"
    with open(config_path) as f:
        return json.load(f)


def _make_mock_response(spec_json: dict, reasoning: str = "test reasoning"):
    """Build a Mock object shaped like an Anthropic API response."""
    response = MagicMock()
    text = (
        f"<reasoning>\n{reasoning}\n</reasoning>\n"
        f"<spec>\n{json.dumps(spec_json)}\n</spec>"
    )
    response.content = [MagicMock(text=text)]
    response.stop_reason = "end_turn"
    return response


def _make_mock_stream(response):
    """A context manager mimicking client.messages.stream(...): yields an object
    exposing text_stream (an iterable of string chunks) and get_final_message()."""
    text = response.content[0].text
    # Split into a few pieces so the accumulation loop exercises real concatenation.
    mid = len(text) // 2
    chunks = [text[:mid], text[mid:]]

    stream_obj = MagicMock()
    stream_obj.text_stream = iter(chunks)
    stream_obj.get_final_message.return_value = response

    ctx = MagicMock()
    ctx.__enter__.return_value = stream_obj
    ctx.__exit__.return_value = False
    return ctx


def _make_mock_extractor(spec_json: dict, reasoning: str = "test reasoning"):
    """Construct a SemanticExtractor with a mocked Anthropic client that
    delivers the given spec_json through .messages.stream()."""
    mock_client = MagicMock()
    response = _make_mock_response(spec_json, reasoning)
    mock_client.messages.stream.return_value = _make_mock_stream(response)
    return SemanticExtractor(client=mock_client)


# ============================================================================
# Canned ProtocolSpec JSON — matches Pydantic shape, valid by construction.
# ============================================================================

def _spec_simple_transfer(volume_uL: float = 100.0):
    """A minimal one-step transfer that fits within p300 range and 96-well grid."""
    return {
        "summary": "transfer test",
        "steps": [
            {
                "order": 1,
                "action": "transfer",
                "composition_provenance": {
                    "step_cited_text": "transfer 100uL from A1 to B1",
                    "parameters_cited_texts": ["transfer 100uL from A1 to B1"],
                    "parameters_reasoning": "user phrase fully specifies the transfer parameters",
                    "grounding": ["instruction"],
                    "confidence": 1.0,
                },
                "volume": {
                    "value": volume_uL, "unit": "uL", "exact": True,
                    "provenance": {"source": "instruction", "cited_text": "100uL", "confidence": 1.0},
                },
                "source": {
                    "description": "source_plate", "well": "A1",
                    "resolved_label": "source_plate",
                    "description_provenance": {"source": "instruction", "cited_text": "source_plate", "confidence": 1.0},
                    "wells_provenance": {"source": "instruction", "cited_text": "from A1", "confidence": 1.0},
                },
                "destination": {
                    "description": "dest_plate", "well": "B1",
                    "resolved_label": "dest_plate",
                    "description_provenance": {"source": "instruction", "cited_text": "dest_plate", "confidence": 1.0},
                    "wells_provenance": {"source": "instruction", "cited_text": "to B1", "confidence": 1.0},
                },
            }
        ],
    }


def _spec_multi_step():
    """A 3-step protocol: transfer → delay → transfer."""
    base_step = lambda order, action, **extras: {
        "order": order, "action": action,
        "composition_provenance": {
            "step_cited_text": "test step phrase",
            "parameters_cited_texts": ["test step phrase"],
            "parameters_reasoning": "test reasoning ties cite to parameters",
            "grounding": ["instruction"],
            "confidence": 1.0,
        },
        **extras,
    }
    prov = {"source": "instruction", "cited_text": "test step phrase", "confidence": 1.0}
    transfer_extras = lambda src, dst: {
        "volume": {"value": 50.0, "unit": "uL", "exact": True, "provenance": prov},
        "source": {"description": "source_plate", "well": src, "resolved_label": "source_plate", "description_provenance": prov, "wells_provenance": prov},
        "destination": {"description": "dest_plate", "well": dst, "resolved_label": "dest_plate", "description_provenance": prov, "wells_provenance": prov},
    }
    return {
        "summary": "multi-step test",
        "steps": [
            base_step(1, "transfer", **transfer_extras("A1", "A1")),
            base_step(2, "delay", duration={"value": 5.0, "unit": "seconds", "provenance": prov}),
            base_step(3, "transfer", **transfer_extras("A2", "A2")),
        ],
    }


def _spec_with_well_outside_grid():
    """Spec that passes Pydantic well-name regex but exceeds the 8x12 grid."""
    spec = _spec_simple_transfer(volume_uL=100.0)
    # I1 matches [A-P]([1-9]|...) regex but is row I — outside an 8-row grid.
    spec["steps"][0]["source"]["well"] = "I1"
    return spec


# ============================================================================
# Integration tests — happy paths and one boundary case per scenario
# ============================================================================

class TestPipelineHappyPath:
    """Stage chain runs end-to-end on canned LLM output."""

    # Test 1: full happy chain — extract → constraints clean → schema → script
    def test_simple_transfer_round_trips_to_python_script(self, simple_config):
        extractor = _make_mock_extractor(_spec_simple_transfer(volume_uL=100.0))

        # Stage 2 — extract
        spec = extractor.extract("Transfer 100uL from A1 to B1")
        assert spec is not None
        assert len(spec.steps) == 1
        assert spec.steps[0].volume.value == 100.0

        # Stage 4 — constraints
        result = PhysicalConstraintsChecker(simple_config).assert_physical_constraints(spec)
        assert result.errors == []  # clean

        # Stage 5a — spec → schema
        schema, _well_warnings, _step_summaries = spec_to_schema(spec, simple_config)
        assert schema is not None
        # A single transfer collapses to ONE high-level Transfer command
        # (not aspirate + dispense atoms — the schema is the planning level).
        assert len(schema.commands) >= 1
        assert schema.commands[0].command_type == "transfer"

        # Stage 5b — script generation
        script = generate_python_script(schema)
        assert "def run(" in script
        # High-level Transfer renders as pipette.transfer(...)
        assert ".transfer(" in script


class TestPipelineConstraintFailure:
    """Constraint checker catches what the LLM can't know about hardware."""

    # Test 2: LLM-output volume exceeds the only configured pipette → flagged
    def test_oversized_volume_flagged_by_constraints(self, simple_config):
        # simple_config has only p300 (max 300uL). 500uL exceeds it.
        extractor = _make_mock_extractor(_spec_simple_transfer(volume_uL=500.0))

        spec = extractor.extract("Transfer 500uL from A1 to B1")
        assert spec is not None  # extraction itself succeeds — Pydantic doesn't know about pipettes

        result = PhysicalConstraintsChecker(simple_config).assert_physical_constraints(spec)
        capacity_violations = [
            v for v in result.violations
            if v.violation_type == ViolationType.PIPETTE_CAPACITY
        ]
        assert len(capacity_violations) >= 1
        assert capacity_violations[0].severity == Severity.ERROR

    # Test 5: well that passes Pydantic regex but exceeds labware grid
    def test_out_of_grid_well_flagged_by_constraints(self, simple_config):
        # Well "I1" matches [A-P] regex but corning 96-well only has rows A-H.
        extractor = _make_mock_extractor(_spec_with_well_outside_grid())

        spec = extractor.extract("Transfer 100uL from I1 to B1")
        assert spec is not None  # Pydantic accepts the well-name; constraints don't

        result = PhysicalConstraintsChecker(simple_config).assert_physical_constraints(spec)
        well_violations = [
            v for v in result.violations
            if v.violation_type == ViolationType.WELL_INVALID
        ]
        assert len(well_violations) >= 1


class TestPipelineMalformedLLMOutput:
    """Pipeline contains malformed LLM output — failure is fail-fast, not crash."""

    # Test 3: LLM returns unparseable garbage → extract returns None gracefully
    def test_garbage_llm_response_returns_none(self, simple_config):
        mock_client = MagicMock()
        garbage_response = MagicMock()
        garbage_response.content = [MagicMock(text="not valid xml or json at all")]
        garbage_response.stop_reason = "end_turn"
        mock_client.messages.create.return_value = garbage_response

        extractor = SemanticExtractor(client=mock_client)
        spec = extractor.extract("Transfer 100uL from A1 to B1")

        # Per extractor's docstring: "Returns ProtocolSpec on success, None on failure (fail-fast)."
        assert spec is None

    def test_extraction_retries_transient_timeout(self, simple_config):
        # A read timeout fired mid-stream is transient: extract() retries the
        # whole streamed call and recovers instead of failing the run.
        import httpx
        from unittest.mock import patch
        response = _make_mock_response(_spec_simple_transfer(volume_uL=100.0))
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = [
            httpx.ReadTimeout("stalled mid-stream"),
            _make_mock_stream(response),
        ]
        extractor = SemanticExtractor(client=mock_client)
        with patch("time.sleep"):   # skip the backoff
            spec = extractor.extract("Transfer 100uL from A1 to B1")
        assert spec is not None
        assert mock_client.messages.stream.call_count == 2
        # Retry telemetry the pipeline writes into the state log.
        assert extractor.extraction_attempts == 2
        assert len(extractor.extraction_retries) == 1
        assert extractor.extraction_retries[0]["error_type"] == "ReadTimeout"

    def test_extraction_does_not_retry_terminal_error(self, simple_config):
        # A max_tokens truncation is terminal, not transient — fail fast, no retry.
        from unittest.mock import patch
        response = _make_mock_response(_spec_simple_transfer(volume_uL=100.0))
        response.stop_reason = "max_tokens"
        mock_client = MagicMock()
        mock_client.messages.stream.return_value = _make_mock_stream(response)
        extractor = SemanticExtractor(client=mock_client)
        with patch("time.sleep"):
            spec = extractor.extract("Transfer 100uL from A1 to B1")
        assert spec is None
        assert mock_client.messages.stream.call_count == 1
        # Terminal error → one attempt, no transient retries recorded.
        assert extractor.extraction_attempts == 1
        assert extractor.extraction_retries == []


class TestPipelineMultiStep:
    """Multi-step specs propagate step count through the chain."""

    # Test 4: 3-step spec → 3 steps in spec → multiple commands in schema/script
    def test_three_step_spec_round_trips(self, simple_config):
        extractor = _make_mock_extractor(_spec_multi_step())

        spec = extractor.extract("Transfer, delay, transfer")
        assert spec is not None
        assert len(spec.steps) == 3

        result = PhysicalConstraintsChecker(simple_config).assert_physical_constraints(spec)
        assert result.errors == []

        schema, _, _ = spec_to_schema(spec, simple_config)
        assert schema is not None
        # 2 transfers + 1 delay = 3 high-level commands.
        assert len(schema.commands) == 3
        command_types = [c.command_type for c in schema.commands]
        assert command_types == ["transfer", "delay", "transfer"]

        script = generate_python_script(schema)
        assert script.count(".transfer(") == 2
        assert "delay" in script.lower()


class TestGeneratorModuleInference:
    """generate_python_script: labware sharing a module's slot loads onto the module.

    Regression for a magnetic-bead crash. The config placed `sample_plate` on the
    same deck slot as the magnetic module but never set `on_module`, so the
    generator emitted `protocol.load_labware(..., '4')` onto a slot the module
    already occupied. Opentrons rejected it with LocationIsOccupiedError. A deck
    slot holds exactly one object, so the generator now infers the module from
    the shared slot and loads the labware onto it.
    """

    def _magbead_schema(self):
        """A schema reproducing the bug: labware on slot 4 with on_module unset,
        while a magnetic module also occupies slot 4."""
        return ProtocolSchema(
            protocol_name="magbead_regression",
            author="test",
            modules=[Module(module_type="magnetic", slot="4", label="mag_mod")],
            labware=[
                Labware(slot="1", load_name="opentrons_96_tiprack_300ul", label="tiprack_300"),
                Labware(slot="2", load_name="opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
                        label="reagent_rack"),
                # on_module deliberately unset — this is the bug condition.
                Labware(slot="4", load_name="nest_96_wellplate_2ml_deep", label="sample_plate"),
            ],
            pipettes=[Pipette(mount="left", model="p300_single_gen2", tipracks=["tiprack_300"])],
            commands=[Transfer(pipette="left", source_labware="reagent_rack", source_well="A1",
                               dest_labware="sample_plate", dest_well="A1", volume=80.0)],
        )

    def test_labware_on_module_slot_loads_through_module(self):
        script = generate_python_script(self._magbead_schema())
        # The sample plate must load THROUGH the module, not onto the deck slot.
        assert "mod_4.load_labware('nest_96_wellplate_2ml_deep'" in script
        # And it must NOT try to claim the occupied deck slot directly.
        assert "protocol.load_labware('nest_96_wellplate_2ml_deep', '4'" not in script

    def test_generated_script_simulates_without_slot_collision(self):
        import io
        from opentrons import simulate

        script = generate_python_script(self._magbead_schema())
        # Raises ProtocolEngineExecuteError (LocationIsOccupiedError) before the fix.
        simulate.simulate(io.StringIO(script))
