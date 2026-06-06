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

def _prov(source="instruction", text="test cited text", confidence=1.0):
    if source == "instruction":
        return Provenance(source=source, cited_text=text, confidence=confidence)
    return Provenance(source=source, reasoning=text, confidence=confidence)


def _comp(grounding=None, label="test step", confidence=1.0):
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
# CompositionProvenance.require_instruction_grounding — invariant tests
# ============================================================================

class TestCompositionProvenanceGroundingInvariant:
    """Architectural invariant: every step must trace back to user instruction.
    A CompositionProvenance whose grounding does not include 'instruction'
    is rejected at parse time — this prevents the LLM from silently injecting
    steps that have no instruction origin.
    """

    # Common kwargs to keep test signal focused on the grounding invariant.
    # Each test below overrides only `grounding` (or other) to exercise the
    # specific clause being tested.
    _DEFAULT_KWARGS = dict(
        step_cited_text="user said this",
        parameters_cited_texts=["user said this with these parameters"],
        parameters_reasoning="ties the cite to the parameter values",
        confidence=1.0,
    )

    # Post: grounding=['instruction'] alone is valid
    def test_instruction_alone_is_valid(self):
        cp = CompositionProvenance(**self._DEFAULT_KWARGS, grounding=["instruction"])
        assert cp.grounding == ["instruction"]

    # Post: grounding=['instruction', 'domain_default'] is valid (compound) — but
    # requires step_reasoning when domain_default is in grounding.
    def test_instruction_plus_domain_default_is_valid(self):
        cp = CompositionProvenance(
            **self._DEFAULT_KWARGS,
            grounding=["instruction", "domain_default"],
            step_reasoning="explains how the cited instruction expanded via domain knowledge",
        )
        assert "instruction" in cp.grounding
        assert "domain_default" in cp.grounding

    # Post: grounding=['domain_default'] alone is REJECTED — no instruction origin
    def test_domain_default_alone_is_rejected(self):
        with pytest.raises(ValidationError, match="must include 'instruction'"):
            CompositionProvenance(
                **self._DEFAULT_KWARGS,
                grounding=["domain_default"],
                step_reasoning="LLM added this from domain knowledge",
            )

    # Post: grounding=[] is rejected by the Literal/min_length constraint anyway
    def test_empty_grounding_is_rejected(self):
        with pytest.raises(ValidationError):
            CompositionProvenance(**self._DEFAULT_KWARGS, grounding=[])

    # Post: grounding=['config'] is rejected — 'config' is no longer a valid Literal
    def test_config_grounding_is_rejected(self):
        with pytest.raises(ValidationError):
            CompositionProvenance(**self._DEFAULT_KWARGS, grounding=["config"])


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
    # (uses action="transfer" because delay's keep-set excludes
    # replicates per ADR-0015; the test targets coerce_replicates, not
    # the pruner.)
    def test_two_stays_two(self):
        step = _step(action="transfer", replicates=2)
        assert step.replicates == 2

    # Post: replicates=10 stays 10
    def test_large_value_stays_unchanged(self):
        step = _step(action="transfer", replicates=10)
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
                source=LocationRef(description="src", well="A1", description_provenance=_prov(), wells_provenance=_prov()),
                destination=LocationRef(description="dst", well="A1", description_provenance=_prov(), wells_provenance=_prov()),
            ),
        ])

    # Post: transfer action without source raises with "no source" or "missing source"
    def test_transfer_without_source_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            self._complete([
                _step(
                    order=1, action="transfer",
                    volume=_vol(100.0),
                    destination=LocationRef(description="dst", well="A1", description_provenance=_prov(), wells_provenance=_prov()),
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
                    source=LocationRef(description="src", well="A1", description_provenance=_prov(), wells_provenance=_prov()),
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

    # Post: transfer action with source LocationRef that has description but
    # no well/wells/well_range raises with "missing source well(s)".
    # Closes the coverage hole where the constraint checker can't catch
    # absent wells and the spec passed completeness with a half-specified ref.
    def test_transfer_with_empty_source_locationref_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            self._complete([
                _step(
                    order=1, action="transfer",
                    volume=_vol(100.0),
                    source=LocationRef(description="reservoir", description_provenance=_prov()),
                    destination=LocationRef(description="dst", well="A1", description_provenance=_prov(), wells_provenance=_prov()),
                ),
            ])
        assert "missing source well(s)" in str(exc_info.value)

    # Post: same coverage for destination side.
    def test_transfer_with_empty_destination_locationref_raises(self):
        with pytest.raises(ValidationError) as exc_info:
            self._complete([
                _step(
                    order=1, action="transfer",
                    volume=_vol(100.0),
                    source=LocationRef(description="src", well="A1", description_provenance=_prov(), wells_provenance=_prov()),
                    destination=LocationRef(description="plate", description_provenance=_prov()),
                ),
            ])
        assert "missing destination well(s)" in str(exc_info.value)


# ============================================================================
# LocationRef provenance split — description_provenance required;
# wells_provenance required when any well/wells/well_range is set.
# ============================================================================

class TestLocationRefProvenanceRequired:
    """Every populated LocationRef must carry description_provenance, and
    (separately) wells_provenance whenever any well/wells/well_range is
    populated. The split closes the historic schema conflation where one
    provenance covered both the labware label and the well positions —
    semantically distinct claims that usually ground in different parts
    of the instruction."""

    def test_locationref_without_description_provenance_raises(self):
        # description_provenance is required regardless of whether wells set.
        with pytest.raises(ValidationError) as exc_info:
            LocationRef(description="tube rack", well="A1")
        msg = str(exc_info.value)
        assert "description_provenance" in msg.lower() or "provenance" in msg.lower()

    def test_wells_present_without_wells_provenance_raises(self):
        # wells_provenance is required when any of well/wells/well_range set.
        with pytest.raises(ValidationError) as exc_info:
            LocationRef(
                description="tube rack",
                well="A1",
                description_provenance=_prov(source="instruction", text="tube rack"),
            )
        msg = str(exc_info.value)
        assert "wells_provenance" in msg.lower()

    def test_labware_only_locationref_omits_wells_provenance(self):
        # Refs that name labware alone (e.g. a module by name with no well)
        # may leave wells_provenance null.
        ref = LocationRef(
            description="temperature module",
            description_provenance=_prov(source="instruction", text="temperature module"),
        )
        assert ref.description_provenance is not None
        assert ref.wells_provenance is None

    def test_locationref_with_both_provenances_constructs_cleanly(self):
        ref = LocationRef(
            description="tube rack",
            well="A1",
            description_provenance=_prov(source="instruction", text="tube rack"),
            wells_provenance=_prov(source="instruction", text="A1"),
        )
        assert ref.description_provenance.source == "instruction"
        assert ref.wells_provenance.source == "instruction"

    def test_inferred_provenance_acceptable_for_locationref(self):
        # The required-field rule doesn't constrain the SOURCE — inferred
        # provenance from gap-filling (e.g. ConfigLookupSuggester) is just
        # as valid as instruction-sourced provenance.
        ref = LocationRef(
            description="reagent_rack (inferred from config)",
            well="A1",
            description_provenance=_prov(source="inferred", text="config lookup matched substance 'buffer'"),
            wells_provenance=_prov(source="inferred", text="config lookup matched substance 'buffer'"),
        )
        assert ref.description_provenance.source == "inferred"
