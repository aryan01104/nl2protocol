"""Oracle source-well resolution: an unspecified source well is matched to the
well that holds the step's substance in the uploaded initial-state map."""

from types import SimpleNamespace

from nl2protocol.pipeline import ProtocolAgent


def _agent():
    return ProtocolAgent.__new__(ProtocolAgent)


def _ic(labware, well, substance):
    return SimpleNamespace(labware=labware, well=well, substance=substance)


def _step(substance, resolved_label, well=None, wells=None, well_range=None, order=1):
    sub = SimpleNamespace(value=substance) if substance is not None else None
    ref = SimpleNamespace(
        resolved_label=resolved_label, description=resolved_label,
        well=well, wells=wells, well_range=well_range, wells_provenance=None,
    )
    return SimpleNamespace(substance=sub, source=ref, order=order)


def test_single_match_sets_well_grounded_in_map():
    step = _step("diluent", "reservoir")
    spec = SimpleNamespace(steps=[step], initial_contents=[_ic("reservoir", "A1", "diluent")])
    _agent()._resolve_source_wells_from_oracle(spec)
    assert step.source.well == "A1"
    assert step.source.wells_provenance.source == "initial_state"


def test_no_match_defaults_to_a1_marked_default():
    step = _step("buffer", "reservoir")
    spec = SimpleNamespace(steps=[step], initial_contents=[_ic("reservoir", "A1", "diluent")])
    _agent()._resolve_source_wells_from_oracle(spec)
    assert step.source.well == "A1"
    assert step.source.wells_provenance.source == "inferred"
    assert "defaulted" in step.source.wells_provenance.positive_reasoning.lower()


def test_multiple_matches_phase1_uses_first_pending_confirm():
    step = _step("diluent", "reservoir")
    spec = SimpleNamespace(steps=[step], initial_contents=[
        _ic("reservoir", "A1", "diluent"),
        _ic("reservoir", "A2", "diluent"),
    ])
    _agent()._resolve_source_wells_from_oracle(spec)
    assert step.source.well == "A1"
    assert step.source.wells_provenance.source == "inferred"
    assert "confirm" in step.source.wells_provenance.positive_reasoning.lower()


def test_case_insensitive_substance_match():
    step = _step("DILUENT", "reservoir")
    spec = SimpleNamespace(steps=[step], initial_contents=[_ic("reservoir", "B3", "diluent")])
    _agent()._resolve_source_wells_from_oracle(spec)
    assert step.source.well == "B3"
    assert step.source.wells_provenance.source == "initial_state"


def test_ref_with_existing_well_is_untouched():
    step = _step("diluent", "reservoir", well="C5")
    spec = SimpleNamespace(steps=[step], initial_contents=[_ic("reservoir", "A1", "diluent")])
    _agent()._resolve_source_wells_from_oracle(spec)
    assert step.source.well == "C5"
    assert step.source.wells_provenance is None  # not overwritten


class _StubChooser:
    def __init__(self, mapping):
        self.mapping = mapping
        self.rows = None

    def confirm(self, rows):
        self.rows = rows
        return self.mapping


def test_multiple_matches_with_handler_applies_user_choice():
    step = _step("diluent", "reservoir", order=1)
    spec = SimpleNamespace(steps=[step], initial_contents=[
        _ic("reservoir", "A1", "diluent"),
        _ic("reservoir", "A2", "diluent"),
    ])
    agent = _agent()
    agent.source_well_handler = _StubChooser({"s1-source": "A2"})
    agent._resolve_source_wells_from_oracle(spec)
    assert step.source.well == "A2"
    assert step.source.wells_provenance.source == "initial_state"
    # The chooser was handed the candidate wells.
    assert agent.source_well_handler.rows[0]["candidates"] == ["A1", "A2"]
    assert agent.source_well_handler.rows[0]["key"] == "s1-source"


def test_multiple_matches_handler_abort_falls_back_to_default():
    step = _step("diluent", "reservoir", order=1)
    spec = SimpleNamespace(steps=[step], initial_contents=[
        _ic("reservoir", "A1", "diluent"),
        _ic("reservoir", "A2", "diluent"),
    ])
    agent = _agent()
    agent.source_well_handler = _StubChooser(None)  # abort/timeout
    agent._resolve_source_wells_from_oracle(spec)
    assert step.source.well == "A1"  # first match
    assert step.source.wells_provenance.source == "inferred"


def test_skips_when_no_substance_or_no_label():
    no_sub = _step(None, "reservoir")
    no_label = _step("diluent", None)
    spec = SimpleNamespace(steps=[no_sub, no_label],
                           initial_contents=[_ic("reservoir", "A1", "diluent")])
    _agent()._resolve_source_wells_from_oracle(spec)
    assert no_sub.source.well is None
    assert no_label.source.well is None
