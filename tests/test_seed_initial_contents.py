"""Test the oracle seeding step: the uploaded map replaces spec.initial_contents."""

from types import SimpleNamespace

from nl2protocol.pipeline import ProtocolAgent
from nl2protocol.stage_1_pre_extraction.initial_state import InitialStateSheet


def _agent():
    return ProtocolAgent.__new__(ProtocolAgent)


def test_seed_replaces_initial_contents_from_oracle():
    sheet = InitialStateSheet(cells={
        ("sample_plate", "B1"): ("DNA template", 5.0),
        ("reagent_rack", "A1"): ("master mix", 300.0),
        ("sample_plate", "A1"): ("DNA template", 5.0),
    }, errors=[])
    # extractor produced something unrelated; the oracle must overwrite it wholesale
    spec = SimpleNamespace(initial_contents=["stale-extractor-entry"])

    _agent()._seed_initial_contents_from_oracle(spec, sheet)

    ic = spec.initial_contents
    assert len(ic) == 3
    # one WellContents per cell, keyed on the config label straight from the sheet
    assert {(e.labware, e.well) for e in ic} == {
        ("sample_plate", "A1"), ("sample_plate", "B1"), ("reagent_rack", "A1"),
    }
    # values + provenance carried through
    first = {(e.labware, e.well): e for e in ic}
    assert first[("reagent_rack", "A1")].substance == "master mix"
    assert first[("reagent_rack", "A1")].volume_ul == 300.0
    assert first[("sample_plate", "A1")].volume_ul_provenance.source == "inferred"


def test_seed_is_sorted_by_labware_then_well():
    sheet = InitialStateSheet(cells={
        ("sample_plate", "B1"): ("x", 1.0),
        ("reagent_rack", "A1"): ("y", 2.0),
        ("sample_plate", "A1"): ("x", 1.0),
    }, errors=[])
    spec = SimpleNamespace(initial_contents=[])

    _agent()._seed_initial_contents_from_oracle(spec, sheet)

    order = [(e.labware, e.well) for e in spec.initial_contents]
    assert order == [
        ("reagent_rack", "A1"), ("sample_plate", "A1"), ("sample_plate", "B1"),
    ]
