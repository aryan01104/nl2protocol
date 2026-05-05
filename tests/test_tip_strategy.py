"""
Tests for inferred tip strategy in spec_to_schema.

These tests are DETERMINISTIC — no LLM calls, no API keys needed.
They verify that spec_to_schema correctly decides when to reuse tips
vs pick up fresh ones, based on source well changes and mixing.
"""

import json
import pytest
from pathlib import Path

from nl2protocol.extraction import (
    CompleteProtocolSpec, ExtractedStep, ProvenancedVolume,
    Provenance, CompositionProvenance, LocationRef, PostAction,
    SemanticExtractor,
)
from nl2protocol.models import Transfer, PickUpTip, DropTip


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def config():
    config_path = Path(__file__).parent.parent / "test_cases" / "examples" / "simple_transfer" / "config.json"
    with open(config_path) as f:
        return json.load(f)


def _prov():
    return Provenance(source="instruction", cited_text="test cited text", confidence=1.0)


def _loc(**kwargs):
    """LocationRef with default test provenance — added when LocationRef.provenance
    became required at the field level (ADR-0007). Lets pre-existing test
    fixtures construct LocationRefs without each call having to spell out
    a fresh provenance object."""
    kwargs.setdefault("provenance", _prov())
    return LocationRef(**kwargs)


def _comp():
    return CompositionProvenance(
        step_cited_text="test step",
        parameters_cited_texts=["test step"],
        parameters_reasoning="test step",
        grounding=["instruction"],
        confidence=1.0,
    )


def _vol(value):
    return ProvenancedVolume(value=value, unit="uL", exact=True, provenance=_prov())


def _spec(steps, **kwargs):
    defaults = {
        "protocol_type": "test",
        "summary": "test protocol",
        "reasoning": "",
        "explicit_volumes": [],
        "initial_contents": [],
    }
    defaults.update(kwargs)
    return CompleteProtocolSpec(steps=steps, **defaults)


def _get_schema(steps, config, **kwargs):
    """Build a CompleteProtocolSpec and convert to schema."""
    spec = _spec(steps, **kwargs)
    schema, _, _ = SemanticExtractor.spec_to_schema(spec, config)
    return schema


def _tip_commands(schema):
    """Extract just Transfer, PickUpTip, and DropTip commands from schema."""
    return [
        cmd for cmd in schema.commands
        if isinstance(cmd, (Transfer, PickUpTip, DropTip))
    ]


# ============================================================================
# INFERRED TIP STRATEGY (tip_strategy unspecified)
# ============================================================================

class TestInferredTipStrategy:
    """When tip_strategy is not set, spec_to_schema infers from context."""

    def test_single_source_multiple_dests_reuses_tip(self, config):
        """Transferring from one source well to many destinations: one tip."""
        schema = _get_schema([
            ExtractedStep(
                order=1, action="transfer", volume=_vol(50),
                source=_loc(description="source_plate", well="A1",
                                   resolved_label="source_plate"),
                destination=_loc(description="dest_plate",
                                        well_range="A1-A4",
                                        resolved_label="dest_plate"),
                composition_provenance=_comp(),
            )
        ], config)

        cmds = _tip_commands(schema)
        pickups = [c for c in cmds if isinstance(c, PickUpTip)]
        drops = [c for c in cmds if isinstance(c, DropTip)]
        transfers = [c for c in cmds if isinstance(c, Transfer)]

        # 4 transfers, but only 1 tip (all from same source A1)
        assert len(transfers) == 4
        assert len(pickups) == 1
        assert len(drops) == 1
        for t in transfers:
            assert t.new_tip == "never"

    def test_multiple_sources_different_wells_changes_tips(self, config):
        """Each unique source well gets its own tip."""
        schema = _get_schema([
            ExtractedStep(
                order=1, action="transfer", volume=_vol(50),
                source=_loc(description="source_plate",
                                   well_range="A1-A3",
                                   resolved_label="source_plate"),
                destination=_loc(description="dest_plate",
                                        well_range="B1-B3",
                                        resolved_label="dest_plate"),
                composition_provenance=_comp(),
            )
        ], config)

        cmds = _tip_commands(schema)
        transfers = [c for c in cmds if isinstance(c, Transfer)]

        # 3 transfers, each from a different source — each gets its own tip
        assert len(transfers) == 3
        for t in transfers:
            assert t.new_tip == "always"

    def test_grouped_sources_reuses_within_group(self, config):
        """Replicates from same source well share a tip, different sources don't."""
        schema = _get_schema([
            ExtractedStep(
                order=1, action="transfer", volume=_vol(50),
                source=_loc(description="source_plate",
                                   well_range="A1-A2",
                                   resolved_label="source_plate"),
                destination=_loc(description="dest_plate",
                                        well_range="B1-B6",
                                        resolved_label="dest_plate"),
                replicates=3,
                composition_provenance=_comp(),
            )
        ], config)

        cmds = _tip_commands(schema)
        pickups = [c for c in cmds if isinstance(c, PickUpTip)]
        drops = [c for c in cmds if isinstance(c, DropTip)]
        transfers = [c for c in cmds if isinstance(c, Transfer)]

        # 2 source wells × 3 replicates = 6 transfers, but only 2 tips
        assert len(transfers) == 6
        assert len(pickups) == 2
        assert len(drops) == 2

    def test_mix_after_forces_new_tip_each(self, config):
        """When mixing after dispense, tip touches destination liquid — can't reuse."""
        schema = _get_schema([
            ExtractedStep(
                order=1, action="transfer", volume=_vol(50),
                source=_loc(description="source_plate", well="A1",
                                   resolved_label="source_plate"),
                destination=_loc(description="dest_plate",
                                        well_range="B1-B3",
                                        resolved_label="dest_plate"),
                post_actions=[PostAction(action="mix", repetitions=3,
                                         volume=_vol(50))],
                composition_provenance=_comp(),
            )
        ], config)

        cmds = _tip_commands(schema)
        transfers = [c for c in cmds if isinstance(c, Transfer)]
        pickups = [c for c in cmds if isinstance(c, PickUpTip)]

        # Same source well, but mixing — every transfer gets its own tip
        assert len(transfers) == 3
        assert len(pickups) == 0  # no manual PickUpTip, transfers handle their own
        for t in transfers:
            assert t.new_tip == "always"


# ============================================================================
# CROSS-STEP TIP OPTIMIZATION
# ============================================================================

class TestCrossStepTipOptimization:
    """Redundant DropTip/PickUpTip pairs between steps are removed."""

    def test_same_source_across_steps_shares_tip(self, config):
        """Two adjacent steps from same source, no mixing — one tip."""
        schema = _get_schema([
            ExtractedStep(
                order=1, action="transfer", volume=_vol(50),
                source=_loc(description="source_plate", well="A1",
                                   resolved_label="source_plate"),
                destination=_loc(description="dest_plate",
                                        well_range="B1-B2",
                                        resolved_label="dest_plate"),
                composition_provenance=_comp(),
            ),
            ExtractedStep(
                order=2, action="transfer", volume=_vol(50),
                source=_loc(description="source_plate", well="A1",
                                   resolved_label="source_plate"),
                destination=_loc(description="dest_plate",
                                        well_range="B3-B4",
                                        resolved_label="dest_plate"),
                composition_provenance=_comp(),
            ),
        ], config)

        cmds = _tip_commands(schema)
        pickups = [c for c in cmds if isinstance(c, PickUpTip)]
        drops = [c for c in cmds if isinstance(c, DropTip)]
        transfers = [c for c in cmds if isinstance(c, Transfer)]

        # 4 transfers across 2 steps, all from A1 — 1 tip total
        assert len(transfers) == 4
        assert len(pickups) == 1
        assert len(drops) == 1

    def test_different_source_across_steps_changes_tip(self, config):
        """Two adjacent steps from different sources — tip changes at boundary."""
        schema = _get_schema([
            ExtractedStep(
                order=1, action="transfer", volume=_vol(50),
                source=_loc(description="source_plate", well="A1",
                                   resolved_label="source_plate"),
                destination=_loc(description="dest_plate",
                                        well_range="B1-B2",
                                        resolved_label="dest_plate"),
                composition_provenance=_comp(),
            ),
            ExtractedStep(
                order=2, action="transfer", volume=_vol(50),
                source=_loc(description="source_plate", well="A2",
                                   resolved_label="source_plate"),
                destination=_loc(description="dest_plate",
                                        well_range="B3-B4",
                                        resolved_label="dest_plate"),
                composition_provenance=_comp(),
            ),
        ], config)

        cmds = _tip_commands(schema)
        pickups = [c for c in cmds if isinstance(c, PickUpTip)]
        drops = [c for c in cmds if isinstance(c, DropTip)]
        transfers = [c for c in cmds if isinstance(c, Transfer)]

        # 4 transfers, 2 from A1, 2 from A2 — 2 tips
        assert len(transfers) == 4
        assert len(pickups) == 2
        assert len(drops) == 2


# ============================================================================
# TIP STRATEGY FIELD IS IGNORED (deterministic algorithm always runs)
# ============================================================================

class TestTipStrategyFieldIgnored:
    """Even if the LLM sets tip_strategy, the deterministic algorithm runs."""

    def test_new_tip_each_ignored_single_source(self, config):
        """tip_strategy='new_tip_each' is ignored — same source still groups."""
        schema = _get_schema([
            ExtractedStep(
                order=1, action="transfer", volume=_vol(50),
                source=_loc(description="source_plate", well="A1",
                                   resolved_label="source_plate"),
                destination=_loc(description="dest_plate",
                                        well_range="B1-B4",
                                        resolved_label="dest_plate"),
                tip_strategy="new_tip_each",
                composition_provenance=_comp(),
            )
        ], config)

        cmds = _tip_commands(schema)
        pickups = [c for c in cmds if isinstance(c, PickUpTip)]
        drops = [c for c in cmds if isinstance(c, DropTip)]
        transfers = [c for c in cmds if isinstance(c, Transfer)]

        # Same source A1, no mixing — algorithm groups regardless of tip_strategy
        assert len(transfers) == 4
        assert len(pickups) == 1
        assert len(drops) == 1
