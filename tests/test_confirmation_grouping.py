"""
Unit tests for the confirmation-queue helpers in ProtocolAgent.

Covers two deterministic helpers added to fix the PCR-mastermix UX issue
(25 separate prompts for 24 wells of the same prefilled labware):

  - ProtocolAgent._summarize_well_list — pure function on a list of wells.
  - ProtocolAgent._build_initial_volume_queue — groups null-volume
    WellContents by (labware, substance) into batched confirmation entries.

Both are pure: no LLM, no I/O, no API key needed.
"""

import pytest

from nl2protocol.pipeline import ProtocolAgent
from nl2protocol.extraction import (
    ExtractedStep, LocationRef, ProvenancedVolume, Provenance,
    CompositionProvenance, ProtocolSpec, WellContents,
)


# ============================================================================
# FIXTURES
# ============================================================================

def _prov():
    return Provenance(source="instruction", reason="t", confidence=1.0)


def _comp():
    return CompositionProvenance(
        justification="t", grounding=["instruction"], confidence=1.0,
    )


def _vol(v):
    return ProvenancedVolume(value=v, unit="uL", exact=True, provenance=_prov())


def _spec(initial_contents=None):
    return ProtocolSpec(
        summary="test",
        steps=[ExtractedStep(
            order=1, action="transfer", volume=_vol(2.0),
            source=LocationRef(description="src", well="A1"),
            destination=LocationRef(description="dst", well="A1"),
            composition_provenance=_comp(),
        )],
        initial_contents=initial_contents or [],
    )


SAMPLE_PLATE_LOAD = "nest_96_wellplate_100ul_pcr_full_skirt"
TUBE_RACK_LOAD = "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap"


@pytest.fixture
def config():
    return {
        "labware": {
            "sample_plate": {"load_name": SAMPLE_PLATE_LOAD},
            "tube_rack": {"load_name": TUBE_RACK_LOAD},
            "reagent": {"load_name": SAMPLE_PLATE_LOAD},
        }
    }


# ============================================================================
# _summarize_well_list — pure projection of a well list to display string
# ============================================================================

class TestSummarizeWellList:
    """The display contract: contiguous rectangular block → 'A1-H3',
    irregular set → 'A1, A2, A3, … (N wells)', single → 'A1', empty → ''."""

    def test_empty(self):
        assert ProtocolAgent._summarize_well_list([]) == ""

    def test_single_well(self):
        assert ProtocolAgent._summarize_well_list(["A1"]) == "A1"
        assert ProtocolAgent._summarize_well_list(["H12"]) == "H12"

    def test_full_24_well_block(self):
        # The pcr_mastermix case: A1-H3 is 8 rows × 3 cols
        wells = [f"{r}{c}" for r in "ABCDEFGH" for c in range(1, 4)]
        assert ProtocolAgent._summarize_well_list(wells) == "A1-H3"

    def test_full_12_well_row(self):
        wells = [f"A{c}" for c in range(1, 13)]
        assert ProtocolAgent._summarize_well_list(wells) == "A1-A12"

    def test_full_8_well_column(self):
        wells = [f"{r}1" for r in "ABCDEFGH"]
        assert ProtocolAgent._summarize_well_list(wells) == "A1-H1"

    def test_unordered_input_still_collapses(self):
        # Order shouldn't matter — same set of wells gives same range
        wells = ["C2", "A1", "B3", "A3", "C1", "A2", "B1", "C3", "B2"]
        assert ProtocolAgent._summarize_well_list(wells) == "A1-C3"

    def test_irregular_set_falls_back(self):
        # Non-rectangular: missing the middle well, can't be a block
        result = ProtocolAgent._summarize_well_list(["A1", "C5", "D9", "H12"])
        assert "(4 wells)" in result
        assert result.startswith("A1, C5, D9")

    def test_partial_block_not_a_range(self):
        # 3 wells from a 4-well rectangle is NOT a rectangle — must fall back
        wells = ["A1", "A2", "B1"]  # missing B2
        result = ProtocolAgent._summarize_well_list(wells)
        assert "(3 wells)" in result
        assert "A1-B2" not in result

    def test_two_wells_same_row(self):
        # Two contiguous wells → still a valid range
        assert ProtocolAgent._summarize_well_list(["A1", "A2"]) == "A1-A2"


# ============================================================================
# _build_initial_volume_queue — grouped confirmation queue
# ============================================================================

class TestBuildInitialVolumeQueue:
    """Contract: groups null-volume WellContents by (labware, substance).
    Singletons emit type='initial_volume', groups emit 'initial_volume_group'.
    Already-stated volumes never appear in the queue."""

    def test_no_initial_contents(self, config):
        queue = ProtocolAgent._build_initial_volume_queue(_spec([]), config)
        assert queue == []

    def test_all_volumes_stated_means_empty_queue(self, config):
        # Both wells have explicit volumes → nothing to confirm
        ic = [
            WellContents(labware="sample_plate", well="A1",
                         substance="DNA", volume_ul=50.0),
            WellContents(labware="tube_rack", well="A1",
                         substance="buffer", volume_ul=500.0),
        ]
        queue = ProtocolAgent._build_initial_volume_queue(_spec(ic), config)
        assert queue == []

    def test_single_null_volume_emits_singleton(self, config):
        ic = [WellContents(labware="tube_rack", well="A1",
                           substance="master mix")]
        queue = ProtocolAgent._build_initial_volume_queue(_spec(ic), config)
        assert len(queue) == 1
        assert queue[0]["type"] == "initial_volume"
        assert queue[0]["labware"] == "tube_rack"
        assert queue[0]["well"] == "A1"
        assert queue[0]["substance"] == "master mix"

    def test_group_of_24_collapses_to_one_entry(self, config):
        # The pcr_mastermix regression case
        ic = [
            WellContents(labware="sample_plate", well=f"{r}{c}",
                         substance="DNA template")
            for r in "ABCDEFGH" for c in range(1, 4)
        ]
        queue = ProtocolAgent._build_initial_volume_queue(_spec(ic), config)
        assert len(queue) == 1
        entry = queue[0]
        assert entry["type"] == "initial_volume_group"
        assert entry["labware"] == "sample_plate"
        assert entry["substance"] == "DNA template"
        assert len(entry["wells"]) == 24

    def test_different_substances_dont_merge(self, config):
        # Same labware, different substances → 2 entries, not 1
        ic = [
            WellContents(labware="sample_plate", well="A1", substance="DNA"),
            WellContents(labware="sample_plate", well="A2", substance="DNA"),
            WellContents(labware="sample_plate", well="B1", substance="buffer"),
            WellContents(labware="sample_plate", well="B2", substance="buffer"),
        ]
        queue = ProtocolAgent._build_initial_volume_queue(_spec(ic), config)
        assert len(queue) == 2
        substances = {e["substance"] for e in queue}
        assert substances == {"DNA", "buffer"}
        # Each is a group of 2
        for e in queue:
            assert e["type"] == "initial_volume_group"
            assert len(e["wells"]) == 2

    def test_different_labware_dont_merge(self, config):
        ic = [
            WellContents(labware="sample_plate", well="A1", substance="DNA"),
            WellContents(labware="sample_plate", well="A2", substance="DNA"),
            WellContents(labware="reagent", well="A1", substance="DNA"),
            WellContents(labware="reagent", well="A2", substance="DNA"),
        ]
        queue = ProtocolAgent._build_initial_volume_queue(_spec(ic), config)
        labware = {e["labware"] for e in queue}
        assert labware == {"sample_plate", "reagent"}

    def test_mixed_stated_and_null(self, config):
        # Some wells have volumes, some don't. Only nulls reach the queue.
        ic = [
            WellContents(labware="sample_plate", well="A1",
                         substance="DNA", volume_ul=50.0),
            WellContents(labware="sample_plate", well="A2", substance="DNA"),
            WellContents(labware="sample_plate", well="A3", substance="DNA"),
        ]
        queue = ProtocolAgent._build_initial_volume_queue(_spec(ic), config)
        assert len(queue) == 1
        assert queue[0]["type"] == "initial_volume_group"
        assert set(queue[0]["wells"]) == {"A2", "A3"}

    def test_default_volume_uses_well_capacity(self, config):
        # Singleton on a tube rack should pick up the larger capacity
        ic = [WellContents(labware="tube_rack", well="A1", substance="x")]
        queue = ProtocolAgent._build_initial_volume_queue(_spec(ic), config)
        # Tube rack capacity is much larger than a 96-well plate well
        assert queue[0]["default_volume"] > 100.0

    def test_default_volume_falls_back_when_unknown_labware(self):
        # Labware not in config → falls back to the 15000uL hard-coded default
        ic = [WellContents(labware="mystery_box", well="A1", substance="x")]
        queue = ProtocolAgent._build_initial_volume_queue(_spec(ic), {})
        assert queue[0]["default_volume"] == 15000.0
