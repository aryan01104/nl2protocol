"""
Integration tests for `ProtocolAgent._confirm_labware_assignments_via_handler`.

These tests exercise the user-visible path: spec + labware suggestions go
in, an `assignments_handler.confirm(table)` call goes out. They assert on
the structured table the modal would render — specifically that the
shape-mismatch warning surfaces for every candidate the user could pick,
not just the resolver's initial `suggested_label`.

Motivation (2026-06-02 smoke-test regression): the first Fix A landed a
warning that only fired when `suggested_label` was non-None. The
Western-Blot config has three racks sharing one load_name, so the
resolver returned `suggested_label=None` and the user picked sample_rack
manually — no warning ever shown. These tests cover the override case
plus the regression.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import List

from nl2protocol.extraction.resolver import LabwareMatchSuggestion
from nl2protocol.models.spec import (
    CompositionProvenance, ExtractedStep, LocationRef, Provenance,
    ProtocolSpec,
)
from nl2protocol.pipeline import ProtocolAgent


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _instr_prov() -> Provenance:
    return Provenance(source="instruction", cited_text="t", confidence=1.0)


def _comp_prov() -> CompositionProvenance:
    return CompositionProvenance(
        step_cited_text="t", parameters_cited_texts=["t"],
        parameters_reasoning="t", grounding=["instruction"],
        confidence=1.0,
    )


def _wb_config() -> dict:
    """Mirrors the user's Western-Blot config: three 24-position racks
    share the same load_name (sample_rack, reagent_rack, dest_block) plus
    a tip rack."""
    return {
        "labware": {
            "tiprack_20": {"load_name": "opentrons_96_tiprack_20ul"},
            "sample_rack": {
                "load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
            },
            "reagent_rack": {
                "load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
            },
            "dest_block": {
                "load_name": "opentrons_24_aluminumblock_nest_1.5ml_snapcap",
            },
        },
    }


class _CapturingHandler:
    """Stub assignments_handler that captures the table the modal would
    render and returns a no-op confirmed dict so the helper completes."""

    def __init__(self):
        self.captured_table: List[dict] = []

    def confirm(self, table):
        self.captured_table = list(table)
        # Return an empty dict — the helper's caller treats None as
        # abort, dict as "user submitted." We don't exercise downstream
        # apply here.
        return {row["description"]: row["suggested_label"] for row in table}


def _make_stub_agent(config: dict, handler) -> SimpleNamespace:
    """Build the smallest stand-in object that
    `_confirm_labware_assignments_via_handler` can run on. Binds the
    real shape-warning helpers from ProtocolAgent (via MethodType so
    `self` resolves to the stub) and stubs out the heavy constructor
    dependencies."""
    agent = SimpleNamespace(
        config_loader=SimpleNamespace(config=config),
        assignments_handler=handler,
    )
    agent._shape_mismatch_facts = MethodType(
        ProtocolAgent._shape_mismatch_facts, agent,
    )
    agent._shape_mismatch_warnings = MethodType(
        ProtocolAgent._shape_mismatch_warnings, agent,
    )
    return agent


def _spec_with_wells(wells_per_step_dst: List[List[str]]) -> ProtocolSpec:
    """ProtocolSpec where each step has source.description='tube rack'
    + destination.description='tube rack', destination.wells set from
    the given list."""
    steps = []
    for i, wells in enumerate(wells_per_step_dst, start=1):
        steps.append(ExtractedStep(
            order=i, action="transfer",
            source=LocationRef(
                description="tube rack", well="A1",
                description_provenance=_instr_prov(),
                wells_provenance=_instr_prov(),
            ),
            destination=LocationRef(
                description="tube rack", wells=wells,
                description_provenance=_instr_prov(),
                wells_provenance=_instr_prov(),
            ),
            composition_provenance=_comp_prov(),
        ))
    return ProtocolSpec(summary="t", steps=steps)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_warning_fires_when_resolver_has_no_suggestion():
    """REGRESSION (smoke test 2026-06-02): three candidates share a
    load_name, resolver returns suggested_label=None, user would pick
    manually. The warning MUST surface anyway — naming every candidate
    that can't host the offending wells."""
    config = _wb_config()
    handler = _CapturingHandler()
    agent = _make_stub_agent(config, handler)
    # Step with C7 — outside the 24-position rack's A-D × 1-6 grid.
    spec = _spec_with_wells([["C1", "C2", "C7"]])
    suggestions = {
        "tube rack": LabwareMatchSuggestion(
            description="tube rack",
            suggested_label=None,        # Resolver punted (ambiguous).
            positive_reasoning=None,
            why_not_in_instruction=None,
            confidence=0.0,
            candidates=["sample_rack", "reagent_rack", "dest_block"],
            branch="unresolvable",
        ),
    }

    ProtocolAgent._confirm_labware_assignments_via_handler(
        agent, spec, suggestions,
    )

    assert handler.captured_table, "handler should have been called"
    row = handler.captured_table[0]
    reasoning = row["positive_reasoning"] or ""
    assert "⚠ Shape mismatch:" in reasoning, reasoning
    assert "'C7'" in reasoning
    assert "sample_rack" in reasoning
    assert "reagent_rack" in reasoning
    assert "dest_block" in reasoning


def test_warning_dedupes_candidates_with_same_shape():
    """When N candidates share a shape (same load_name → same
    valid_wells), they show up in a single warning line naming all N —
    not N separate lines with the same well list."""
    config = _wb_config()
    handler = _CapturingHandler()
    agent = _make_stub_agent(config, handler)
    spec = _spec_with_wells([["C7", "C8"]])
    suggestions = {
        "tube rack": LabwareMatchSuggestion(
            description="tube rack",
            suggested_label=None,
            positive_reasoning=None,
            why_not_in_instruction=None,
            confidence=0.0,
            candidates=["sample_rack", "reagent_rack", "dest_block"],
            branch="unresolvable",
        ),
    }

    ProtocolAgent._confirm_labware_assignments_via_handler(
        agent, spec, suggestions,
    )

    reasoning = handler.captured_table[0]["positive_reasoning"] or ""
    # One "Shape mismatch" line, not three.
    assert reasoning.count("⚠ Shape mismatch:") == 1
    # All three candidates named in that one line.
    line = next(
        ln for ln in reasoning.splitlines()
        if ln.startswith("⚠ Shape mismatch:")
    )
    assert "sample_rack" in line and "reagent_rack" in line and "dest_block" in line


def test_no_warning_when_all_candidates_fit():
    """When every candidate's valid_wells covers every well the row
    touches, no shape warning is emitted."""
    config = _wb_config()
    handler = _CapturingHandler()
    agent = _make_stub_agent(config, handler)
    # All wells fit the 24-position rack (rows A-D, cols 1-6).
    spec = _spec_with_wells([["A1", "B2", "C3"]])
    suggestions = {
        "tube rack": LabwareMatchSuggestion(
            description="tube rack",
            suggested_label=None,
            positive_reasoning=None,
            why_not_in_instruction=None,
            confidence=0.0,
            candidates=["sample_rack", "reagent_rack", "dest_block"],
            branch="unresolvable",
        ),
    }

    ProtocolAgent._confirm_labware_assignments_via_handler(
        agent, spec, suggestions,
    )

    reasoning = handler.captured_table[0]["positive_reasoning"] or ""
    assert "Shape mismatch" not in reasoning


def test_warning_aggregates_wells_across_refs_sharing_a_description():
    """One ref's wells alone may fit; the union across every ref sharing
    a description determines the warning. Step 1 uses A1; step 2 uses
    C7. The row warns about C7 because the labware must host both."""
    config = _wb_config()
    handler = _CapturingHandler()
    agent = _make_stub_agent(config, handler)
    spec = _spec_with_wells([["A1"], ["C7"]])
    suggestions = {
        "tube rack": LabwareMatchSuggestion(
            description="tube rack",
            suggested_label="sample_rack",
            positive_reasoning="resolver picked sample_rack",
            why_not_in_instruction=None,
            confidence=0.8,
            candidates=["sample_rack", "reagent_rack", "dest_block"],
            branch="llm",
        ),
    }

    ProtocolAgent._confirm_labware_assignments_via_handler(
        agent, spec, suggestions,
    )

    reasoning = handler.captured_table[0]["positive_reasoning"] or ""
    assert "⚠ Shape mismatch:" in reasoning
    assert "'C7'" in reasoning


def test_warning_stacks_above_reviewer_objection_and_resolver_reasoning():
    """When shape mismatch AND a reviewer objection AND base reasoning
    all exist, they appear in order: shape mismatch first, reviewer
    objection second, resolver's reasoning under an "Original
    reasoning:" label."""
    config = _wb_config()
    handler = _CapturingHandler()
    agent = _make_stub_agent(config, handler)
    spec = _spec_with_wells([["C7"]])
    suggestions = {
        "tube rack": LabwareMatchSuggestion(
            description="tube rack",
            suggested_label="sample_rack",
            positive_reasoning="resolver said sample_rack",
            why_not_in_instruction=None,
            confidence=0.8,
            candidates=["sample_rack", "reagent_rack", "dest_block"],
            branch="llm",
        ),
    }
    reviewer_objections = {
        "tube rack": "the tubes you cite aren't sample_rack-shaped",
    }

    ProtocolAgent._confirm_labware_assignments_via_handler(
        agent, spec, suggestions, reviewer_objections=reviewer_objections,
    )

    reasoning = handler.captured_table[0]["positive_reasoning"] or ""
    shape_idx = reasoning.find("⚠ Shape mismatch:")
    reviewer_idx = reasoning.find("⚠ Reviewer flagged:")
    original_idx = reasoning.find("Original reasoning:")
    assert 0 <= shape_idx < reviewer_idx < original_idx, reasoning


def test_no_row_when_no_candidates_or_no_wells():
    """When a row has no candidates or the description's wells set is
    empty (e.g. a description that only appears as a non-wellled
    reference), no shape warning is emitted. Other row fields still
    populate normally."""
    config = _wb_config()
    handler = _CapturingHandler()
    agent = _make_stub_agent(config, handler)
    spec = _spec_with_wells([["A1"]])  # Only fitting wells.
    # Candidates list empty → no shape check possible.
    suggestions = {
        "tube rack": LabwareMatchSuggestion(
            description="tube rack",
            suggested_label=None,
            positive_reasoning=None,
            why_not_in_instruction=None,
            confidence=0.0,
            candidates=[],
            branch="unresolvable",
        ),
    }

    ProtocolAgent._confirm_labware_assignments_via_handler(
        agent, spec, suggestions,
    )

    row = handler.captured_table[0]
    assert (row["positive_reasoning"] is None
            or "Shape mismatch" not in row["positive_reasoning"])
