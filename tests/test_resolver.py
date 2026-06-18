"""Tests for nl2protocol/extraction/resolver.py — labware resolver.

Covers (a) `_parse_assignment` (new-shape vs legacy-shape unpacking),
(b) `_positive_reasoning` (LLM-reasoning vs honest-fallback branches),
(c) `_llm_resolve` end-to-end with a fake Anthropic client,
(d) `suggest` integration on a minimal ProtocolSpec,
(e) capability + type filters and branch-resolving helpers (Phase 1+).
"""

import json
import pytest
from typing import Any, Optional

from nl2protocol.models.spec import InstructionProvenance, InferredProvenance, validate_provenance
from nl2protocol.extraction.resolver import (
    LabwareMatcher, LabwareMatchSuggestion,
    _load_name_category, _description_category_filter,
    _capability_filter, _type_filter, _has_namespace_split,
)
from nl2protocol.models.spec import (
    CompositionProvenance,
    ExtractedStep,
    InstructionProvenance,
    LocationRef,
    Provenance,
    ProtocolSpec,
    WellContents,
)


# ============================================================================
# Test scaffolding: fake Anthropic client + minimal ProtocolSpec builder
# ============================================================================


class _FakeContent:
    def __init__(self, text: str):
        self.text = text


class _FakeResponse:
    def __init__(self, text: str):
        self.content = [_FakeContent(text)]


class FakeAnthropic:
    """Minimal stand-in for `anthropic.Anthropic`. Records every
    `.messages.create()` call and returns a pre-set response (or raises
    a pre-set exception) so resolver tests don't need a live API key."""

    def __init__(self, response_text: Optional[str] = None,
                 raise_exc: Optional[BaseException] = None):
        self._response_text = response_text
        self._raise_exc = raise_exc
        self.messages = self  # `client.messages.create(...)` → self.create(...)
        self.create_calls: list[dict] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.create_calls.append(kwargs)
        if self._raise_exc is not None:
            raise self._raise_exc
        return _FakeResponse(self._response_text or "")


def _inst_prov(cite: str = "x") -> Provenance:
    """Minimal instruction-sourced Provenance for fixture construction."""
    return InstructionProvenance(
        source="instruction",
        cited_text=[cite],
        review_status="original",
        confidence=1.0,
    )


def _make_min_spec(description: str = "tube rack") -> ProtocolSpec:
    """Build a one-step ProtocolSpec with a single source LocationRef
    using `description`. Just enough structure for the resolver to
    collect one unique description and exercise the prompt path."""
    comp = CompositionProvenance(
        step_cited_text="Add 2uL sample to A1",
        parameters_cited_texts=["Add 2uL sample to A1"],
        parameters_reasoning="t",
        grounding=["instruction"],
        confidence=1.0,
    )
    step = ExtractedStep(
        order=1,
        action="transfer",
        composition_provenance=comp,
        source=LocationRef(
            description=description,
            well="A1",
            description_provenance=_inst_prov("A1"),
            wells_provenance=_inst_prov("A1"),
        ),
        destination=LocationRef(
            description="plate",
            well="B1",
            description_provenance=_inst_prov("B1"),
            wells_provenance=_inst_prov("B1"),
        ),
    )
    return ProtocolSpec(summary="t", steps=[step])


_DEFAULT_CONFIG = {
    "labware": {
        "reagent_rack": {
            "load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
            "slot": "2",
        },
        "wellplate_96": {
            "load_name": "corning_96_wellplate_360ul_flat",
            "slot": "5",
        },
    }
}


# ============================================================================
# _parse_assignment — normalize new and legacy LLM shapes
# ============================================================================


class TestParseAssignment_NewShape:
    """LLM emits `{"label": ..., "reasoning": ...}` per description."""

    def test_label_and_reasoning_extracted(self):
        result = LabwareMatcher._parse_assignment(
            {"label": "reagent_rack", "reasoning": "Only tuberack in config."}
        )
        assert result == ("reagent_rack", "Only tuberack in config.")

    def test_null_label_in_new_shape(self):
        result = LabwareMatcher._parse_assignment(
            {"label": None, "reasoning": "No tuberack-like labware exists."}
        )
        # label None is propagated; caller filters it out, but reasoning
        # is still parsed for completeness.
        assert result == (None, "No tuberack-like labware exists.")

    def test_missing_reasoning_field(self):
        # New shape without `reasoning` key → reasoning is None.
        result = LabwareMatcher._parse_assignment({"label": "reagent_rack"})
        assert result == ("reagent_rack", None)

    def test_missing_label_field(self):
        result = LabwareMatcher._parse_assignment({"reasoning": "x"})
        assert result == (None, "x")


class TestParseAssignment_LegacyShape:
    """LLM emits a bare string label (pre-refactor shape)."""

    def test_bare_string(self):
        result = LabwareMatcher._parse_assignment("reagent_rack")
        # Reasoning is None — `_positive_reasoning` will fall back honestly.
        assert result == ("reagent_rack", None)

    def test_empty_string(self):
        result = LabwareMatcher._parse_assignment("")
        # Empty / whitespace-only labels collapse to None at parse
        # time (CodeRabbit P1 strictening) so they never reach a
        # downstream `config[""]` lookup. Pre-fix this returned
        # ("", None); see also `TestParseAssignment_TypeGuards`.
        assert result == (None, None)


class TestParseAssignment_OtherShapes:
    """Null and unexpected types both degrade to (None, None)."""

    def test_none(self):
        assert LabwareMatcher._parse_assignment(None) == (None, None)

    def test_int(self):
        # Defensive: an LLM hallucinating an integer instead of a label
        # shouldn't crash the resolver.
        assert LabwareMatcher._parse_assignment(42) == (None, None)

    def test_list(self):
        assert LabwareMatcher._parse_assignment(["reagent_rack"]) == (None, None)


class TestParseAssignment_TypeGuards:
    """CodeRabbit P1: an LLM may return label / reasoning as a non-string
    inside the new {label, reasoning} dict shape. Without guards, the
    downstream `label in valid_labels` check (unhashable types like list/
    dict) or `reasoning.strip()` (no .strip on int) crashes the entire
    parse pass, dropping ALL descriptions to the empty fallback."""

    def test_non_string_label_in_dict_becomes_none(self):
        # Label as list → not a real config label; treat as missing.
        result = LabwareMatcher._parse_assignment(
            {"label": ["reagent_rack"], "reasoning": "ok"}
        )
        assert result == (None, "ok")

    def test_non_string_reasoning_in_dict_becomes_none(self):
        # Reasoning as int → no .strip(); treat as missing reasoning
        # (label still parses if it's a string).
        result = LabwareMatcher._parse_assignment(
            {"label": "reagent_rack", "reasoning": 42}
        )
        assert result == ("reagent_rack", None)

    def test_empty_string_label_becomes_none(self):
        # Whitespace-only / empty label is not a real label.
        result = LabwareMatcher._parse_assignment(
            {"label": "   ", "reasoning": "ok"}
        )
        assert result == (None, "ok")

    def test_legacy_string_shape_strips_whitespace(self):
        # Existing bare-string callers get whitespace normalization for free.
        result = LabwareMatcher._parse_assignment("  reagent_rack  ")
        assert result == ("reagent_rack", None)

    def test_legacy_empty_string_becomes_none(self):
        # Pre-fix: ("", None). Post-fix: (None, None) — empty string
        # isn't a real label, treating as missing prevents it slipping
        # through to a config[""] lookup downstream.
        assert LabwareMatcher._parse_assignment("") == (None, None)
        assert LabwareMatcher._parse_assignment("   ") == (None, None)


# ============================================================================
# _positive_reasoning — real-reasoning branch vs honest-fallback branch
# ============================================================================


class TestPositiveReasoning_WithLLMReasoning:
    """When the LLM supplied reasoning, the output should preface it
    with the description→label mapping so the user sees both the
    structural context and the concrete signal."""

    def test_includes_mapping_prefix(self):
        r = LabwareMatcher(config=_DEFAULT_CONFIG)
        out = r._positive_reasoning(
            description="tube rack",
            label="reagent_rack",
            reasoning="Only tuberack in config; tiprack_20/300 are tip racks.",
        )
        assert "'tube rack'" in out
        assert "'reagent_rack'" in out
        assert "Only tuberack in config" in out

    def test_load_name_never_in_output(self):
        # CARRY-C1: load_name is always omitted from the modal text,
        # regardless of whether the config carries one. The full
        # Opentrons identifier (50+ chars) was noise for reviewers;
        # it lives on in config["labware"][label] for audit purposes.
        r = LabwareMatcher(config=_DEFAULT_CONFIG)
        out = r._positive_reasoning(
            description="tube rack",
            label="reagent_rack",
            reasoning="Single candidate.",
        )
        assert "load_name" not in out
        assert "opentrons_24_tuberack" not in out
        assert "'tube rack'" in out and "'reagent_rack'" in out

    def test_strips_surrounding_whitespace_in_reasoning(self):
        r = LabwareMatcher(config=_DEFAULT_CONFIG)
        out = r._positive_reasoning(
            description="tube rack",
            label="reagent_rack",
            reasoning="   Trimmed.   ",
        )
        assert "Trimmed." in out
        # No double-space artifact from concatenation.
        assert "  Trimmed" not in out


class TestPositiveReasoning_FallbackBranch:
    """When the LLM did not supply reasoning, the output is an honest
    fallback — explicitly admits no reasoning was surfaced — NOT the
    pre-fix template that falsely claimed reasoning based on context."""

    def test_fallback_used_when_reasoning_none(self):
        r = LabwareMatcher(config=_DEFAULT_CONFIG)
        out = r._positive_reasoning(
            description="tube rack",
            label="reagent_rack",
            reasoning=None,
        )
        assert "Reasoning was not surfaced" in out
        # Mapping prefix still present.
        assert "'tube rack'" in out and "'reagent_rack'" in out

    def test_fallback_used_when_reasoning_blank(self):
        r = LabwareMatcher(config=_DEFAULT_CONFIG)
        out = r._positive_reasoning(
            description="tube rack",
            label="reagent_rack",
            reasoning="   ",
        )
        assert "Reasoning was not surfaced" in out

    def test_fallback_does_not_contain_old_template_phrase(self):
        # Regression guard: the old "based on description text + step
        # usage context" template was the exact phrase the user
        # flagged. Make sure neither branch emits it.
        r = LabwareMatcher(config=_DEFAULT_CONFIG)
        out_with = r._positive_reasoning(
            description="tube rack", label="reagent_rack",
            reasoning="Real reason.",
        )
        out_without = r._positive_reasoning(
            description="tube rack", label="reagent_rack", reasoning=None,
        )
        for out in (out_with, out_without):
            assert "based on description text" not in out
            assert "step usage context" not in out


# ============================================================================
# _llm_resolve — full LLM call path with the fake client
# ============================================================================


class TestLLMResolve:
    """End-to-end resolver path: prompt → fake LLM → parsed dict."""

    def test_new_shape_round_trip(self):
        # LLM returns the new {label, reasoning} shape.
        response = json.dumps({
            "assignments": {
                "tube rack": {
                    "label": "reagent_rack",
                    "reasoning": "Only tuberack-typed labware in the config.",
                },
                "plate": {
                    "label": "wellplate_96",
                    "reasoning": "Only 96-well container in config.",
                },
            }
        })
        client = FakeAnthropic(response_text=response)
        resolver = LabwareMatcher(config=_DEFAULT_CONFIG, client=client)
        spec = _make_min_spec("tube rack")

        resolved = resolver._llm_resolve(["tube rack", "plate"], spec)
        assert resolved["tube rack"] == ("reagent_rack",
                                           "Only tuberack-typed labware in the config.")
        assert resolved["plate"] == ("wellplate_96",
                                       "Only 96-well container in config.")

    def test_legacy_shape_round_trip(self):
        # LLM emits the old string-only shape — resolver should still
        # accept it but reasoning is None (falls back honestly later).
        response = json.dumps({
            "assignments": {"tube rack": "reagent_rack"}
        })
        client = FakeAnthropic(response_text=response)
        resolver = LabwareMatcher(config=_DEFAULT_CONFIG, client=client)
        spec = _make_min_spec("tube rack")

        resolved = resolver._llm_resolve(["tube rack"], spec)
        assert resolved["tube rack"] == ("reagent_rack", None)

    def test_null_label_filtered_out(self):
        # LLM says "no good match" — desc should be ABSENT from the
        # returned dict so the caller treats it as unresolvable.
        response = json.dumps({
            "assignments": {
                "tube rack": {"label": None, "reasoning": "No tuberack in config."}
            }
        })
        client = FakeAnthropic(response_text=response)
        resolver = LabwareMatcher(config=_DEFAULT_CONFIG, client=client)
        spec = _make_min_spec("tube rack")

        resolved = resolver._llm_resolve(["tube rack"], spec)
        assert "tube rack" not in resolved

    def test_unknown_label_filtered_out(self):
        # LLM hallucinates a label that doesn't exist in config — must
        # be filtered out (existing safety behavior, preserved).
        response = json.dumps({
            "assignments": {
                "tube rack": {"label": "fake_rack", "reasoning": "x"}
            }
        })
        client = FakeAnthropic(response_text=response)
        resolver = LabwareMatcher(config=_DEFAULT_CONFIG, client=client)
        spec = _make_min_spec("tube rack")

        resolved = resolver._llm_resolve(["tube rack"], spec)
        assert "tube rack" not in resolved

    def test_client_exception_returns_empty(self):
        # Network error or malformed response → empty dict (existing
        # exception-swallow path, preserved).
        client = FakeAnthropic(raise_exc=RuntimeError("network down"))
        resolver = LabwareMatcher(config=_DEFAULT_CONFIG, client=client)
        spec = _make_min_spec("tube rack")

        resolved = resolver._llm_resolve(["tube rack"], spec)
        assert resolved == {}

    def test_no_client_returns_empty(self):
        # Without a client (test-fake path), returns empty dict and
        # makes no calls.
        resolver = LabwareMatcher(config=_DEFAULT_CONFIG, client=None)
        spec = _make_min_spec("tube rack")

        resolved = resolver._llm_resolve(["tube rack"], spec)
        assert resolved == {}


# ============================================================================
# suggest() integration — LabwareSuggestion produced for each description
# ============================================================================


class TestSuggestIntegration:
    """`suggest()` builds a LabwareMatchSuggestion per unique description.

    These exercise the LLM branch (≥2 candidates after capability + type
    filters). The deterministic branch is exercised by
    `TestSuggestCapabilityBranching` below. Specs use the bare
    description "rack" so the type filter is None (no cue) and every
    config tuberack survives — forcing the LLM-disambiguation path.
    """

    # Two-tuberack config: bare "rack" description leaves both as
    # survivors after capability + type filters, triggering the llm branch.
    _TWO_TUBERACK_CONFIG = {
        "labware": {
            "reagent_rack": {
                "load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
                "slot": "2",
            },
            "spare_rack": {
                "load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
                "slot": "3",
            },
        }
    }

    def _ambiguous_spec(self) -> ProtocolSpec:
        comp = CompositionProvenance(
            step_cited_text="Add 2uL sample to A1",
            parameters_cited_texts=["Add 2uL sample to A1"],
            parameters_reasoning="t",
            grounding=["instruction"],
            confidence=1.0,
        )
        step = ExtractedStep(
            order=1, action="transfer", composition_provenance=comp,
            source=LocationRef(
                description="rack", well="A1",
                description_provenance=_inst_prov("A1"),
                wells_provenance=_inst_prov("A1"),
            ),
        )
        return ProtocolSpec(summary="t", steps=[step])

    def test_llm_reasoning_threaded_into_suggestion(self):
        response = json.dumps({
            "assignments": {
                "rack": {
                    "label": "reagent_rack",
                    "reasoning": "Step 1 source role matches reagent rack convention.",
                },
            }
        })
        client = FakeAnthropic(response_text=response)
        resolver = LabwareMatcher(config=self._TWO_TUBERACK_CONFIG, client=client)
        sug = resolver.suggest(self._ambiguous_spec())["rack"]
        assert sug.suggested_label == "reagent_rack"
        assert "Step 1 source role matches reagent rack convention." in sug.positive_reasoning
        assert "'rack'" in sug.positive_reasoning
        assert "opentrons_24_tuberack" not in sug.positive_reasoning
        assert "load_name" not in sug.positive_reasoning

    def test_legacy_shape_yields_fallback_reasoning(self):
        response = json.dumps({
            "assignments": {"rack": "reagent_rack"},
        })
        client = FakeAnthropic(response_text=response)
        resolver = LabwareMatcher(config=self._TWO_TUBERACK_CONFIG, client=client)
        sug = resolver.suggest(self._ambiguous_spec())["rack"]
        assert sug.suggested_label == "reagent_rack"
        assert "Reasoning was not surfaced" in sug.positive_reasoning
        assert "based on description text" not in sug.positive_reasoning

    def test_null_label_yields_unresolvable_suggestion(self):
        response = json.dumps({
            "assignments": {
                "rack": {"label": None,
                          "reasoning": "Insufficient signal to disambiguate."},
            }
        })
        client = FakeAnthropic(response_text=response)
        resolver = LabwareMatcher(config=self._TWO_TUBERACK_CONFIG, client=client)
        sug = resolver.suggest(self._ambiguous_spec())["rack"]
        assert sug.suggested_label is None
        assert sug.positive_reasoning is None
        assert sug.confidence == 0.0
        # Candidates are now narrowed survivors (not the full config),
        # so the dropdown shows only physically valid choices.
        assert "reagent_rack" in sug.candidates
        assert "spare_rack" in sug.candidates

    def test_prompt_includes_narrowed_candidates(self):
        # Verifies the new narrowed-candidate prompt shape: each
        # description gets a pre-filtered candidate list rather than
        # the full config.
        response = json.dumps({"assignments": {}})
        client = FakeAnthropic(response_text=response)
        resolver = LabwareMatcher(config=self._TWO_TUBERACK_CONFIG, client=client)
        resolver.suggest(self._ambiguous_spec())
        assert len(client.create_calls) == 1
        prompt = client.create_calls[0]["messages"][0]["content"]
        assert '"label"' in prompt
        assert '"reasoning"' in prompt
        assert "candidates" in prompt
        # The new prompt requires the LLM to pick FROM the listed
        # candidates — that constraint must be spelled out.
        assert "MUST be one of the listed candidates" in prompt


# ============================================================================
# Capability-based matcher helpers (Phase 1+ contract stubs)
# ============================================================================
# Tests assert the FINAL behavior. Until the helpers are implemented,
# they raise NotImplementedError so the tests fail with that signal —
# clear marker of which contracts are still pending implementation.


# Shared fixture: a config with three labware spanning two categories +
# two capacities. Used by capability-filter and type-filter tests so the
# fit/drop decisions have something to bite on.
_THREE_LABWARE_CONFIG = {
    "labware": {
        "tiprack_20": {
            "load_name": "opentrons_96_tiprack_20ul",
            "slot": "1",
        },
        "sample_rack": {
            "load_name": "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap",
            "slot": "2",
        },
        "wellplate_96": {
            "load_name": "corning_96_wellplate_360ul_flat",
            "slot": "5",
        },
    }
}


class TestLoadNameCategory:
    """Closed-set extraction of the category token from Opentrons load_names.

    Pattern: `<vendor>_<count>_<type>_<details...>`. The third token is the
    category. Closed set:
    {tiprack, tuberack, wellplate, reservoir, aluminumblock}. Anything
    else returns None (treated as unknown by the filter pipeline)."""

    def test_tuberack_extracted(self):
        assert _load_name_category(
            "opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap"
        ) == "tuberack"

    def test_tiprack_extracted(self):
        assert _load_name_category("opentrons_96_tiprack_20ul") == "tiprack"

    def test_wellplate_extracted(self):
        assert _load_name_category("corning_96_wellplate_360ul_flat") == "wellplate"

    def test_aluminumblock_extracted(self):
        assert _load_name_category(
            "opentrons_24_aluminumblock_nest_1.5ml_snapcap"
        ) == "aluminumblock"

    def test_unknown_third_token_returns_none(self):
        # Made-up category, well-formed shape but not in closed set.
        assert _load_name_category("opentrons_24_widget_foo") is None

    def test_shape_too_short_returns_none(self):
        assert _load_name_category("opentrons_24") is None

    def test_empty_returns_none(self):
        assert _load_name_category("") is None


class TestDescriptionCategoryFilter:
    """Maps user wording to allowed-category sets. `None` means
    "no category cue — don't filter by type"."""

    def test_tube_rack_allows_tuberack_and_aluminumblock(self):
        result = _description_category_filter("tube rack")
        assert result == {"tuberack", "aluminumblock"}

    def test_eppendorf_alone_allows_tuberack_and_aluminumblock(self):
        result = _description_category_filter("Eppendorf 1.5ml")
        assert result == {"tuberack", "aluminumblock"}

    def test_tipbox_keeps_only_tiprack(self):
        assert _description_category_filter("tipbox") == {"tiprack"}

    def test_96_well_plate_keeps_only_wellplate(self):
        assert _description_category_filter("96-well plate") == {"wellplate"}

    def test_reservoir_keeps_only_reservoir(self):
        assert _description_category_filter("reservoir") == {"reservoir"}

    def test_bare_rack_returns_none(self):
        # No qualifier — could mean tube or tip — don't filter.
        assert _description_category_filter("rack") is None

    def test_no_cue_returns_none(self):
        # Description with no category keyword at all.
        assert _description_category_filter("widget") is None


class TestCollectDescriptionsWithWells:
    """Aggregates wells per description across the whole spec. Order
    preserves first-seen; wells union across all occurrences."""

    def _comp(self):
        return CompositionProvenance(
            step_cited_text="x", parameters_cited_texts=["x"],
            parameters_reasoning="t", grounding=["instruction"], confidence=1.0,
        )

    def test_empty_spec_returns_empty(self):
        # Pause-only step contributes no descriptions.
        spec = ProtocolSpec(summary="t", steps=[
            ExtractedStep(order=1, action="pause", note="hold",
                          composition_provenance=self._comp()),
        ])
        matcher = LabwareMatcher(config=_DEFAULT_CONFIG, client=None)
        assert matcher._collect_descriptions_with_wells(spec) == {}

    def test_single_well_collected(self):
        # _make_min_spec puts source desc=description, dest desc="plate",
        # both with single wells (A1, B1).
        spec = _make_min_spec("tube rack")
        matcher = LabwareMatcher(config=_DEFAULT_CONFIG, client=None)
        result = matcher._collect_descriptions_with_wells(spec)
        assert list(result.keys()) == ["tube rack", "plate"]
        assert result["tube rack"]["wells"] == {"A1"}
        assert result["plate"]["wells"] == {"B1"}

    def test_well_range_expands_via_helper(self):
        step = ExtractedStep(
            order=1, action="transfer", composition_provenance=self._comp(),
            source=LocationRef(
                description="tube rack", well_range="A1-A6",
                description_provenance=_inst_prov("A1-A6"),
                wells_provenance=_inst_prov("A1-A6"),
            ),
        )
        spec = ProtocolSpec(summary="t", steps=[step])
        matcher = LabwareMatcher(config=_DEFAULT_CONFIG, client=None)
        result = matcher._collect_descriptions_with_wells(spec)
        assert result["tube rack"]["wells"] == {"A1", "A2", "A3", "A4", "A5", "A6"}

    def test_two_steps_same_description_merge_wells(self):
        # Same description on two steps — wells union, contexts list has both.
        steps = [
            ExtractedStep(order=1, action="transfer",
                          composition_provenance=self._comp(),
                          source=LocationRef(description="tube rack", well="A1",
                                             description_provenance=_inst_prov("A1"),
                                             wells_provenance=_inst_prov("A1"))),
            ExtractedStep(order=2, action="transfer",
                          composition_provenance=self._comp(),
                          source=LocationRef(description="tube rack", well="B2",
                                             description_provenance=_inst_prov("B2"),
                                             wells_provenance=_inst_prov("B2"))),
        ]
        spec = ProtocolSpec(summary="t", steps=steps)
        matcher = LabwareMatcher(config=_DEFAULT_CONFIG, client=None)
        result = matcher._collect_descriptions_with_wells(spec)
        assert result["tube rack"]["wells"] == {"A1", "B2"}
        assert len(result["tube rack"]["contexts"]) == 2

    def test_initial_contents_description_appears(self):
        step = ExtractedStep(
            order=1, action="pause", note="hold",
            composition_provenance=self._comp(),
        )
        ic = WellContents(labware="reagent rack", well="C3", substance="buffer")
        spec = ProtocolSpec(summary="t", steps=[step], initial_contents=[ic])
        matcher = LabwareMatcher(config=_DEFAULT_CONFIG, client=None)
        result = matcher._collect_descriptions_with_wells(spec)
        assert "reagent rack" in result
        assert result["reagent rack"]["wells"] == {"C3"}


class TestCapabilityFilter:
    """Drops candidates whose valid_wells doesn't superset the referenced
    wells. Fail-open on unknown load_names."""

    def test_all_wells_fit_both_candidates_kept(self):
        # A1 fits every labware in _THREE_LABWARE_CONFIG.
        result = _capability_filter({"A1"}, _THREE_LABWARE_CONFIG)
        assert set(result) == {"tiprack_20", "sample_rack", "wellplate_96"}

    def test_c7_drops_24_rack_keeps_96_layouts(self):
        # 24-rack is 4x6 — no column 7. C7 must drop it.
        # tiprack_20 (96-well, 8x12) and wellplate_96 (96-well) still fit.
        result = _capability_filter({"A1", "B2", "C7"}, _THREE_LABWARE_CONFIG)
        assert "sample_rack" not in result
        assert set(result) == {"tiprack_20", "wellplate_96"}

    def test_empty_wells_keeps_all_candidates(self):
        # No constraint at all → every labware survives.
        result = _capability_filter(set(), _THREE_LABWARE_CONFIG)
        assert set(result) == {"tiprack_20", "sample_rack", "wellplate_96"}

    def test_empty_config_returns_empty(self):
        assert _capability_filter({"A1"}, {"labware": {}}) == []

    def test_unknown_load_name_fails_open_and_keeps_candidate(self):
        # Labware with a load_name get_well_info doesn't recognize must
        # NOT be dropped — mirrors constraints.py:553-559 convention.
        cfg = {"labware": {
            "weird_box": {"load_name": "made_up_box_format", "slot": "3"},
        }}
        assert _capability_filter({"A1"}, cfg) == ["weird_box"]


class TestTypeFilter:
    """Filters by load_name category. `None`-filter (bare description)
    passes candidates through unchanged."""

    def test_tube_description_drops_tipracks_and_wellplates(self):
        # "tube rack" → allowed {tuberack, aluminumblock}. Out of the
        # three-labware fixture, only sample_rack qualifies.
        result = _type_filter("tube rack",
                               ["tiprack_20", "sample_rack", "wellplate_96"],
                               _THREE_LABWARE_CONFIG)
        assert result == ["sample_rack"]

    def test_bare_rack_passes_candidates_through(self):
        # No category cue → filter returns input unchanged.
        result = _type_filter("rack",
                               ["tiprack_20", "sample_rack"],
                               _THREE_LABWARE_CONFIG)
        assert result == ["tiprack_20", "sample_rack"]

    def test_no_rule_match_passes_through(self):
        # "widget" has no category cue → no filter.
        result = _type_filter("widget", ["tiprack_20"], _THREE_LABWARE_CONFIG)
        assert result == ["tiprack_20"]

    def test_all_filtered_returns_empty(self):
        # "tube rack" against tiprack-only candidate list → nothing survives.
        result = _type_filter("tube rack", ["tiprack_20"], _THREE_LABWARE_CONFIG)
        assert result == []


class TestResolveOne:
    """Composes capability + type + namespace-split detection to pick
    a branch for one description. Returns (branch, survivors)."""

    def test_single_survivor_returns_deterministic(self):
        # "plate" + wells {A1, B1}: capability keeps all 3 candidates;
        # type filter keeps only wellplate_96 → deterministic.
        matcher = LabwareMatcher(config=_THREE_LABWARE_CONFIG, client=None)
        branch, survivors = matcher._resolve_one("plate", {"A1", "B1"})
        assert branch == "deterministic"
        assert survivors == ["wellplate_96"]

    def test_multi_survivor_returns_llm(self):
        # "rack" (no type cue) + wells {A1}: capability keeps all;
        # type filter no-ops → 3 survivors → llm.
        matcher = LabwareMatcher(config=_THREE_LABWARE_CONFIG, client=None)
        branch, survivors = matcher._resolve_one("rack", {"A1"})
        assert branch == "llm"
        assert len(survivors) >= 2

    def test_zero_survivors_with_namespace_pattern_returns_namespace_split(
        self, monkeypatch,
    ):
        # Real Opentrons labware is always-A-rooted rectangles, which
        # makes "aggregate fits nothing + each subgroup fits something"
        # impossible to construct (a labware big enough to host one
        # subgroup hosts every smaller one too). Monkey-patch
        # get_well_info to return row-disjoint shapes so the strict
        # algorithm's positive path can be exercised.
        shapes = {
            "opentrons_8_tuberack_top_thing":
                ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"],
            "opentrons_8_tuberack_bot_thing":
                ["C1", "C2", "C3", "C4", "D1", "D2", "D3", "D4"],
        }

        def fake_well_info(load_name):
            if load_name not in shapes:
                raise ValueError(f"unknown {load_name}")
            return {"valid_wells": shapes[load_name]}

        monkeypatch.setattr(
            "nl2protocol.models.labware.get_well_info", fake_well_info,
        )
        cfg = {
            "labware": {
                "top": {"load_name": "opentrons_8_tuberack_top_thing", "slot": "1"},
                "bot": {"load_name": "opentrons_8_tuberack_bot_thing", "slot": "2"},
            }
        }
        matcher = LabwareMatcher(config=cfg, client=None)
        # Aggregate {A1, B1, C1, D1}: top has A/B but not C/D; bot has
        # C/D but not A/B. Neither single labware fits all four.
        # Subgroups: A→top, B→top, C→bot, D→bot. All fit something.
        wells = {"A1", "B1", "C1", "D1"}
        branch, survivors = matcher._resolve_one("tube rack", wells)
        assert branch == "namespace_split"
        assert survivors == []

    def test_zero_survivors_no_pattern_returns_unresolvable(self):
        # Wells with no letter-prefix pattern (Z99 alone) and no candidate
        # fits → unresolvable.
        matcher = LabwareMatcher(config=_THREE_LABWARE_CONFIG, client=None)
        branch, survivors = matcher._resolve_one("widget", {"Z99"})
        assert branch == "unresolvable"
        assert survivors == []


class TestHasNamespaceSplit:
    """Module-level helper used by `_resolve_one` and by
    `NamespaceSplitDetector`. Returns dict on clean partition, None
    otherwise."""

    def test_clean_partition_detected_with_row_disjoint_shapes(
        self, monkeypatch,
    ):
        # See TestResolveOne::namespace_split for why monkey-patching
        # is needed (real Opentrons labware can't be row-disjoint).
        shapes = {
            "opentrons_8_tuberack_top_thing":
                ["A1", "A2", "B1", "B2"],
            "opentrons_8_tuberack_bot_thing":
                ["C1", "C2", "D1", "D2"],
        }

        def fake_well_info(load_name):
            if load_name not in shapes:
                raise ValueError(f"unknown {load_name}")
            return {"valid_wells": shapes[load_name]}

        monkeypatch.setattr(
            "nl2protocol.models.labware.get_well_info", fake_well_info,
        )
        cfg = {
            "labware": {
                "top": {"load_name": "opentrons_8_tuberack_top_thing", "slot": "1"},
                "bot": {"load_name": "opentrons_8_tuberack_bot_thing", "slot": "2"},
            }
        }
        wells = {"A1", "B1", "C1", "D1"}
        result = _has_namespace_split("tube rack", wells, cfg)
        assert result is not None
        assert set(result["partition"].keys()) == {"A", "B", "C", "D"}
        pair_dict = dict(result["candidate_pairs"])
        assert pair_dict["A"] == "top"
        assert pair_dict["B"] == "top"
        assert pair_dict["C"] == "bot"
        assert pair_dict["D"] == "bot"

    def test_single_prefix_returns_none(self):
        # Only A-prefixes — not a multi-rack pattern.
        result = _has_namespace_split("tube rack",
                                       {"A1", "A2", "A3"},
                                       _THREE_LABWARE_CONFIG)
        assert result is None

    def test_partition_without_fitting_labware_returns_none(self):
        # Two prefixes but config has only a tiprack — type filter drops
        # every candidate per subgroup → no partition.
        cfg = {
            "labware": {
                "tiprack_20": {"load_name": "opentrons_96_tiprack_20ul", "slot": "1"},
            }
        }
        assert _has_namespace_split("tube rack", {"A1", "B1"}, cfg) is None
