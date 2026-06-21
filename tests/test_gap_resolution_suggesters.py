"""
Contract tests for the deterministic suggesters (ADR-0008 PR2).

Each suggester has its own class. Tests pin the inputs that should produce
a Suggestion (positive) and the inputs that should produce None (the
suggester's domain-of-applicability boundary).

LLMSpotSuggester + IndependentReviewSuggester get their own test files
since they need canned-LLM-response infrastructure.
"""

from __future__ import annotations

import pytest

from nl2protocol.models.spec import InstructionProvenance, InferredProvenance, validate_provenance
from nl2protocol.gap_resolution import Gap
from nl2protocol.gap_resolution.suggesters import (
    CarryoverSuggester,
    ConfigLookupSuggester,
    RegexFromNoteSuggester,
    WellCapacitySuggester,
    WellRangeClipSuggester,
)
from nl2protocol.models.spec import (
    CompositionProvenance,
    ExtractedStep,
    InstructionProvenance,
    LocationRef,
    ProvenanceBase,
    ProtocolSpec,
    ProvenancedDuration,
    ProvenancedString,
    ProvenancedTemperature,
    ProvenancedVolume,
    WellContents,
)


# ============================================================================
# FIXTURES
# ============================================================================

def _instr_prov(text: str = "test") -> ProvenanceBase:
    return InstructionProvenance(source="instruction", cited_text=text, confidence=1.0)


def _comp() -> CompositionProvenance:
    return CompositionProvenance(
        step_cited_text="test step",
        parameters_cited_texts=["test step"],
        parameters_reasoning="test cohesion",
        grounding=["instruction"],
        confidence=1.0,
    )


def _gap(field_path: str, kind: str = "missing", step_order: int = 1,
         description: str = "", current_value=None) -> Gap:
    return Gap(
        id=f"step{step_order}.{field_path.split('.')[-1]}",
        step_order=step_order,
        field_path=field_path,
        kind=kind,
        current_value=current_value,
        description=description,
        severity="blocker",
    )


# ============================================================================
# ConfigLookupSuggester
# ============================================================================

class TestConfigLookupSuggester:
    """Substance → source location via config.labware.contents."""

    def test_finds_substance_in_config_labware(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(
                order=1, action="transfer",
                substance=ProvenancedString(value="buffer", provenance=_instr_prov("buffer")),
                source=None,
                destination=LocationRef(description="dest", well="A1",
                                        description_provenance=_instr_prov("A1"), wells_provenance=_instr_prov("A1")),
                composition_provenance=_comp(),
            ),
        ])
        config = {"labware": {"reagent_rack": {"contents": {"B3": "buffer"}}}}
        gap = _gap("steps[0].source", description="missing source for buffer")
        s = ConfigLookupSuggester().suggest(spec=spec, gap=gap,
                                             context={"config": config})
        assert s is not None
        assert s.value.well == "B3"
        # PR3b bug-1 fix: description and resolved_label both equal the bare
        # config key, so ConstraintChecker's `resolved_label or description`
        # lookup finds a real config key and doesn't fire LABWARE_NOT_FOUND.
        assert s.value.description == "reagent_rack"
        assert s.value.resolved_label == "reagent_rack"
        # resolved_label_provenance carries the substance-match reasoning
        # (the reviewer pass reads this; the location's primary provenance
        # describes the synthesized ref instead).
        assert s.value.resolved_label_provenance is not None
        assert "buffer" in s.value.resolved_label_provenance.positive_reasoning
        # Silent-black-text guard (historical bug): both LocationRef
        # provenances must be populated with source="inferred", otherwise
        # the renderer shows the synthesized ref without the ▴ marker.
        assert s.value.description_provenance is not None
        assert s.value.description_provenance.source == "inferred"
        assert s.value.wells_provenance is not None
        assert s.value.wells_provenance.source == "inferred"
        assert s.provenance_source == "deterministic"
        assert "buffer" in s.positive_reasoning
        assert s.confidence == 0.9

    def test_suggester_output_passes_constraint_check(self):
        """PR3b bug-1 regression: feeding the suggester's LocationRef into
        the spec and re-running ConstraintViolationDetector must NOT emit
        a LABWARE_NOT_FOUND gap. Pre-fix, the suggester wrote a decorated
        description ('reagent_rack (inferred from config)') and left
        resolved_label=None, so the constraint check fell back to the
        decorated description and failed the config-key lookup."""
        from nl2protocol.gap_resolution import ConstraintViolationDetector
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(
                order=1, action="transfer",
                substance=ProvenancedString(value="buffer", provenance=_instr_prov("buffer")),
                volume=ProvenancedVolume(value=10.0, unit="uL", exact=True,
                                          provenance=_instr_prov("10uL")),
                source=None,
                destination=LocationRef(
                    description="reagent_rack", well="A1",
                    resolved_label="reagent_rack",
                    description_provenance=_instr_prov("A1"), wells_provenance=_instr_prov("A1"),
                ),
                composition_provenance=_comp(),
            ),
        ])
        config = {
            "labware": {
                "reagent_rack": {
                    "load_name": "opentrons_24_tuberack_eppendorf_2ml_safelock_snapcap",
                    "contents": {"B3": "buffer"},
                }
            },
            "pipettes": {"left": {"model": "p20_single_gen2"}},
        }
        # Run the suggester: produces a LocationRef.
        gap = _gap("steps[0].source", description="missing source for buffer")
        s = ConfigLookupSuggester().suggest(spec=spec, gap=gap,
                                             context={"config": config})
        # Apply suggester output to the spec.
        spec.steps[0].source = s.value
        # Re-detect: constraint check should be silent on LABWARE_NOT_FOUND.
        gaps = ConstraintViolationDetector().detect(spec, context={"config": config})
        labware_not_found = [g for g in gaps
                             if g.metadata.get("violation_type") == "labware_not_found"]
        assert labware_not_found == []

    def test_returns_none_when_substance_not_in_config(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(
                order=1, action="transfer",
                substance=ProvenancedString(value="rare_reagent",
                                            provenance=_instr_prov("rare_reagent")),
                source=None,
                destination=LocationRef(description="dest", well="A1",
                                        description_provenance=_instr_prov("A1"), wells_provenance=_instr_prov("A1")),
                composition_provenance=_comp(),
            ),
        ])
        config = {"labware": {"reagent_rack": {"contents": {"B3": "buffer"}}}}
        gap = _gap("steps[0].source")
        assert ConfigLookupSuggester().suggest(spec=spec, gap=gap,
                                                context={"config": config}) is None

    def test_returns_none_for_non_source_gap(self):
        # ConfigLookup only handles `.source` gaps.
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="comment", note="x",
                          composition_provenance=_comp())
        ])
        gap = _gap("steps[0].volume")
        assert ConfigLookupSuggester().suggest(spec=spec, gap=gap,
                                                context={"config": {}}) is None

    def test_returns_none_when_step_has_no_substance(self):
        # If the step doesn't name a substance, lookup has no key.
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(
                order=1, action="transfer",
                volume=ProvenancedVolume(value=10.0, unit="uL", exact=True,
                                         provenance=_instr_prov()),
                source=None,
                destination=LocationRef(description="dest", well="A1",
                                        description_provenance=_instr_prov("A1"), wells_provenance=_instr_prov("A1")),
                composition_provenance=_comp(),
            ),
        ])
        gap = _gap("steps[0].source")
        assert ConfigLookupSuggester().suggest(
            spec=spec, gap=gap,
            context={"config": {"labware": {"r": {"contents": {"A1": "buffer"}}}}}
        ) is None

    def test_does_not_mutate_input_spec(self):
        # The suggester returns a Suggestion; applying it is the caller's
        # job. The input spec must be untouched after .suggest() — otherwise
        # pipeline-state logs would diverge from what the prior stage emitted.
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(
                order=1, action="transfer",
                substance=ProvenancedString(value="buffer", provenance=_instr_prov("buffer")),
                source=None,
                destination=LocationRef(description="dest", well="A1",
                                        description_provenance=_instr_prov("A1"), wells_provenance=_instr_prov("A1")),
                composition_provenance=_comp(),
            ),
        ])
        config = {"labware": {"reagent_rack": {"contents": {"B3": "buffer"}}}}
        gap = _gap("steps[0].source", description="missing source for buffer")
        original = spec.model_dump()
        ConfigLookupSuggester().suggest(spec=spec, gap=gap,
                                        context={"config": config})
        assert spec.model_dump() == original


# ============================================================================
# CarryoverSuggester
# ============================================================================

class TestCarryoverSuggester:
    """wait_for_temperature inherits from prior set_temperature."""

    def test_inherits_from_immediately_prior_set_temperature(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="set_temperature",
                          temperature=ProvenancedTemperature(
                              value=4.0, provenance=_instr_prov("4°C")),
                          composition_provenance=_comp()),
            ExtractedStep(order=2, action="wait_for_temperature",
                          temperature=None,
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[1].temperature", step_order=2)
        s = CarryoverSuggester().suggest(spec=spec, gap=gap, context={})
        assert s is not None
        assert s.value.value == 4.0
        assert s.confidence == 0.95
        assert "4.0" in s.positive_reasoning
        # The ▴ tooltip names which prior step the value came from,
        # so the reasoning must mention "set_temperature".
        assert "set_temperature" in s.positive_reasoning

    def test_picks_most_recent_set_temperature(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="set_temperature",
                          temperature=ProvenancedTemperature(
                              value=4.0, provenance=_instr_prov("4°C")),
                          composition_provenance=_comp()),
            ExtractedStep(order=2, action="set_temperature",
                          temperature=ProvenancedTemperature(
                              value=42.0, provenance=_instr_prov("42°C")),
                          composition_provenance=_comp()),
            ExtractedStep(order=3, action="wait_for_temperature",
                          temperature=None,
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[2].temperature", step_order=3)
        s = CarryoverSuggester().suggest(spec=spec, gap=gap, context={})
        assert s is not None
        assert s.value.value == 42.0

    def test_returns_none_when_no_prior_set_temperature(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="wait_for_temperature",
                          temperature=None,
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[0].temperature")
        assert CarryoverSuggester().suggest(spec=spec, gap=gap, context={}) is None

    def test_returns_none_for_non_wait_step(self):
        # Carryover only applies to wait_for_temperature gaps.
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="set_temperature",
                          temperature=ProvenancedTemperature(
                              value=4.0, provenance=_instr_prov("4°C")),
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[0].temperature")
        assert CarryoverSuggester().suggest(spec=spec, gap=gap, context={}) is None

    def test_does_not_mutate_input_spec(self):
        # Same contract as ConfigLookupSuggester: .suggest() must be pure
        # w.r.t. the input spec.
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="set_temperature",
                          temperature=ProvenancedTemperature(
                              value=4.0, provenance=_instr_prov("4°C")),
                          composition_provenance=_comp()),
            ExtractedStep(order=2, action="wait_for_temperature",
                          temperature=None,
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[1].temperature", step_order=2)
        original = spec.model_dump()
        CarryoverSuggester().suggest(spec=spec, gap=gap, context={})
        assert spec.model_dump() == original


# ============================================================================
# WellCapacitySuggester
# ============================================================================

class TestWellCapacitySuggester:
    """Initial-volume defaults from labware well capacity."""

    def test_proposes_capacity_for_eppendorf(self):
        spec = ProtocolSpec(summary="t",
                            steps=[ExtractedStep(order=1, action="comment",
                                                 note="x",
                                                 composition_provenance=_comp())],
                            initial_contents=[
                                WellContents(labware="sample_rack", well="A1",
                                             substance="buffer", volume_ul=None)
                            ])
        config = {"labware": {"sample_rack": {
            "load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
        }}}
        gap = Gap(id="ic0.volume_ul", step_order=None,
                  field_path="initial_contents[0].volume_ul",
                  kind="missing", current_value=None,
                  description="initial volume null", severity="suggestion")
        s = WellCapacitySuggester().suggest(spec=spec, gap=gap,
                                             context={"config": config})
        assert s is not None
        assert s.value == 1500.0
        assert s.confidence == 0.7
        assert "well holds" in s.positive_reasoning

    def test_proposes_capacity_for_96_wellplate(self):
        spec = ProtocolSpec(summary="t",
                            steps=[ExtractedStep(order=1, action="comment",
                                                 note="x",
                                                 composition_provenance=_comp())],
                            initial_contents=[
                                WellContents(labware="assay_plate", well="A1",
                                             substance="media", volume_ul=None)
                            ])
        config = {"labware": {"assay_plate": {
            "load_name": "corning_96_wellplate_360ul_flat",
        }}}
        gap = Gap(id="ic0.volume_ul", step_order=None,
                  field_path="initial_contents[0].volume_ul",
                  kind="missing", current_value=None,
                  description="x", severity="suggestion")
        s = WellCapacitySuggester().suggest(spec=spec, gap=gap,
                                             context={"config": config})
        assert s is not None
        assert s.value == 300.0

    def test_returns_none_for_non_initial_contents_gap(self):
        spec = ProtocolSpec(summary="t",
                            steps=[ExtractedStep(order=1, action="comment",
                                                 note="x",
                                                 composition_provenance=_comp())])
        gap = _gap("steps[0].volume")  # different path shape
        assert WellCapacitySuggester().suggest(spec=spec, gap=gap,
                                                context={"config": {}}) is None

    def test_uses_labware_suggestions_when_available(self):
        # User description ('tube rack') doesn't literally match any config
        # key, but labware_suggestions resolves it to 'reagent_rack'. The
        # suggester should use that to look up the real load_name.
        spec = ProtocolSpec(summary="t",
                            steps=[ExtractedStep(order=1, action="comment",
                                                 note="x",
                                                 composition_provenance=_comp())],
                            initial_contents=[
                                WellContents(labware="tube rack", well="A1",
                                             substance="plasmid",
                                             volume_ul=None)
                            ])
        config = {"labware": {"reagent_rack": {
            "load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
        }}}

        class _FakeSug:
            def __init__(self, label):
                self.suggested_label = label
        labware_suggestions = {"tube rack": _FakeSug("reagent_rack")}

        gap = Gap(id="ic0.volume_ul", step_order=None,
                  field_path="initial_contents[0].volume_ul",
                  kind="missing", current_value=None,
                  description="x", severity="suggestion")
        s = WellCapacitySuggester().suggest(
            spec=spec, gap=gap,
            context={"config": config,
                     "labware_suggestions": labware_suggestions},
        )
        assert s is not None
        assert s.value == 1500.0
        # Resolved via labware_suggestions → terse "assumed full" reasoning;
        # value (1500) confirms it picked the right load_name.
        assert "well holds" in s.positive_reasoning

    def test_falls_back_when_no_resolution_anywhere(self):
        # Description matches neither labware_suggestions nor a config key.
        # Reasoning must be honest about the gap, not falsely cite a
        # non-existent labware.
        spec = ProtocolSpec(summary="t",
                            steps=[ExtractedStep(order=1, action="comment",
                                                 note="x",
                                                 composition_provenance=_comp())],
                            initial_contents=[
                                WellContents(labware="weird container",
                                             well="A1", substance="x",
                                             volume_ul=None)
                            ])
        gap = Gap(id="ic0.volume_ul", step_order=None,
                  field_path="initial_contents[0].volume_ul",
                  kind="missing", current_value=None,
                  description="x", severity="suggestion")
        s = WellCapacitySuggester().suggest(
            spec=spec, gap=gap,
            context={"config": {"labware": {}}},
        )
        assert s is not None
        assert s.value == 1500.0  # safe fallback
        # Unresolved → reasoning is honest the labware wasn't in config.
        assert "labware not in config" in s.positive_reasoning

    def test_suggestion_with_null_label_falls_back_to_direct_lookup(self):
        # labware_suggestions exists but the suggestion's label is None
        # (resolver couldn't pick). The suggester should try the legacy
        # path next instead of treating the suggestion's null as the
        # answer.
        spec = ProtocolSpec(summary="t",
                            steps=[ExtractedStep(order=1, action="comment",
                                                 note="x",
                                                 composition_provenance=_comp())],
                            initial_contents=[
                                WellContents(labware="sample_rack",
                                             well="A1", substance="x",
                                             volume_ul=None)
                            ])
        config = {"labware": {"sample_rack": {
            "load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
        }}}

        class _FakeSug:
            suggested_label = None
        labware_suggestions = {"sample_rack": _FakeSug()}

        gap = Gap(id="ic0.volume_ul", step_order=None,
                  field_path="initial_contents[0].volume_ul",
                  kind="missing", current_value=None,
                  description="x", severity="suggestion")
        s = WellCapacitySuggester().suggest(
            spec=spec, gap=gap,
            context={"config": config,
                     "labware_suggestions": labware_suggestions},
        )
        assert s is not None
        assert s.value == 1500.0
        # Direct-lookup path succeeded — value (1500) confirms resolution.
        assert "well holds" in s.positive_reasoning

    def test_stale_suggested_label_falls_back_to_direct_lookup(self):
        # CodeRabbit P1: a suggested_label that's no longer in the
        # active config must be ignored, not propagated through to a
        # broken load_name lookup. The suggester should fall back to
        # the legacy direct-config path when the suggestion is stale.
        spec = ProtocolSpec(summary="t",
                            steps=[ExtractedStep(order=1, action="comment",
                                                 note="x",
                                                 composition_provenance=_comp())],
                            initial_contents=[
                                WellContents(labware="sample_rack",
                                             well="A1", substance="x",
                                             volume_ul=None)
                            ])
        # Config has `sample_rack` as a real key — but suggestion
        # points at `nonexistent_label`, which isn't in config.
        config = {"labware": {"sample_rack": {
            "load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
        }}}

        class _FakeSug:
            suggested_label = "nonexistent_label"
        labware_suggestions = {"sample_rack": _FakeSug()}

        gap = Gap(id="ic0.volume_ul", step_order=None,
                  field_path="initial_contents[0].volume_ul",
                  kind="missing", current_value=None,
                  description="x", severity="suggestion")
        s = WellCapacitySuggester().suggest(
            spec=spec, gap=gap,
            context={"config": config,
                     "labware_suggestions": labware_suggestions},
        )
        assert s is not None
        assert s.value == 1500.0
        # Stale suggested_label ignored — direct lookup resolved (value 1500);
        # terse reasoning carries no label, so it cannot leak the stale one.
        assert "well holds" in s.positive_reasoning


# ============================================================================
# RegexFromNoteSuggester
# ============================================================================

class TestRegexFromNoteSuggester:
    """Extract duration from pause step notes."""

    def test_parses_minutes_from_note(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="pause",
                          note="incubate 5 minutes at room temp",
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[0].duration", description="missing duration")
        s = RegexFromNoteSuggester().suggest(spec=spec, gap=gap, context={})
        assert s is not None
        assert s.value.value == 5
        assert s.value.unit == "minutes"

    def test_parses_seconds_from_note(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="pause",
                          note="hold for 45 s briefly",
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[0].duration")
        s = RegexFromNoteSuggester().suggest(spec=spec, gap=gap, context={})
        assert s is not None
        assert s.value.value == 45
        assert s.value.unit == "seconds"

    def test_returns_none_when_note_has_no_duration(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="pause",
                          note="user transfers tubes manually",
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[0].duration")
        assert RegexFromNoteSuggester().suggest(spec=spec, gap=gap, context={}) is None

    def test_returns_none_for_non_pause_step(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="comment", note="wait 5 minutes",
                          composition_provenance=_comp()),
        ])
        gap = _gap("steps[0].duration")
        assert RegexFromNoteSuggester().suggest(spec=spec, gap=gap, context={}) is None


# ============================================================================
# WellRangeClipSuggester
# ============================================================================

class TestWellRangeClipSuggester:
    """Constraint violation: wells exceed labware range → propose clipping."""

    def test_proposes_clipped_wells_from_constraint_message(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="comment", note="x",
                          composition_provenance=_comp())
        ])
        gap = Gap(
            id="step1.constraint",
            step_order=1,
            field_path="steps[0].destination.wells",
            kind="constraint_violation",
            current_value=["A7", "A8"],
            description=(
                "Step 1: Wells ['A7', 'A8'] do not exist on 'sample_rack' "
                "(opentrons_24_tuberack). Valid well range: A1-D6."
            ),
            severity="blocker",
        )
        s = WellRangeClipSuggester().suggest(spec=spec, gap=gap, context={})
        assert s is not None
        assert "A1" in s.value
        assert "D6" in s.value
        # 24-position rack: A1-A6, B1-B6, C1-C6, D1-D6 = 24 wells
        assert len(s.value) == 24
        assert s.confidence == 0.7

    def test_returns_none_for_non_constraint_gap(self):
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="comment", note="x",
                          composition_provenance=_comp())
        ])
        gap = _gap("steps[0].volume")
        assert WellRangeClipSuggester().suggest(spec=spec, gap=gap, context={}) is None

    def test_returns_none_when_description_doesnt_match(self):
        # Description doesn't have the expected pattern; can't propose a fix.
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="comment", note="x",
                          composition_provenance=_comp())
        ])
        gap = Gap(id="x", step_order=1, field_path="x",
                  kind="constraint_violation", current_value=None,
                  description="some other constraint problem",
                  severity="blocker")
        assert WellRangeClipSuggester().suggest(spec=spec, gap=gap, context={}) is None



# ============================================================================
# LLMSpotSuggester scope-guidance prompt (Phase 7 follow-up)
# ============================================================================

from nl2protocol.gap_resolution.suggesters import LLMSpotSuggester


class TestLLMSpotSuggesterPromptScope:
    """The spot suggester proposes a value for ONE field. Its prompt
    must explicitly tell the LLM that the reasoning is scoped to THAT
    field and must not imply changes elsewhere — otherwise the LLM
    happily writes "A1 on dest_block" reasoning when only the well is
    being changed (the labware stays sample_rack), and the user is
    misled by reasoning that argues for a change that won't happen."""

    class _StubClient:
        def __init__(self):
            self.messages = self
            self.create_calls = []

        def create(self, **kwargs):
            self.create_calls.append(kwargs)
            class _R:
                content = [type("C", (), {"text": '{"value": "A1", '
                                          '"positive_reasoning": "ok", '
                                          '"why_not_in_instruction": "ok", '
                                          '"confidence": 0.6}'})()]
            return _R()

    def _spec(self):
        comp = CompositionProvenance(
            step_cited_text="t", parameters_cited_texts=["t"],
            parameters_reasoning="t", grounding=["instruction"], confidence=1.0,
        )
        step = ExtractedStep(
            order=1, action="transfer", composition_provenance=comp,
            destination=LocationRef(
                description="rack", well="A1",
                description_provenance=_instr_prov("A1"),
                wells_provenance=_instr_prov("A1"),
            ),
        )
        return ProtocolSpec(summary="t", steps=[step])

    def _gap(self):
        return Gap(
            id="g1", step_order=1, field_path="steps[0].destination.well",
            kind="missing", current_value=None,
            description="well missing", severity="blocker", metadata={},
        )

    def test_prompt_names_scope_constraint(self):
        client = self._StubClient()
        sug = LLMSpotSuggester(client=client, model_name="x")
        sug.suggest(self._gap(), self._spec(), context={
            "instruction": "Add 5uL", "config": {"labware": {}},
        })
        assert len(client.create_calls) == 1
        prompt = client.create_calls[0]["messages"][0]["content"]
        assert "SCOPE" in prompt
        assert "only the value of THIS field" in prompt.lower() \
            or "only the value of this field" in prompt.lower()

    def test_prompt_carries_bad_vs_good_example(self):
        client = self._StubClient()
        sug = LLMSpotSuggester(client=client, model_name="x")
        sug.suggest(self._gap(), self._spec(), context={
            "instruction": "x", "config": {},
        })
        prompt = client.create_calls[0]["messages"][0]["content"]
        # Anti-pattern: reasoning implying a labware change that won't happen.
        assert "BAD" in prompt
        assert "heating block" in prompt
        # Counter-example showing scope-respecting reasoning.
        assert "GOOD" in prompt

    def test_prompt_steers_low_confidence_when_field_constrained(self):
        client = self._StubClient()
        sug = LLMSpotSuggester(client=client, model_name="x")
        sug.suggest(self._gap(), self._spec(), context={
            "instruction": "x", "config": {},
        })
        prompt = client.create_calls[0]["messages"][0]["content"]
        # The prompt must tell the LLM what to do when it can't justify
        # the value without implying a change elsewhere — confidence < 0.5
        # is the signal, not an invented justification.
        assert "confidence < 0.5" in prompt


class TestLLMSpotSuggesterCitedBranching:
    """Suggestion.provenance_source = "cited" iff the LLM returned a
    non-null cited_text. Drives the gap-modal badge text: a value the
    LLM identified as verbatim-present in the instruction shows up as
    "(cited)" instead of "(inferred)"."""

    @staticmethod
    def _stub_client(response_json: str):
        """Build a stub client that returns the given JSON body once."""
        class _Stub:
            def __init__(self):
                self.messages = self
            def create(self, **kwargs):
                class _R:
                    content = [type("C", (), {"text": response_json})()]
                return _R()
        return _Stub()

    def _spec(self):
        comp = CompositionProvenance(
            step_cited_text="t", parameters_cited_texts=["t"],
            parameters_reasoning="t", grounding=["instruction"], confidence=1.0,
        )
        step = ExtractedStep(
            order=1, action="transfer", composition_provenance=comp,
            destination=LocationRef(
                description="rack", well="A1",
                description_provenance=_instr_prov("A1"),
                wells_provenance=_instr_prov("A1"),
            ),
        )
        return ProtocolSpec(summary="t", steps=[step])

    def _gap(self):
        return Gap(
            id="g1", step_order=1, field_path="steps[0].destination.well",
            kind="missing", current_value=None,
            description="well missing", severity="blocker", metadata={},
        )

    def test_cited_text_present_labels_as_cited(self):
        client = self._stub_client(
            '{"value": "A1", "cited_text": "well A1", '
            '"positive_reasoning": "ok", "why_not_in_instruction": "ok", '
            '"confidence": 0.8}'
        )
        result = LLMSpotSuggester(client=client, model_name="x").suggest(
            self._gap(), self._spec(),
            context={"instruction": "...well A1...", "config": {}},
        )
        assert result is not None
        assert result.provenance_source == "cited"

    def test_cited_text_null_labels_as_inferred(self):
        client = self._stub_client(
            '{"value": "A1", "cited_text": null, '
            '"positive_reasoning": "ok", "why_not_in_instruction": "ok", '
            '"confidence": 0.6}'
        )
        result = LLMSpotSuggester(client=client, model_name="x").suggest(
            self._gap(), self._spec(),
            context={"instruction": "x", "config": {}},
        )
        assert result is not None
        assert result.provenance_source == "inferred"

    def test_cited_text_field_omitted_labels_as_inferred(self):
        client = self._stub_client(
            '{"value": "A1", "positive_reasoning": "ok", '
            '"why_not_in_instruction": "ok", "confidence": 0.6}'
        )
        result = LLMSpotSuggester(client=client, model_name="x").suggest(
            self._gap(), self._spec(),
            context={"instruction": "x", "config": {}},
        )
        assert result is not None
        assert result.provenance_source == "inferred"

    def test_cited_text_propagates_onto_suggestion(self):
        """When the LLM returns cited_text, Suggestion.cited_text carries
        the substring through — not just the label. The apply path reads
        this to stamp InstructionProvenance(source="instruction", cited_text=[...])
        on the spec field, so the value renders in the report with the
        same encoding as an extractor-sourced citation."""
        client = self._stub_client(
            '{"value": "95", "cited_text": "Set temperature to 95°C", '
            '"positive_reasoning": "ok", "why_not_in_instruction": "ok", '
            '"confidence": 0.9}'
        )
        result = LLMSpotSuggester(client=client, model_name="x").suggest(
            self._gap(), self._spec(),
            context={"instruction": "Set temperature to 95°C", "config": {}},
        )
        assert result is not None
        assert result.cited_text == "Set temperature to 95°C"

    def test_prompt_includes_cited_text_instruction(self):
        client = self._stub_client(
            '{"value": "A1", "cited_text": null, '
            '"positive_reasoning": "ok", "why_not_in_instruction": "ok", '
            '"confidence": 0.6}'
        )
        # Stash the call args so we can inspect the prompt.
        calls = []
        orig_create = client.create
        def _capture(**kwargs):
            calls.append(kwargs)
            return orig_create(**kwargs)
        client.create = _capture
        LLMSpotSuggester(client=client, model_name="x").suggest(
            self._gap(), self._spec(),
            context={"instruction": "x", "config": {}},
        )
        prompt = calls[0]["messages"][0]["content"]
        assert "cited_text" in prompt
        assert "CITATION CHECK" in prompt


# ============================================================================
# LLMSpotSuggester structured tool-use path (form-aware suggester)
# ============================================================================


class TestLLMSpotSuggesterStructured:
    """For top-level Provenanced/LocationRef gap paths, the suggester
    must call Anthropic with tool-use so the API enforces shape, then
    return a Suggestion whose value is a properly-typed Pydantic model
    instance — never a raw dict/int/string. This is the structural
    fix for the boundary bug that crashed eval 01."""

    @staticmethod
    def _tool_use_client(tool_input: dict):
        """Stub that returns one Anthropic-style response carrying a
        single tool_use block with `input` set to the given dict."""
        class _Block:
            def __init__(self, payload: dict):
                self.type = "tool_use"
                self.input = payload
        class _Resp:
            def __init__(self, payload: dict):
                self.content = [_Block(payload)]
        class _Stub:
            def __init__(self):
                self.messages = self
                self.create_calls = []
            def create(self, **kwargs):
                self.create_calls.append(kwargs)
                return _Resp(tool_input)
        return _Stub()

    def _spec_with_missing_source(self) -> ProtocolSpec:
        step = ExtractedStep(order=1, action="transfer", composition_provenance=_comp())
        return ProtocolSpec(summary="t", steps=[step])

    def _gap_at(self, field_path: str) -> Gap:
        return Gap(
            id=f"g.{field_path}", step_order=1, field_path=field_path,
            kind="missing", current_value=None,
            description=f"missing {field_path}", severity="blocker",
            metadata={},
        )

    def test_source_path_invokes_tool_use(self):
        client = self._tool_use_client({
            "description": "reagent_rack",
            "well": "A1",
            "positive_reasoning": "rack hosts the substance",
            "why_not_in_instruction": "instruction names no well",
            "cited_text": None,
            "confidence": 0.7,
        })
        sug = LLMSpotSuggester(client=client, model_name="x")
        result = sug.suggest(
            self._gap_at("steps[0].source"),
            self._spec_with_missing_source(),
            context={"instruction": "x", "config": {"labware": {}}},
        )
        # Tool-use was invoked with the LocationRef schema.
        assert len(client.create_calls) == 1
        call = client.create_calls[0]
        assert "tools" in call and len(call["tools"]) == 1
        assert call["tools"][0]["name"] == "propose_location_ref"
        assert call["tool_choice"] == {"type": "tool", "name": "propose_location_ref"}
        # The returned Suggestion.value is a real LocationRef, not a dict.
        assert result is not None
        assert isinstance(result.value, LocationRef)
        assert result.value.description == "reagent_rack"
        assert result.value.well == "A1"
        # Inner Provenance is well-formed.
        assert result.value.description_provenance.source == "inferred"
        assert result.value.wells_provenance.source == "inferred"

    def test_volume_path_returns_provenanced_volume_instance(self):
        client = self._tool_use_client({
            "value": 100,
            "unit": "uL",
            "exact": True,
            "positive_reasoning": "instruction names 100 uL",
            "why_not_in_instruction": None,
            "cited_text": "100 uL",
            "confidence": 0.95,
        })
        sug = LLMSpotSuggester(client=client, model_name="x")
        result = sug.suggest(
            self._gap_at("steps[0].volume"),
            self._spec_with_missing_source(),
            context={"instruction": "Add 100 uL ...", "config": {}},
        )
        assert result is not None
        assert isinstance(result.value, ProvenancedVolume)
        assert result.value.value == 100.0
        assert result.value.unit == "uL"
        # Inner Provenance was built as instruction-cited (cited_text non-null).
        assert result.value.provenance.source == "instruction"
        assert result.value.provenance.cited_text == ["100 uL"]
        # Suggestion-level provenance_source reflects the cite branch.
        assert result.provenance_source == "cited"
        assert result.cited_text == "100 uL"

    def test_subfield_path_uses_freeform_path(self):
        """A gap on a subfield (e.g. `.source.well`) targets a primitive,
        not a Pydantic model. The structured dispatch must NOT match —
        the legacy free-form text-JSON path keeps handling it so the
        existing prompt-scope tests (which fire on `.destination.well`)
        stay valid."""
        class _Text:
            def __init__(self):
                self.messages = self
                self.kwargs = None
            def create(self, **kwargs):
                self.kwargs = kwargs
                class _C:
                    text = ('{"value": "A1", '
                            '"positive_reasoning": "ok", '
                            '"why_not_in_instruction": "ok", '
                            '"confidence": 0.6, "cited_text": null}')
                class _R:
                    content = [_C()]
                return _R()
        client = _Text()
        sug = LLMSpotSuggester(client=client, model_name="x")
        sug.suggest(
            self._gap_at("steps[0].destination.well"),
            self._spec_with_missing_source(),
            context={"instruction": "x", "config": {}},
        )
        # Free-form path: NO tools key in the call.
        assert "tools" not in client.kwargs

    def test_builder_validation_failure_returns_none(self):
        """If the LLM emits a value that fails Pydantic validation
        (e.g. negative volume), the suggester returns None — the gap
        propagates to the user, no spec corruption."""
        client = self._tool_use_client({
            "value": -5,  # gt=0 constraint on ProvenancedVolume.value
            "unit": "uL",
            "exact": True,
            "positive_reasoning": "bad",
            "why_not_in_instruction": "n/a",
            "cited_text": None,
            "confidence": 0.5,
        })
        sug = LLMSpotSuggester(client=client, model_name="x")
        result = sug.suggest(
            self._gap_at("steps[0].volume"),
            self._spec_with_missing_source(),
            context={"instruction": "x", "config": {}},
        )
        assert result is None
