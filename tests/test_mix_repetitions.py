"""
Tests for standalone-mix cycle counts in spec_to_schema and the field pruner.

DETERMINISTIC — no LLM calls. A standalone `mix` action carries its cycle count
in `ExtractedStep.repetitions`. Regression guard: "mix 10 times" must emit
Mix(repetitions=10), not the silent default of 3.
"""

import json
import pytest
from pathlib import Path

from nl2protocol.constants import DEFAULT_MIX_REPS
from nl2protocol.extraction import (
    CompleteProtocolSpec, ExtractedStep, ProvenancedVolume,
    CompositionProvenance, LocationRef, spec_to_schema,
)
from nl2protocol.models.spec import InstructionProvenance
from nl2protocol.models import Mix


@pytest.fixture
def config():
    config_path = Path(__file__).parent.parent / "test_cases" / "examples" / "simple_transfer" / "config.json"
    with open(config_path) as f:
        return json.load(f)


def _prov():
    return InstructionProvenance(source="instruction", cited_text="x", confidence=1.0)


def _comp():
    return CompositionProvenance(
        step_cited_text="x", parameters_cited_texts=["x"],
        parameters_reasoning="x", grounding=["instruction"], confidence=1.0,
    )


def _vol(value):
    return ProvenancedVolume(value=value, unit="uL", exact=True, provenance=_prov())


def _loc(**kwargs):
    kwargs.setdefault("description_provenance", _prov())
    if any(kwargs.get(k) for k in ("well", "wells", "well_range")):
        kwargs.setdefault("wells_provenance", _prov())
    return LocationRef(**kwargs)


def _mix_step(repetitions):
    return ExtractedStep(
        order=1, action="mix", volume=_vol(100), repetitions=repetitions,
        destination=_loc(description="source_plate", well="A1",
                         resolved_label="source_plate"),
        composition_provenance=_comp(),
    )


def _mix_command(steps, config):
    spec = CompleteProtocolSpec(
        steps=steps, protocol_type="t", summary="t", reasoning="", initial_contents=[],
    )
    schema, _, _ = spec_to_schema(spec, config)
    return next(c for c in schema.commands if isinstance(c, Mix))


class TestStandaloneMixRepetitions:
    def test_stated_count_is_used(self, config):
        """"mix 10 times" → Mix(repetitions=10), not the default."""
        mix = _mix_command([_mix_step(repetitions=10)], config)
        assert mix.repetitions == 10
        assert mix.volume == 100

    def test_missing_count_is_surfaced_not_silently_defaulted(self, config):
        """No stated count → completeness rejects the spec so the count is
        surfaced as a gap (suggested DEFAULT_MIX_REPS, user-editable), instead
        of codegen silently filling it."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="missing mix cycle count"):
            _mix_command([_mix_step(repetitions=None)], config)


class TestMixCycleCountGapFlow:
    """A missing mix cycle count surfaces as a reviewable gap with a suggested
    default the user can accept or edit, instead of a silent codegen default."""

    def _gap(self, field_path=".repetitions", kind="missing"):
        from nl2protocol.gap_resolution.types import Gap
        return Gap(id="g", step_order=1, field_path=f"steps[0]{field_path}",
                   kind=kind, current_value=None,
                   description="missing mix cycle count", severity="blocker")

    def test_suggester_offers_default_below_auto_accept_threshold(self):
        from nl2protocol.gap_resolution import MixCycleCountSuggester
        from nl2protocol.gap_resolution.orchestrator import DEFAULT_AUTO_ACCEPT_THRESHOLD
        s = MixCycleCountSuggester().suggest(self._gap(), spec=None, context={})
        assert s is not None
        assert s.value == DEFAULT_MIX_REPS
        # Below the auto-accept threshold so the user always sees/edits it.
        assert s.confidence < DEFAULT_AUTO_ACCEPT_THRESHOLD

    def test_suggester_declines_unrelated_gaps(self):
        from nl2protocol.gap_resolution import MixCycleCountSuggester
        sug = MixCycleCountSuggester()
        assert sug.suggest(self._gap(field_path=".volume"), None, {}) is None
        assert sug.suggest(self._gap(kind="fabricated"), None, {}) is None

    def test_serial_dilution_missing_count_is_surfaced(self, config):
        """serial_dilution's per-transfer mix count is required too (was a
        hardcoded 3 in codegen)."""
        from pydantic import ValidationError
        sd = ExtractedStep(
            order=1, action="serial_dilution", volume=_vol(100), repetitions=None,
            source=_loc(description="plate", well="A1", resolved_label="plate"),
            destination=_loc(description="plate", well_range="A1-A12", resolved_label="plate"),
            composition_provenance=_comp(),
        )
        with pytest.raises(ValidationError, match="missing mix cycle count"):
            CompleteProtocolSpec(steps=[sd], protocol_type="t", summary="t",
                                 reasoning="", initial_contents=[])


class TestRepetitionsPruning:
    def test_repetitions_kept_on_mix_step(self):
        """A mix step retains its repetitions through the action-field pruner."""
        step = _mix_step(repetitions=10)
        assert step.repetitions == 10
        assert not any(p.field_name == "repetitions" for p in step.pruned_fields)

    def test_repetitions_pruned_on_transfer_step(self):
        """repetitions belongs on a mix action; on a transfer it is scrubbed
        (the count there lives on PostAction) and recorded in pruned_fields."""
        step = ExtractedStep(
            order=1, action="transfer", volume=_vol(80), repetitions=10,
            source=_loc(description="source_plate", well="A1",
                        resolved_label="source_plate"),
            destination=_loc(description="dest_plate", well="B1",
                             resolved_label="dest_plate"),
            composition_provenance=_comp(),
        )
        assert step.repetitions is None
        pruned = [p for p in step.pruned_fields if p.field_name == "repetitions"]
        assert len(pruned) == 1
        assert pruned[0].value == 10
