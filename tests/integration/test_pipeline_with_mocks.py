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
  - Stage 5a (spec → schema): real `extractor.spec_to_schema`.
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
from nl2protocol.pipeline import generate_python_script
from nl2protocol.validation.constraints import (
    ConstraintChecker, Severity, ViolationType,
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


def _make_mock_extractor(spec_json: dict, reasoning: str = "test reasoning"):
    """Construct a SemanticExtractor with a mocked Anthropic client that
    returns the given spec_json on .messages.create()."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_mock_response(spec_json, reasoning)
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
                    "justification": "user said transfer",
                    "grounding": ["instruction"],
                    "confidence": 1.0,
                },
                "volume": {
                    "value": volume_uL, "unit": "uL", "exact": True,
                    "provenance": {"source": "instruction", "reason": "stated", "confidence": 1.0},
                },
                "source": {
                    "description": "source_plate", "well": "A1",
                    "resolved_label": "source_plate",
                    "provenance": {"source": "instruction", "reason": "stated", "confidence": 1.0},
                },
                "destination": {
                    "description": "dest_plate", "well": "B1",
                    "resolved_label": "dest_plate",
                    "provenance": {"source": "instruction", "reason": "stated", "confidence": 1.0},
                },
            }
        ],
    }


def _spec_multi_step():
    """A 3-step protocol: transfer → delay → transfer."""
    base_step = lambda order, action, **extras: {
        "order": order, "action": action,
        "composition_provenance": {
            "justification": "test step",
            "grounding": ["instruction"],
            "confidence": 1.0,
        },
        **extras,
    }
    prov = {"source": "instruction", "reason": "stated", "confidence": 1.0}
    transfer_extras = lambda src, dst: {
        "volume": {"value": 50.0, "unit": "uL", "exact": True, "provenance": prov},
        "source": {"description": "source_plate", "well": src, "resolved_label": "source_plate", "provenance": prov},
        "destination": {"description": "dest_plate", "well": dst, "resolved_label": "dest_plate", "provenance": prov},
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
        spec = extractor.extract("Transfer 100uL from A1 to B1", simple_config)
        assert spec is not None
        assert len(spec.steps) == 1
        assert spec.steps[0].volume.value == 100.0

        # Stage 4 — constraints
        result = ConstraintChecker(simple_config).check_all(spec)
        assert result.errors == []  # clean

        # Stage 5a — spec → schema
        schema, _well_warnings, _step_summaries = extractor.spec_to_schema(spec, simple_config)
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

        spec = extractor.extract("Transfer 500uL from A1 to B1", simple_config)
        assert spec is not None  # extraction itself succeeds — Pydantic doesn't know about pipettes

        result = ConstraintChecker(simple_config).check_all(spec)
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

        spec = extractor.extract("Transfer 100uL from I1 to B1", simple_config)
        assert spec is not None  # Pydantic accepts the well-name; constraints don't

        result = ConstraintChecker(simple_config).check_all(spec)
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
        spec = extractor.extract("Transfer 100uL from A1 to B1", simple_config)

        # Per extractor's docstring: "Returns ProtocolSpec on success, None on failure (fail-fast)."
        assert spec is None


class TestPipelineMultiStep:
    """Multi-step specs propagate step count through the chain."""

    # Test 4: 3-step spec → 3 steps in spec → multiple commands in schema/script
    def test_three_step_spec_round_trips(self, simple_config):
        extractor = _make_mock_extractor(_spec_multi_step())

        spec = extractor.extract("Transfer, delay, transfer", simple_config)
        assert spec is not None
        assert len(spec.steps) == 3

        result = ConstraintChecker(simple_config).check_all(spec)
        assert result.errors == []

        schema, _, _ = extractor.spec_to_schema(spec, simple_config)
        assert schema is not None
        # 2 transfers + 1 delay = 3 high-level commands.
        assert len(schema.commands) == 3
        command_types = [c.command_type for c in schema.commands]
        assert command_types == ["transfer", "delay", "transfer"]

        script = generate_python_script(schema)
        assert script.count(".transfer(") == 2
        assert "delay" in script.lower()
