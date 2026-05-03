"""
Contract tests for the Pydantic model validators in nl2protocol.models.spec.

Each test class targets one validator. Each test method targets one clause
of the validator's docstring contract.
"""

import pytest
from pydantic import ValidationError

from nl2protocol.models.spec import (
    ExtractedStep, ProtocolSpec, CompleteProtocolSpec,
    Provenance, CompositionProvenance, ProvenancedVolume, ProvenancedString,
    LocationRef,
)


# ============================================================================
# HELPERS
# ============================================================================

def _prov(source="instruction", reason="test", confidence=1.0):
    return Provenance(source=source, reason=reason, confidence=confidence)


def _comp(grounding=None, justification="test step", confidence=1.0):
    return CompositionProvenance(
        justification=justification,
        grounding=grounding or ["instruction"],
        confidence=confidence,
    )


def _vol(value, unit="uL"):
    return ProvenancedVolume(value=value, unit=unit, exact=True, provenance=_prov())


def _pstr(value):
    return ProvenancedString(value=value, provenance=_prov())


def _step(order=1, action="delay", **kwargs):
    """Build an ExtractedStep with a default minimal-valid base."""
    base = {"order": order, "action": action, "composition_provenance": _comp()}
    base.update(kwargs)
    return ExtractedStep(**base)


def _spec(steps, **kwargs):
    return ProtocolSpec(summary="test spec", steps=steps, **kwargs)


# ============================================================================
# ExtractedStep.coerce_replicates — contract tests
# ============================================================================

class TestCoerceReplicates:
    """Contract tests for ExtractedStep.coerce_replicates."""

    # Post: replicates=None stays None
    def test_none_stays_none(self):
        step = _step(replicates=None)
        assert step.replicates is None

    # Post: replicates=2 stays 2
    def test_two_stays_two(self):
        step = _step(replicates=2)
        assert step.replicates == 2

    # Post: replicates=10 stays 10
    def test_large_value_stays_unchanged(self):
        step = _step(replicates=10)
        assert step.replicates == 10

    # Post: replicates=1 coerces to None
    def test_one_coerces_to_none(self):
        step = _step(replicates=1)
        assert step.replicates is None

    # Post: replicates=0 coerces to None (lenient normalization)
    def test_zero_coerces_to_none(self):
        step = _step(replicates=0)
        assert step.replicates is None

    # Post: replicates=-1 coerces to None (lenient normalization)
    def test_negative_coerces_to_none(self):
        step = _step(replicates=-3)
        assert step.replicates is None


# ============================================================================
# ProtocolSpec.validate_step_ordering — contract tests
# ============================================================================

class TestValidateStepOrdering:
    """Contract tests for ProtocolSpec.validate_step_ordering."""

    # Post: single step with order=1 is OK
    def test_single_step_with_order_1(self):
        _spec([_step(order=1)])  # no exception

    # Post: sequential 1..N is OK
    def test_sequential_orders_ok(self):
        _spec([_step(order=1), _step(order=2), _step(order=3)])

    # Post: out-of-order permutation is OK (only multiset matters)
    def test_permuted_orders_ok(self):
        _spec([_step(order=3), _step(order=1), _step(order=2)])

    # Post: gap raises ValueError
    def test_gap_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            _spec([_step(order=1), _step(order=3)])
        assert "consecutive 1..N" in str(exc_info.value)

    # Post: duplicate order raises ValueError
    def test_duplicate_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            _spec([_step(order=1), _step(order=1), _step(order=2)])
        assert "consecutive 1..N" in str(exc_info.value)

    # Post: starts at 0 — Pydantic's ge=1 on order rejects this BEFORE the
    # validator runs. Confirms the upstream constraint catches the case.
    def test_zero_based_rejected_by_field_constraint(self):
        with pytest.raises(ValidationError):
            _spec([_step(order=0), _step(order=1)])

    # Post: starts at 2 (no order=1) raises ValueError
    def test_starts_at_two_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            _spec([_step(order=2), _step(order=3), _step(order=4)])
        assert "consecutive 1..N" in str(exc_info.value)

    # Post: ValueError message embeds the offending order list
    def test_error_message_includes_order_list(self):
        with pytest.raises(ValidationError) as exc_info:
            _spec([_step(order=1), _step(order=3)])
        # Pydantic wraps the message; the actual orders list should appear
        assert "[1, 3]" in str(exc_info.value)


# ============================================================================
# CompleteProtocolSpec.validate_completeness — contract tests
# ============================================================================

class TestValidateCompleteness:
    """Contract tests for CompleteProtocolSpec.validate_completeness."""

    def _complete(self, steps, **kwargs):
        return CompleteProtocolSpec(summary="test", steps=steps, **kwargs)

    # Post: liquid action with volume is OK
    def test_liquid_action_with_volume_ok(self):
        self._complete([
            _step(order=1, action="mix", volume=_vol(50.0)),
        ])

    # Post: liquid action without volume raises with "missing volume"
    def test_liquid_action_without_volume_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            self._complete([_step(order=1, action="mix")])
        assert "missing volume" in str(exc_info.value)

    # Post: transfer action with source and destination is OK
    def test_transfer_with_source_and_destination_ok(self):
        self._complete([
            _step(
                order=1, action="transfer",
                volume=_vol(100.0),
                source=LocationRef(description="src", well="A1"),
                destination=LocationRef(description="dst", well="A1"),
            ),
        ])

    # Post: transfer action without source raises with "no source" or "missing source"
    def test_transfer_without_source_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            self._complete([
                _step(
                    order=1, action="transfer",
                    volume=_vol(100.0),
                    destination=LocationRef(description="dst", well="A1"),
                ),
            ])
        msg = str(exc_info.value)
        assert "source" in msg.lower()

    # Post: transfer action without destination raises with "missing destination"
    def test_transfer_without_destination_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            self._complete([
                _step(
                    order=1, action="transfer",
                    volume=_vol(100.0),
                    source=LocationRef(description="src", well="A1"),
                ),
            ])
        assert "missing destination" in str(exc_info.value)

    # Post: non-liquid, non-transfer action without volume/source/dest is OK
    def test_delay_step_without_volume_ok(self):
        from nl2protocol.models.spec import ProvenancedDuration
        self._complete([
            _step(
                order=1, action="delay",
                duration=ProvenancedDuration(value=30, unit="seconds", provenance=_prov()),
            ),
        ])

    # Post: multiple errors accumulate into ONE ValueError
    def test_multiple_errors_accumulate_in_one_raise(self):
        with pytest.raises(ValidationError) as exc_info:
            self._complete([
                _step(order=1, action="mix"),                 # missing volume
                _step(order=2, action="transfer", volume=_vol(50.0)),  # missing source AND dest
            ])
        msg = str(exc_info.value)
        # Both issues should appear in the same single raise
        assert "missing volume" in msg
        assert "source" in msg.lower()
        assert "missing destination" in msg
        # And the prefix names the count
        assert "Spec is incomplete" in msg

    # Post: substance hint appears in error message when substance is set
    def test_substance_hint_in_error_message(self):
        with pytest.raises(ValidationError) as exc_info:
            self._complete([
                _step(order=1, action="mix", substance=_pstr("buffer")),
            ])
        assert "for 'buffer'" in str(exc_info.value)
