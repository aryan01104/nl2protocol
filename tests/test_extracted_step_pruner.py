"""
Contract tests for `ExtractedStep.prune_irrelevant_fields_by_action` (ADR-0015).

The pruner runs as a `@model_validator(mode='after')` and nulls fields
the action doesn't consume, recording each scrub on
`ExtractedStep.pruned_fields`. The single source of truth for the
per-action keep-set is `_ACTION_KEEPS` in `nl2protocol.models.spec`.

These tests:
  1. Walk every action in `_ACTION_KEEPS` and assert the post-prune shape
     matches the keep-set (table-driven — one row per action).
  2. Assert pruned values land in `pruned_fields` with the right shape.
  3. Assert clean input is a no-op (no scrubs, empty `pruned_fields`).
  4. Assert kept fields survive untouched on every action.
"""

from __future__ import annotations

import pytest

from nl2protocol.models.spec import (
    CompositionProvenance, ExtractedStep, LocationRef, PostAction,
    Provenance, ProvenancedDuration, ProvenancedString,
    ProvenancedTemperature, ProvenancedVolume, _ACTION_KEEPS,
    _PRUNABLE_FIELDS,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _instr_prov() -> Provenance:
    return Provenance(source="instruction", cited_text="t", confidence=1.0)


def _comp() -> CompositionProvenance:
    return CompositionProvenance(
        step_cited_text="t", parameters_cited_texts=["t"],
        parameters_reasoning="t", grounding=["instruction"],
        confidence=1.0,
    )


def _loc(description: str = "tube rack") -> LocationRef:
    return LocationRef(
        description=description, well="A1",
        description_provenance=_instr_prov(),
        wells_provenance=_instr_prov(),
    )


def _vol(value: float = 50.0) -> ProvenancedVolume:
    return ProvenancedVolume(
        value=value, unit="uL", exact=True, provenance=_instr_prov(),
    )


def _sub(value: str = "reagent") -> ProvenancedString:
    return ProvenancedString(value=value, provenance=_instr_prov())


def _temp(value: float = 95.0) -> ProvenancedTemperature:
    return ProvenancedTemperature(value=value, provenance=_instr_prov())


def _dur(seconds: float = 60.0) -> ProvenancedDuration:
    return ProvenancedDuration(
        value=seconds, unit="seconds", provenance=_instr_prov(),
    )


def _build_max_step(action: str) -> ExtractedStep:
    """Construct an ExtractedStep with EVERY prunable field populated,
    regardless of whether the action consumes it. The pruner is then
    expected to keep only the fields in `_ACTION_KEEPS[action]`."""
    return ExtractedStep(
        order=1, action=action,
        composition_provenance=_comp(),
        substance=_sub(),
        volume=_vol(),
        temperature=_temp(),
        duration=_dur(),
        source=_loc("source rack"),
        destination=_loc("dest rack"),
        post_actions=[PostAction(action="mix", repetitions=2)],
        replicates=3,
        note="some note",
    )


# ---------------------------------------------------------------------------
# Table-driven matrix coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(_ACTION_KEEPS.keys()))
def test_post_prune_shape_matches_keep_set(action):
    """For every action in the matrix, an over-populated ExtractedStep
    is pruned down to exactly the keep-set. Fields outside the keep-set
    are nulled; fields inside are preserved."""
    step = _build_max_step(action)
    keep = _ACTION_KEEPS[action]
    for field_name in _PRUNABLE_FIELDS:
        value = getattr(step, field_name)
        if field_name in keep:
            assert value is not None, (
                f"action={action!r} should keep field {field_name!r}, "
                f"but it was nulled"
            )
        else:
            assert value is None, (
                f"action={action!r} should null field {field_name!r}, "
                f"but it kept value={value!r}"
            )


@pytest.mark.parametrize("action", sorted(_ACTION_KEEPS.keys()))
def test_pruned_values_recorded_in_pruned_fields(action):
    """Every scrub appends a PrunedFieldRecord. The set of recorded
    field names matches exactly the set of prunable fields outside the
    keep-set."""
    step = _build_max_step(action)
    keep = _ACTION_KEEPS[action]
    expected_scrubbed = _PRUNABLE_FIELDS - keep
    recorded = {r.field_name for r in step.pruned_fields}
    assert recorded == expected_scrubbed, (
        f"action={action!r}: expected scrubs {expected_scrubbed}, "
        f"got {recorded}"
    )


@pytest.mark.parametrize("action", sorted(_ACTION_KEEPS.keys()))
def test_pruned_field_record_preserves_value(action):
    """Each PrunedFieldRecord.value carries the original LLM-filled
    value (not the post-prune None). Sub-models are preserved as
    pydantic instances, primitives pass through."""
    step = _build_max_step(action)
    scrubbed = {r.field_name: r.value for r in step.pruned_fields}
    # `note` was the literal string "some note"; primitives pass through.
    if "note" in scrubbed:
        assert scrubbed["note"] == "some note"
    if "replicates" in scrubbed:
        assert scrubbed["replicates"] == 3
    # `destination` was a LocationRef; the record should hold the
    # original sub-model (not a dict, not None).
    if "destination" in scrubbed:
        assert isinstance(scrubbed["destination"], LocationRef)
        assert scrubbed["destination"].description == "dest rack"


# ---------------------------------------------------------------------------
# No-op + idempotency
# ---------------------------------------------------------------------------


def test_clean_input_produces_no_pruned_records():
    """An ExtractedStep that only fills fields the action consumes
    produces an empty `pruned_fields` list."""
    step = ExtractedStep(
        order=1, action="set_temperature",
        composition_provenance=_comp(),
        temperature=_temp(),
    )
    assert step.pruned_fields == []
    assert step.temperature.value == 95.0


def test_clean_transfer_keeps_full_payload():
    """A transfer step with the full pipetting payload is left
    untouched — the keep-set is large enough to cover everything we
    fill."""
    step = ExtractedStep(
        order=1, action="transfer",
        composition_provenance=_comp(),
        volume=_vol(), substance=_sub(),
        source=_loc("source rack"), destination=_loc("dest rack"),
        replicates=3,
    )
    assert step.pruned_fields == []
    assert step.volume.value == 50.0
    assert step.source.description == "source rack"
    assert step.destination.description == "dest rack"
    assert step.replicates == 3


def test_pruner_is_idempotent_after_revalidation():
    """Re-validating an already-pruned step is a no-op: every scrubbed
    field is already None so the non-null guard skips them. The
    `pruned_fields` list does NOT grow on re-validation (count stays
    the same; existing records pass through). We check by count
    because the round-trip serializes sub-model values to dicts (the
    `value: Any` typing on PrunedFieldRecord doesn't drive re-parse),
    so value-equality across the round trip is shape-dependent."""
    step = _build_max_step("set_temperature")
    original_count = len(step.pruned_fields)
    revalidated = ExtractedStep.model_validate(step.model_dump())
    assert len(revalidated.pruned_fields) == original_count, (
        f"re-validating an already-pruned step should not add new "
        f"scrub records. before={original_count}, "
        f"after={len(revalidated.pruned_fields)}"
    )
    # Field names also preserved — order-independent compare.
    assert {r.field_name for r in revalidated.pruned_fields} == (
        {r.field_name for r in step.pruned_fields}
    )


# ---------------------------------------------------------------------------
# Concrete scenarios pulled from the Western Blot smoke run
# ---------------------------------------------------------------------------


def test_set_temperature_with_destination_drops_destination():
    """Smoke-test scenario: LLM fills `destination` on a set_temperature
    step with the temperature module's name. The pruner nulls it and
    records the LocationRef on `pruned_fields`. This is the exact
    pattern that produced phantom modal rows in the 2026-06-02 run."""
    step = ExtractedStep(
        order=5, action="set_temperature",
        composition_provenance=_comp(),
        temperature=_temp(95.0),
        destination=LocationRef(
            description="temperature module",
            description_provenance=_instr_prov(),
        ),
    )
    assert step.destination is None
    assert step.temperature.value == 95.0
    assert len(step.pruned_fields) == 1
    assert step.pruned_fields[0].field_name == "destination"
    assert step.pruned_fields[0].value.description == "temperature module"


def test_comment_with_destination_drops_destination():
    """A `comment` step with the LLM hallucinating a destination gets
    cleaned — comments have no location."""
    step = ExtractedStep(
        order=1, action="comment",
        composition_provenance=_comp(),
        note="incubate at room temperature",
        destination=_loc("plate"),
    )
    assert step.destination is None
    assert step.note == "incubate at room temperature"
    assert any(r.field_name == "destination" for r in step.pruned_fields)


def test_delay_keeps_duration_and_note_only():
    """`delay` keeps duration + note. A delay step with stray volume /
    source / destination gets every irrelevant field scrubbed."""
    step = ExtractedStep(
        order=1, action="delay",
        composition_provenance=_comp(),
        duration=_dur(120.0),
        note="wait for binding",
        volume=_vol(),
        source=_loc(),
        destination=_loc(),
    )
    assert step.duration.value == 120.0
    assert step.note == "wait for binding"
    assert step.volume is None
    assert step.source is None
    assert step.destination is None
    scrubbed = {r.field_name for r in step.pruned_fields}
    assert {"volume", "source", "destination"} <= scrubbed
