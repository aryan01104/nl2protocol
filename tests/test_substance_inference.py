"""Oracle reference resolution: a step's null substance is filled from its
source well's initial contents, stamped source="initial_state"."""

from types import SimpleNamespace

from nl2protocol.models.provenance_models import validate_provenance
from nl2protocol.pipeline import ProtocolAgent


def _agent():
    return ProtocolAgent.__new__(ProtocolAgent)


def _ic(labware, well, substance):
    return SimpleNamespace(labware=labware, well=well, substance=substance)


def _step(substance, resolved_label, well=None, wells=None, well_range=None):
    return SimpleNamespace(
        substance=substance,
        source=SimpleNamespace(resolved_label=resolved_label, well=well,
                               wells=wells, well_range=well_range,
                               description=resolved_label),
    )


def test_initial_state_source_routes_to_inferred_member():
    p = validate_provenance(
        {"source": "initial_state", "positive_reasoning": "x", "confidence": 0.9}
    )
    assert p.source == "initial_state"
    assert p.positive_reasoning == "x"


def test_fills_null_substance_from_source_well_contents():
    step = _step(substance=None, resolved_label="plate", well="A1")
    spec = SimpleNamespace(steps=[step], initial_contents=[_ic("plate", "A1", "sample")])
    _agent()._infer_substances_from_oracle(spec)
    assert step.substance is not None
    assert step.substance.value == "sample"
    assert step.substance.provenance.source == "initial_state"


def test_leaves_existing_substance_untouched():
    existing = SimpleNamespace(value="diluent")
    step = _step(substance=existing, resolved_label="reservoir", well="A1")
    spec = SimpleNamespace(steps=[step], initial_contents=[_ic("reservoir", "A1", "diluent")])
    _agent()._infer_substances_from_oracle(spec)
    assert step.substance is existing  # not overwritten


def test_skips_when_source_has_no_single_well():
    step = _step(substance=None, resolved_label="plate", well=None)
    spec = SimpleNamespace(steps=[step], initial_contents=[_ic("plate", "A1", "sample")])
    _agent()._infer_substances_from_oracle(spec)
    assert step.substance is None


def test_skips_when_no_matching_initial_contents():
    step = _step(substance=None, resolved_label="plate", well="B2")
    spec = SimpleNamespace(steps=[step], initial_contents=[_ic("plate", "A1", "sample")])
    _agent()._infer_substances_from_oracle(spec)
    assert step.substance is None


def test_fills_from_multi_well_source_when_all_wells_agree():
    step = _step(substance=None, resolved_label="plate", wells=["A1", "A2", "A3"])
    spec = SimpleNamespace(steps=[step], initial_contents=[
        _ic("plate", "A1", "sample"), _ic("plate", "A2", "sample"),
        _ic("plate", "A3", "sample"),
    ])
    _agent()._infer_substances_from_oracle(spec)
    assert step.substance is not None
    assert step.substance.value == "sample"
    assert step.substance.provenance.source == "initial_state"


def test_renders_substance_vector_when_wells_disagree():
    step = _step(substance=None, resolved_label="plate", wells=["A1", "A2", "A3"])
    spec = SimpleNamespace(steps=[step], initial_contents=[
        _ic("plate", "A1", "b"), _ic("plate", "A2", "a"), _ic("plate", "A3", "c"),
    ])
    _agent()._infer_substances_from_oracle(spec)
    assert step.substance is not None
    assert step.substance.value == "b, a, c"  # well order, not deduped/sorted
    assert step.substance.provenance.source == "initial_state"


def test_truncates_substance_vector_with_ellipsis():
    wells = [f"A{i}" for i in range(1, 9)]
    step = _step(substance=None, resolved_label="plate", wells=wells)
    spec = SimpleNamespace(steps=[step], initial_contents=[
        _ic("plate", w, f"reagent_{i}") for i, w in enumerate(wells)
    ])
    _agent()._infer_substances_from_oracle(spec)
    assert step.substance.value.endswith(", …")
    assert step.substance.value.count(",") < len(wells)  # not every item shown
    # full list survives in the provenance reasoning
    assert "reagent_7" in step.substance.provenance.positive_reasoning


def test_fills_from_well_range_source():
    step = _step(substance=None, resolved_label="plate", well_range="A1-A3")
    spec = SimpleNamespace(steps=[step], initial_contents=[
        _ic("plate", "A1", "sample"), _ic("plate", "A2", "sample"),
        _ic("plate", "A3", "sample"),
    ])
    _agent()._infer_substances_from_oracle(spec)
    assert step.substance is not None
    assert step.substance.value == "sample"
