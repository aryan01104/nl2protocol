"""
Contract tests for the `prior_revisions` field on Provenanced*, LocationRef,
and WellContents, plus the `push_revision` helper.

Covers the data-model side of the revision-history feature: pushing a
snapshot, the no-nested-history invariant, and order preservation. The
re-routing of write sites (apply path, stage 2.5 confirmations) is tested
separately once those sites are migrated.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nl2protocol.models.spec import InstructionProvenance, InferredProvenance, validate_provenance
from nl2protocol.models.spec import (
    LocationRef,
    InstructionProvenance,
    InferredProvenance,
    ProvenanceBase,
    ProvenancedDuration,
    ProvenancedString,
    ProvenancedTemperature,
    ProvenancedVolume,
    WellContents,
    push_revision,
)


def _instr(text: str) -> ProvenanceBase:
    return InstructionProvenance(source="instruction", cited_text=text, confidence=1.0)


def _inferred(reason: str, status: str = "original") -> ProvenanceBase:
    return InferredProvenance(
        source="inferred",
        positive_reasoning=reason,
        review_status=status,
        confidence=0.8,
    )


# ============================================================================
# Default state
# ============================================================================

class TestDefaultEmptyHistory:
    """Newly constructed fields carry empty `prior_revisions`. Existing test
    fixtures and call sites that don't pass `prior_revisions` keep working."""

    def test_provenanced_volume_default_is_empty(self):
        v = ProvenancedVolume(value=10.0, unit="uL", exact=True, provenance=_instr("10uL"))
        assert v.prior_revisions == []

    def test_provenanced_duration_default_is_empty(self):
        d = ProvenancedDuration(value=5.0, unit="minutes", provenance=_instr("5 minutes"))
        assert d.prior_revisions == []

    def test_provenanced_temperature_default_is_empty(self):
        t = ProvenancedTemperature(value=37.0, provenance=_instr("37C"))
        assert t.prior_revisions == []

    def test_provenanced_string_default_is_empty(self):
        s = ProvenancedString(value="buffer", provenance=_instr("buffer"))
        assert s.prior_revisions == []

    def test_location_ref_default_is_empty(self):
        lr = LocationRef(
            description="tube rack", well="A1",
            description_provenance=_instr("tube rack"),
            wells_provenance=_instr("A1"),
        )
        assert lr.prior_revisions == []

    def test_well_contents_default_is_empty(self):
        wc = WellContents(labware="tube rack", well="A1", substance="buffer")
        assert wc.prior_revisions == []


# ============================================================================
# push_revision: atomic Provenanced* types
# ============================================================================

class TestPushRevisionAtomic:
    """For atomic Provenanced* types, push_revision snapshots the prior
    state, appends to prior_revisions, then mutates the head fields."""

    def test_head_gets_updated_value_and_provenance(self):
        v = ProvenancedVolume(value=10.0, unit="uL", exact=True, provenance=_instr("10uL"))
        push_revision(v, value=15.0, provenance=_inferred("user changed", "user_edited"))
        assert v.value == 15.0
        assert v.provenance.review_status == "user_edited"

    def test_prior_revisions_carries_old_state(self):
        v = ProvenancedVolume(value=10.0, unit="uL", exact=True, provenance=_instr("10uL"))
        push_revision(v, value=15.0, provenance=_inferred("changed", "user_edited"))
        assert len(v.prior_revisions) == 1
        snapshot = v.prior_revisions[0]
        assert snapshot.value == 10.0
        assert snapshot.provenance.cited_text == ["10uL"]

    def test_snapshot_has_empty_prior_revisions(self):
        v = ProvenancedVolume(value=10.0, unit="uL", exact=True, provenance=_instr("10uL"))
        push_revision(v, value=15.0, provenance=_inferred("changed", "user_edited"))
        assert v.prior_revisions[0].prior_revisions == []

    def test_multiple_pushes_accumulate_oldest_first(self):
        v = ProvenancedVolume(value=10.0, unit="uL", exact=True, provenance=_instr("10uL"))
        push_revision(v, value=15.0, provenance=_inferred("first", "user_edited"))
        push_revision(v, value=20.0, provenance=_inferred("second", "user_edited"))
        push_revision(v, value=25.0, provenance=_inferred("third", "user_edited"))
        assert [r.value for r in v.prior_revisions] == [10.0, 15.0, 20.0]
        assert v.value == 25.0

    def test_snapshot_independent_of_head_mutation(self):
        # After push, mutating head fields must not bleed into the snapshot.
        v = ProvenancedVolume(value=10.0, unit="uL", exact=True, provenance=_instr("10uL"))
        push_revision(v, value=15.0, provenance=_inferred("changed", "user_edited"))
        v.value = 999.0  # head-only mutation
        assert v.prior_revisions[0].value == 10.0  # snapshot unchanged

    def test_empty_new_state_is_snapshot_only(self):
        # push_revision with no kwargs pushes a snapshot but leaves the
        # head unchanged — useful when we want to record "this state
        # was reviewed" without changing the value.
        v = ProvenancedVolume(value=10.0, unit="uL", exact=True, provenance=_instr("10uL"))
        push_revision(v)
        assert v.value == 10.0
        assert len(v.prior_revisions) == 1
        assert v.prior_revisions[0].value == 10.0


# ============================================================================
# push_revision: LocationRef (multi-field snapshot)
# ============================================================================

class TestPushRevisionLocationRef:
    """LocationRef has multiple sub-fields that can evolve together. A
    single push_revision snapshots the FULL LocationRef state — temporal
    coupling between sub-field changes is preserved."""

    def test_wells_change_snapshots_full_locationref(self):
        lr = LocationRef(
            description="tube rack", well="C7",
            description_provenance=_instr("tube C7"),
            wells_provenance=_instr("C7"),
        )
        push_revision(lr, well="D1", wells_provenance=_inferred("clipped", "user_accepted_suggestion"))
        snap = lr.prior_revisions[0]
        assert snap.well == "C7"
        assert snap.description == "tube rack"            # unchanged sub-field carried
        assert snap.wells_provenance.cited_text == ["C7"] # unchanged sub-field carried
        assert lr.well == "D1"
        assert lr.wells_provenance.review_status == "user_accepted_suggestion"

    def test_simultaneous_description_and_wells_change_is_one_revision(self):
        # If a single resolution updates BOTH description and well, the
        # chain captures ONE revision, not two. The renderer can diff
        # current vs prior to see both cells changed within the same
        # transition.
        lr = LocationRef(
            description="tube rack", well="C7",
            description_provenance=_instr("tube C7"),
            wells_provenance=_instr("C7"),
        )
        push_revision(
            lr,
            description="sample_rack", well="D1",
            description_provenance=_inferred("resolved by user", "user_edited"),
            wells_provenance=_inferred("clipped", "user_edited"),
        )
        assert len(lr.prior_revisions) == 1
        snap = lr.prior_revisions[0]
        assert (snap.description, snap.well) == ("tube rack", "C7")
        assert (lr.description, lr.well) == ("sample_rack", "D1")


# ============================================================================
# Validator: no nested history
# ============================================================================

class TestNoNestedHistoryValidator:
    """Entries in `prior_revisions` must themselves have empty
    `prior_revisions`. The head owns the chain; revisions are frozen
    points in time."""

    def test_nested_history_rejected_on_construction(self):
        # A snapshot that itself carries history is a structural error.
        nested_snap = ProvenancedVolume(
            value=5.0, unit="uL", exact=True, provenance=_instr("5uL"),
            prior_revisions=[
                ProvenancedVolume(value=3.0, unit="uL", exact=True, provenance=_instr("3uL")),
            ],
        )
        with pytest.raises(ValidationError, match="prior_revisions"):
            ProvenancedVolume(
                value=10.0, unit="uL", exact=True, provenance=_instr("10uL"),
                prior_revisions=[nested_snap],
            )

    def test_flat_history_accepted(self):
        # A snapshot with empty prior_revisions is fine.
        flat_snap = ProvenancedVolume(
            value=3.0, unit="uL", exact=True, provenance=_instr("3uL"),
        )
        head = ProvenancedVolume(
            value=10.0, unit="uL", exact=True, provenance=_instr("10uL"),
            prior_revisions=[flat_snap],
        )
        assert len(head.prior_revisions) == 1


# ============================================================================
# Integration: apply path populates prior_revisions
# ============================================================================

class TestApplyPathPushesRevisions:
    """End-to-end: applying a Resolution through default_apply_resolution
    leaves the corresponding head with one new entry in prior_revisions.
    Confirms Step C wired the writes through push_revision correctly."""

    def _build_minimal_spec(self):
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, ProtocolSpec,
        )
        return ProtocolSpec(
            summary="t",
            steps=[ExtractedStep(
                order=1, action="transfer",
                source=LocationRef(
                    description="tube rack", well="A1",
                    description_provenance=_instr("tube rack A1"),
                    wells_provenance=_instr("A1"),
                ),
                destination=LocationRef(
                    description="tube rack", well="C7",
                    description_provenance=_instr("tube C7"),
                    wells_provenance=_instr("C7"),
                ),
                volume=ProvenancedVolume(
                    value=10.0, unit="uL", exact=True,
                    provenance=_instr("10uL"),
                ),
                composition_provenance=CompositionProvenance(
                    step_cited_text="Transfer 10uL from A1 to C7",
                    parameters_cited_texts=["Transfer 10uL from A1 to C7"],
                    parameters_reasoning="all params cited in single phrase",
                    grounding=["instruction"],
                    confidence=1.0,
                ),
            )],
        )

    def test_subfield_apply_pushes_locationref_revision(self):
        # Path: steps[0].destination.wells — subfield write. Apply path
        # should snapshot the LocationRef BEFORE setting wells + stamping
        # provenance.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        from nl2protocol.gap_resolution.types import Gap, Resolution
        spec = self._build_minimal_spec()
        gap = Gap(
            id="test_gap",
            step_order=1,
            field_path="steps[0].destination.wells",
            kind="constraint_violation",
            severity="blocker",
            description="wells C7 out of range",
            current_value="C7",
        )
        resolution = Resolution(
            action="accept_suggestion",
            new_value=["D1"],
            user_action_provenance="user_accepted_suggestion",
        )
        default_apply_resolution(spec, gap, resolution, suggestion=None)
        dest = spec.steps[0].destination
        assert dest.wells == ["D1"]
        assert len(dest.prior_revisions) == 1
        snap = dest.prior_revisions[0]
        assert snap.well == "C7"
        assert snap.wells is None
        # The snapshot preserves the pre-write provenance shape.
        assert snap.wells_provenance.cited_text == ["C7"]
        # Head's wells_provenance was re-stamped with user action.
        assert dest.wells_provenance.review_status == "user_accepted_suggestion"

    def test_subfield_accept_suggestion_migrates_suggester_reasoning_to_head(self):
        # Phase 3c fix-2: when accept_suggestion changes a value subfield
        # (wells, well, description) on a LocationRef, the apply path
        # must REPLACE the corresponding provenance with one that carries
        # the suggester's reasoning + sets source=inferred. Otherwise the
        # head retains a stale instruction-cited provenance whose
        # cited_text no longer matches the actual value (the screenshot
        # bug: tooltip says cited "C7" while value is "A1").
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        from nl2protocol.gap_resolution.types import Gap, Resolution, Suggestion
        spec = self._build_minimal_spec()
        gap = Gap(
            id="test_gap",
            step_order=1,
            field_path="steps[0].destination.wells",
            kind="constraint_violation",
            severity="blocker",
            description="wells C7 out of range",
            current_value="C7",
        )
        suggestion = Suggestion(
            value=["A1"],
            provenance_source="deterministic",
            positive_reasoning=(
                "Wells [C7] exceed the labware's valid range (A1-D6). "
                "Clipping to in-range wells preserves intent."
            ),
            why_not_in_instruction=(
                "Instruction wells exceeded labware capacity; the "
                "instruction doesn't say which way to compromise."
            ),
            confidence=0.7,
        )
        resolution = Resolution(
            action="accept_suggestion",
            new_value=["A1"],
            user_action_provenance="user_accepted_suggestion",
        )
        default_apply_resolution(spec, gap, resolution, suggestion=suggestion)
        dest = spec.steps[0].destination
        # Head value updated.
        assert dest.wells == ["A1"]
        # Head's wells_provenance now reflects the suggester's reasoning
        # — source flipped from instruction to inferred, no stale cite.
        assert dest.wells_provenance.source == "inferred"
        assert not hasattr(dest.wells_provenance, "cited_text")
        assert "exceed the labware's valid range" in dest.wells_provenance.positive_reasoning
        assert dest.wells_provenance.review_status == "user_accepted_suggestion"
        # Prior chain preserves the original instruction citation —
        # nothing is lost; the audit trail moves.
        assert len(dest.prior_revisions) == 1
        prior_prov = dest.prior_revisions[0].wells_provenance
        assert prior_prov.source == "instruction"
        assert prior_prov.cited_text == ["C7"]

    def test_subfield_edit_writes_user_edited_inferred_provenance(self):
        # User-typed edits to a value subfield also replace the
        # provenance — the new value isn't lifted from the instruction,
        # so source flips to inferred with a "user edited" reasoning.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        from nl2protocol.gap_resolution.types import Gap, Resolution
        spec = self._build_minimal_spec()
        gap = Gap(
            id="test_gap",
            step_order=1,
            field_path="steps[0].destination.wells",
            kind="constraint_violation",
            severity="blocker",
            description="wells out of range",
            current_value="C7",
        )
        resolution = Resolution(
            action="edit",
            new_value=["D1"],
            user_action_provenance="user_edited",
        )
        default_apply_resolution(spec, gap, resolution, suggestion=None)
        dest = spec.steps[0].destination
        assert dest.wells == ["D1"]
        assert dest.wells_provenance.source == "inferred"
        assert not hasattr(dest.wells_provenance, "cited_text")
        assert dest.wells_provenance.review_status == "user_edited"
        assert "User edited" in dest.wells_provenance.positive_reasoning

    def test_fabrication_path_pushes_revision_on_provenance_replace(self):
        # Path: steps[0].volume.provenance — fabrication-shaped path.
        # Apply path should snapshot the ProvenancedVolume BEFORE replacing
        # the provenance slot. Underlying value is untouched.
        from nl2protocol.gap_resolution.orchestrator import default_apply_resolution
        from nl2protocol.gap_resolution.types import Gap, Resolution
        spec = self._build_minimal_spec()
        gap = Gap(
            id="test_gap",
            step_order=1,
            field_path="steps[0].volume.provenance",
            kind="fabricated",
            severity="blocker",
            description="cited_text not in instruction",
            current_value=10.0,
        )
        new_prov = InferredProvenance(
            source="inferred",
            positive_reasoning="resolver picked this",
            review_status="user_accepted_suggestion",
            confidence=0.9,
        )
        resolution = Resolution(
            action="accept_suggestion",
            new_value=new_prov,
            user_action_provenance="user_accepted_suggestion",
        )
        default_apply_resolution(spec, gap, resolution, suggestion=None)
        vol = spec.steps[0].volume
        assert vol.value == 10.0  # untouched
        assert vol.provenance.source == "inferred"
        assert vol.provenance.review_status == "user_accepted_suggestion"
        assert len(vol.prior_revisions) == 1
        snap = vol.prior_revisions[0]
        assert snap.provenance.source == "instruction"
        assert snap.provenance.cited_text == ["10uL"]


# ============================================================================
# Renderer: chains appear in HTML when prior_revisions is populated
# ============================================================================

class TestRendererChain:
    """The HTML renderer emits a value chain (prior segments joined by →
    arrows + head segment) when a tracked field has prior_revisions; falls
    back to a single span when the field has none."""

    def test_no_history_renders_single_span(self):
        # Field with empty prior_revisions: chain renderer must produce
        # output identical to the legacy single-span renderer.
        from nl2protocol.reporting import _render_revisioned_value
        v = ProvenancedVolume(value=10.0, unit="uL", exact=True,
                               provenance=_instr("10uL"))
        html = _render_revisioned_value(
            v,
            lambda f: f"{f.value} {f.unit}",
            lambda f: f.provenance,
            prov_id="vol-1", instruction="10uL",
        )
        assert "prior-rev" not in html
        assert "rev-arrow" not in html
        assert ">10.0 uL<" in html

    def test_one_prior_renders_two_segments_and_arrow(self):
        from nl2protocol.reporting import _render_revisioned_value
        v = ProvenancedVolume(value=10.0, unit="uL", exact=True,
                               provenance=_instr("10uL"))
        push_revision(v, value=15.0, provenance=_inferred("user edited", "user_edited"))
        html = _render_revisioned_value(
            v,
            lambda f: f"{f.value} {f.unit}",
            lambda f: f.provenance,
            prov_id="vol-1", instruction="10uL",
        )
        # Prior segment wrapped in .prior-rev with data-rev-idx=0
        assert 'class="prior-rev" data-rev-idx="0"' in html
        # Old value present in chain
        assert ">10.0 uL<" in html
        # New value present
        assert ">15.0 uL<" in html
        # Arrow separator
        assert 'class="rev-arrow"' in html

    def test_head_carries_prov_id_priors_do_not(self):
        # prov_id is the cite ↔ value pair-highlight anchor. Only the
        # head should participate; priors are frozen snapshots.
        from nl2protocol.reporting import _render_revisioned_value
        v = ProvenancedVolume(value=10.0, unit="uL", exact=True,
                               provenance=_instr("10uL"))
        push_revision(v, value=15.0, provenance=_inferred("changed", "user_edited"))
        html = _render_revisioned_value(
            v,
            lambda f: f"{f.value} {f.unit}",
            lambda f: f.provenance,
            prov_id="vol-1", instruction="10uL",
        )
        # Exactly one occurrence of data-prov-id (the head's)
        assert html.count('data-prov-id="vol-1"') == 1

    def test_locationref_wells_subfield_chain(self):
        # LocationRef.wells_provenance projection works through the chain.
        # Old: well='C7'. New: wells=['D1'].
        from nl2protocol.reporting import _render_revisioned_value, _format_wells_only
        lr = LocationRef(
            description="tube rack", well="C7",
            description_provenance=_instr("tube C7"),
            wells_provenance=_instr("C7"),
        )
        push_revision(
            lr, well=None, wells=["D1"],
            wells_provenance=_inferred("clipped", "user_accepted_suggestion"),
        )
        html = _render_revisioned_value(
            lr,
            _format_wells_only,
            lambda l: l.wells_provenance,
            prov_id="dst-wells", instruction="tube C7",
        )
        assert ">well C7<" in html
        assert ">wells D1<" in html
        assert "prior-rev" in html
        assert "rev-arrow" in html

    def test_consecutive_identical_priors_collapse(self):
        # Two pushes happened, but the projected text for THIS cell is
        # the same across both priors and only changes on the head.
        # Real-world example from the western blot run: stage 2.5's
        # labware-assignment push captures the LocationRef (wells
        # unchanged), then the gap loop pushes again with the actual
        # wells fix. Wells row was rendering ~~C7~~ → ~~C7~~ → A1 — the
        # middle segment is pure noise.
        from nl2protocol.reporting import _render_revisioned_value, _format_wells_only
        # Construct a LocationRef whose prior_revisions has two snapshots
        # with the SAME wells value, ending in a head with a different
        # wells value.
        lr = LocationRef(
            description="tube rack", well="A1",
            description_provenance=_instr("tube rack"),
            wells_provenance=_inferred("clipped to A1 after C7 invalid",
                                        "user_accepted_suggestion"),
            prior_revisions=[
                LocationRef(
                    description="tube rack", well="C7",
                    description_provenance=_instr("tube rack"),
                    wells_provenance=_instr("C7"),
                ),
                LocationRef(
                    description="tube rack", well="C7",
                    description_provenance=_instr("tube rack"),
                    resolved_label="sample_rack",
                    resolved_label_provenance=_inferred(
                        "user picked sample_rack", "user_accepted_suggestion"
                    ),
                    wells_provenance=_instr("C7"),
                ),
            ],
        )
        html = _render_revisioned_value(
            lr, _format_wells_only, lambda l: l.wells_provenance,
            prov_id="dst-wells", instruction="tube C7",
        )
        # Chain renders: well C7 → well A1 (the two C7 priors collapse).
        # We assert exactly ONE prior-rev wrapper, not two.
        assert html.count('class="prior-rev"') == 1
        assert ">well C7<" in html
        assert ">well A1<" in html

    def test_prior_with_identical_projection_is_filtered(self):
        # push_revision always snapshots the WHOLE tracked object. When a
        # caller pushes a LocationRef revision to write resolved_label
        # (stage 2.5 labware-assignment), the snapshot captures the
        # unchanged wells too. The chain renderer must filter that prior
        # out of the wells row — rendering a struck-through duplicate of
        # the same well list is visual noise.
        from nl2protocol.reporting import _render_revisioned_value, _format_wells_only
        lr = LocationRef(
            description="tube rack", wells=["A1", "A2", "A3"],
            description_provenance=_instr("tube rack"),
            wells_provenance=_instr("A1-A3"),
        )
        # Simulate the labware-assignment push: resolved_label flips, but
        # wells + wells_provenance stay the same.
        push_revision(
            lr,
            resolved_label="sample_rack",
            resolved_label_provenance=_inferred("user picked", "user_accepted_suggestion"),
        )
        # Wells row should render as a single span (chain suppressed
        # because the projection didn't change).
        html_wells = _render_revisioned_value(
            lr,
            _format_wells_only,
            lambda l: l.wells_provenance,
            prov_id="src-wells", instruction="A1-A3",
        )
        assert "prior-rev" not in html_wells
        assert "rev-arrow" not in html_wells
        # Description row should ALSO render as a single span — labware
        # description text picks up the new [sample_rack] suffix via
        # _format_labware_label, BUT only if the description sub-field
        # projection changed. For this revision the labware text DOES
        # change (resolved_label appears in the formatter output), so
        # the chain should appear on the description row.
        from nl2protocol.reporting import _format_labware_label
        html_label = _render_revisioned_value(
            lr,
            _format_labware_label,
            lambda l: l.description_provenance,
            prov_id="src-labware", instruction="tube rack",
        )
        # The labware row IS expected to render a chain because the
        # formatter output differs (resolved_label appears now), even
        # though description and description_provenance.source were
        # unchanged. We assert the chain renders by checking for the
        # head text containing "sample_rack".
        assert "sample_rack" in html_label

    def test_prior_with_same_value_text_is_filtered_even_if_source_differs(self):
        # When the value TEXT stays the same but the provenance source
        # changes (e.g., a fabrication resolution that keeps the value
        # but flips source to inferred), the prior is FILTERED — the
        # head's own styling (color class + dotted underline) already
        # communicates the source change. A struck-through duplicate
        # carrying identical text would be visual noise.
        from nl2protocol.reporting import _render_revisioned_value
        s = ProvenancedString(value="buffer", provenance=_instr("buffer"))
        push_revision(s, provenance=_inferred("re-stated as inferred", "user_overrode_fabrication"))
        html = _render_revisioned_value(
            s, lambda f: f.value, lambda f: f.provenance,
            prov_id="subst", instruction="buffer",
        )
        # No chain — text didn't change.
        assert "prior-rev" not in html
        assert "rev-arrow" not in html
        # Head still renders with the inferred styling.
        assert "prov-inferred" in html
        assert ">buffer<" in html

    def test_prior_with_none_subprov_is_skipped(self):
        # A prior revision whose projected sub-provenance is None is
        # skipped — the chain renders as if it weren't there. (Test:
        # synthesize a LocationRef whose prior revision has wells_provenance
        # None, with current head having wells_provenance set.)
        from nl2protocol.reporting import _render_revisioned_value, _format_wells_only
        # Construct a snapshot with wells_provenance=None manually.
        snap_no_wells = LocationRef(
            description="tube rack",
            description_provenance=_instr("tube rack"),
        )
        head = LocationRef(
            description="tube rack", well="A1",
            description_provenance=_instr("tube rack"),
            wells_provenance=_instr("A1"),
            prior_revisions=[snap_no_wells],
        )
        html = _render_revisioned_value(
            head,
            _format_wells_only,
            lambda l: l.wells_provenance,
            prov_id="src-wells", instruction="A1",
        )
        # No chain (skipped the only prior). Single span output.
        assert "prior-rev" not in html
        assert "rev-arrow" not in html
        assert ">well A1<" in html
