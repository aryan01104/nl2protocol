"""
Round-trip and discrimination tests for the GapTarget bridge.

Phase 1 of the typed-target refactor (replaces stringly-typed
`Gap.field_path`). These tests pin the bridge's contract so detectors and
consumers can migrate independently in later phases without losing
fidelity to the string format.
"""

from __future__ import annotations

import pytest

from nl2protocol.gap_resolution.targets import (
    ConstraintPlaceholder,
    GapTarget,
    InitialVolume,
    InitialWell,
    NamespaceSplit,
    StepField,
    StepProvenance,
    StepSubfield,
    UnknownTarget,
    path_to_target,
    target_to_path,
)


# Every string in this list is a path the production codebase emits today
# (collected during Phase 0 inventory). The contract `target_to_path(
# path_to_target(s)) == s` must hold for every entry, including sentinels.
REAL_PATHS = [
    # StepField shapes (top-level step attributes)
    "steps[0].volume",
    "steps[3].substance",
    "steps[5].temperature",
    "steps[2].duration",
    "steps[0].source",
    "steps[0].destination",
    "steps[1].replicates",
    "steps[7].action",
    # StepSubfield shapes (LocationRef nested slots)
    "steps[0].source.well",
    "steps[0].source.wells",
    "steps[2].destination.wells",
    "steps[0].destination.well",
    "steps[0].source.resolved_label",
    "steps[3].destination.resolved_label",
    "steps[0].source.well_range",
    # StepProvenance shapes (must beat StepSubfield via match order)
    "steps[0].volume.provenance",
    "steps[5].substance.provenance",
    "steps[0].source.description_provenance",
    "steps[2].destination.description_provenance",
    "steps[0].destination.wells_provenance",
    "steps[3].source.wells_provenance",
    # InitialContents shapes
    "initial_contents[0].volume_ul",
    "initial_contents[5].volume_ul",
    "initial_contents[0].well",
    "initial_contents[12].well",
    # Sentinel — ConstraintViolationDetector placeholder
    "steps[0].constraint",
    "steps[7].constraint",
    # Sentinel — NamespaceSplitDetector synthetic key
    "labware.namespace_split:my_plate",
    "labware.namespace_split:plate with spaces",
    "labware.namespace_split:reagent_rack",
    # Test fixtures / unknown placeholders — must survive round-trip verbatim
    "unknown",
    "x",
    "a",
    "constraints.pipette_capacity",
    "garbage",
]


class TestRoundTrip:
    """Each production path string must survive `target → path → target`
    unchanged. This is the load-bearing property of the bridge: producers
    and consumers can migrate in either direction without the path losing
    its original meaning.
    """

    @pytest.mark.parametrize("path", REAL_PATHS)
    def test_path_to_target_to_path_is_identity(self, path: str) -> None:
        """target_to_path(path_to_target(s)) == s for every production path."""
        assert target_to_path(path_to_target(path)) == path

    def test_constructed_target_to_path_to_target_is_identity(self) -> None:
        """For each variant, constructing a target then round-tripping it
        through the string form yields a structurally equal target.
        """
        targets: list[GapTarget] = [
            StepField(step_idx=3, field="volume"),
            StepSubfield(step_idx=0, field="source", subfield="well"),
            StepProvenance(step_idx=2, field="volume", slot="provenance"),
            StepProvenance(step_idx=2, field="source", slot="description_provenance"),
            InitialVolume(well_idx=0),
            InitialWell(well_idx=4),
            ConstraintPlaceholder(step_idx=7),
            NamespaceSplit(description="reagent_rack"),
            UnknownTarget(raw="constraints.pipette_capacity"),
        ]
        for t in targets:
            assert path_to_target(target_to_path(t)) == t


class TestDiscrimination:
    """Each path shape must parse to its correct variant. Pins the match-
    order contract — particularly the precedence between StepProvenance
    and StepSubfield (description_provenance must NOT be classified as a
    subfield), and ConstraintPlaceholder vs StepField (the literal token
    "constraint" must NOT be classified as a field).
    """

    def test_top_level_field_parses_as_step_field(self) -> None:
        t = path_to_target("steps[3].volume")
        assert isinstance(t, StepField)
        assert t.step_idx == 3
        assert t.field == "volume"

    def test_nested_locationref_slot_parses_as_step_subfield(self) -> None:
        t = path_to_target("steps[0].source.well")
        assert isinstance(t, StepSubfield)
        assert t.step_idx == 0
        assert t.field == "source"
        assert t.subfield == "well"

    def test_atomic_provenance_slot_parses_as_step_provenance(self) -> None:
        """`steps[N].volume.provenance` is provenance, not a "provenance"
        subfield on a volume.
        """
        t = path_to_target("steps[2].volume.provenance")
        assert isinstance(t, StepProvenance)
        assert t.step_idx == 2
        assert t.field == "volume"
        assert t.slot == "provenance"

    def test_locationref_description_provenance_beats_subfield(self) -> None:
        """The bridge must classify `description_provenance` as a
        StepProvenance slot, NOT a StepSubfield named `description_provenance`.
        This is the match-order trap that motivated putting the prov regex
        first.
        """
        t = path_to_target("steps[0].source.description_provenance")
        assert isinstance(t, StepProvenance)
        assert t.field == "source"
        assert t.slot == "description_provenance"

    def test_locationref_wells_provenance_beats_subfield(self) -> None:
        t = path_to_target("steps[3].destination.wells_provenance")
        assert isinstance(t, StepProvenance)
        assert t.field == "destination"
        assert t.slot == "wells_provenance"

    def test_constraint_placeholder_beats_step_field(self) -> None:
        """`steps[N].constraint` must NOT be classified as a StepField with
        field="constraint" — that's exactly the misclassification that
        crashed the apply layer.
        """
        t = path_to_target("steps[7].constraint")
        assert isinstance(t, ConstraintPlaceholder)
        assert t.step_idx == 7

    def test_initial_volume_parses_correctly(self) -> None:
        t = path_to_target("initial_contents[5].volume_ul")
        assert isinstance(t, InitialVolume)
        assert t.well_idx == 5

    def test_initial_well_parses_correctly(self) -> None:
        t = path_to_target("initial_contents[12].well")
        assert isinstance(t, InitialWell)
        assert t.well_idx == 12

    def test_namespace_split_parses_with_description(self) -> None:
        t = path_to_target("labware.namespace_split:my_plate")
        assert isinstance(t, NamespaceSplit)
        assert t.description == "my_plate"

    def test_namespace_split_admits_spaces_in_description(self) -> None:
        t = path_to_target("labware.namespace_split:plate with spaces")
        assert isinstance(t, NamespaceSplit)
        assert t.description == "plate with spaces"

    def test_unrecognized_string_falls_through_to_unknown(self) -> None:
        t = path_to_target("garbage")
        assert isinstance(t, UnknownTarget)
        assert t.raw == "garbage"

    def test_unknown_preserves_synthetic_test_path(self) -> None:
        t = path_to_target("constraints.pipette_capacity")
        assert isinstance(t, UnknownTarget)
        assert t.raw == "constraints.pipette_capacity"


class TestGapConstruction:
    """Phase 6 — Gap and ReviewResult take typed targets directly. The
    Phase 2 string-bridge tests (auto-derive from field_path) are gone
    because field_path is no longer a constructor kwarg — it's a derived
    @property over the first target.
    """

    def _gap_kwargs(self, **overrides):
        from nl2protocol.gap_resolution.types import Gap  # noqa: F401
        base = dict(
            id="test-id",
            step_order=1,
            targets=[StepField(step_idx=3, field="volume")],
            kind="missing",
            current_value=None,
            description="test",
            severity="blocker",
        )
        base.update(overrides)
        return base

    def test_gap_field_path_derives_from_first_target(self) -> None:
        """`gap.field_path` is now a derived view — the string form of
        targets[0]. Callers that read field_path (JS template, event
        payloads) keep working without producer-side migration.
        """
        from nl2protocol.gap_resolution.types import Gap
        g = Gap(**self._gap_kwargs(
            targets=[StepField(step_idx=3, field="volume")],
        ))
        assert g.field_path == "steps[3].volume"

    def test_gap_with_multiple_targets_uses_first_for_field_path(self) -> None:
        """Deduped constraint-violation gaps carry N targets; field_path
        is the string form of the first (the historical 'representative'
        of the dedup group).
        """
        from nl2protocol.gap_resolution.types import Gap
        g = Gap(**self._gap_kwargs(
            targets=[
                StepField(step_idx=0, field="volume"),
                StepField(step_idx=1, field="volume"),
                StepField(step_idx=2, field="volume"),
            ],
            kind="constraint_violation",
        ))
        assert g.field_path == "steps[0].volume"
        assert len(g.targets) == 3

    def test_review_result_target_required(self) -> None:
        """`target` is now a required kwarg (no default). Construction
        without it raises TypeError at the dataclass level."""
        from nl2protocol.gap_resolution.types import ReviewResult
        with pytest.raises(TypeError):
            ReviewResult(  # type: ignore[call-arg]
                confirms_positive=True,
                confirms_negative=True,
                objection=None,
            )

    def test_review_result_field_path_derives_from_target(self) -> None:
        from nl2protocol.gap_resolution.types import ReviewResult
        r = ReviewResult(
            target=InitialVolume(well_idx=4),
            confirms_positive=True,
            confirms_negative=True,
            objection=None,
        )
        assert r.field_path == "initial_contents[4].volume_ul"

    def test_review_result_validation_still_fires(self) -> None:
        """Phase 6 didn't touch ReviewResult's objection invariant — a
        disconfirmed review with no objection text still raises."""
        from nl2protocol.gap_resolution.types import ReviewResult
        with pytest.raises(ValueError, match="objection"):
            ReviewResult(
                target=StepField(step_idx=0, field="volume"),
                confirms_positive=False,
                confirms_negative=True,
                objection=None,    # ← invalid: must be set when disconfirmed
            )
