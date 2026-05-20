"""Tests for nl2protocol/extraction/resolver.py — labware resolver.

Covers (a) `_parse_assignment` (new-shape vs legacy-shape unpacking),
(b) `_positive_reasoning` (LLM-reasoning vs honest-fallback branches),
(c) `_llm_resolve` end-to-end with a fake Anthropic client,
(d) `suggest` integration on a minimal ProtocolSpec.
"""

import json
from typing import Any, Optional

from nl2protocol.extraction.resolver import LabwareResolver, LabwareSuggestion
from nl2protocol.models.spec import (
    CompositionProvenance,
    ExtractedStep,
    LocationRef,
    Provenance,
    ProtocolSpec,
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
    return Provenance(
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
        result = LabwareResolver._parse_assignment(
            {"label": "reagent_rack", "reasoning": "Only tuberack in config."}
        )
        assert result == ("reagent_rack", "Only tuberack in config.")

    def test_null_label_in_new_shape(self):
        result = LabwareResolver._parse_assignment(
            {"label": None, "reasoning": "No tuberack-like labware exists."}
        )
        # label None is propagated; caller filters it out, but reasoning
        # is still parsed for completeness.
        assert result == (None, "No tuberack-like labware exists.")

    def test_missing_reasoning_field(self):
        # New shape without `reasoning` key → reasoning is None.
        result = LabwareResolver._parse_assignment({"label": "reagent_rack"})
        assert result == ("reagent_rack", None)

    def test_missing_label_field(self):
        result = LabwareResolver._parse_assignment({"reasoning": "x"})
        assert result == (None, "x")


class TestParseAssignment_LegacyShape:
    """LLM emits a bare string label (pre-refactor shape)."""

    def test_bare_string(self):
        result = LabwareResolver._parse_assignment("reagent_rack")
        # Reasoning is None — `_positive_reasoning` will fall back honestly.
        assert result == ("reagent_rack", None)

    def test_empty_string(self):
        result = LabwareResolver._parse_assignment("")
        # Empty / whitespace-only labels collapse to None at parse
        # time (CodeRabbit P1 strictening) so they never reach a
        # downstream `config[""]` lookup. Pre-fix this returned
        # ("", None); see also `TestParseAssignment_TypeGuards`.
        assert result == (None, None)


class TestParseAssignment_OtherShapes:
    """Null and unexpected types both degrade to (None, None)."""

    def test_none(self):
        assert LabwareResolver._parse_assignment(None) == (None, None)

    def test_int(self):
        # Defensive: an LLM hallucinating an integer instead of a label
        # shouldn't crash the resolver.
        assert LabwareResolver._parse_assignment(42) == (None, None)

    def test_list(self):
        assert LabwareResolver._parse_assignment(["reagent_rack"]) == (None, None)


class TestParseAssignment_TypeGuards:
    """CodeRabbit P1: an LLM may return label / reasoning as a non-string
    inside the new {label, reasoning} dict shape. Without guards, the
    downstream `label in valid_labels` check (unhashable types like list/
    dict) or `reasoning.strip()` (no .strip on int) crashes the entire
    parse pass, dropping ALL descriptions to the empty fallback."""

    def test_non_string_label_in_dict_becomes_none(self):
        # Label as list → not a real config label; treat as missing.
        result = LabwareResolver._parse_assignment(
            {"label": ["reagent_rack"], "reasoning": "ok"}
        )
        assert result == (None, "ok")

    def test_non_string_reasoning_in_dict_becomes_none(self):
        # Reasoning as int → no .strip(); treat as missing reasoning
        # (label still parses if it's a string).
        result = LabwareResolver._parse_assignment(
            {"label": "reagent_rack", "reasoning": 42}
        )
        assert result == ("reagent_rack", None)

    def test_empty_string_label_becomes_none(self):
        # Whitespace-only / empty label is not a real label.
        result = LabwareResolver._parse_assignment(
            {"label": "   ", "reasoning": "ok"}
        )
        assert result == (None, "ok")

    def test_legacy_string_shape_strips_whitespace(self):
        # Existing bare-string callers get whitespace normalization for free.
        result = LabwareResolver._parse_assignment("  reagent_rack  ")
        assert result == ("reagent_rack", None)

    def test_legacy_empty_string_becomes_none(self):
        # Pre-fix: ("", None). Post-fix: (None, None) — empty string
        # isn't a real label, treating as missing prevents it slipping
        # through to a config[""] lookup downstream.
        assert LabwareResolver._parse_assignment("") == (None, None)
        assert LabwareResolver._parse_assignment("   ") == (None, None)


# ============================================================================
# _positive_reasoning — real-reasoning branch vs honest-fallback branch
# ============================================================================


class TestPositiveReasoning_WithLLMReasoning:
    """When the LLM supplied reasoning, the output should preface it
    with the description→label mapping (including load_name) so the
    user sees both the structural context and the concrete signal."""

    def test_includes_mapping_prefix(self):
        r = LabwareResolver(config=_DEFAULT_CONFIG)
        out = r._positive_reasoning(
            description="tube rack",
            label="reagent_rack",
            reasoning="Only tuberack in config; tiprack_20/300 are tip racks.",
        )
        assert "'tube rack'" in out
        assert "'reagent_rack'" in out
        assert "opentrons_24_tuberack" in out
        assert "Only tuberack in config" in out

    def test_load_name_omitted_when_missing(self):
        r = LabwareResolver(config={"labware": {"foo": {}}})
        out = r._positive_reasoning(
            description="thing",
            label="foo",
            reasoning="Single candidate.",
        )
        assert "load_name" not in out
        assert "'thing'" in out and "'foo'" in out
        assert "Single candidate." in out

    def test_strips_surrounding_whitespace_in_reasoning(self):
        r = LabwareResolver(config=_DEFAULT_CONFIG)
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
        r = LabwareResolver(config=_DEFAULT_CONFIG)
        out = r._positive_reasoning(
            description="tube rack",
            label="reagent_rack",
            reasoning=None,
        )
        assert "Reasoning was not surfaced" in out
        # Mapping prefix still present.
        assert "'tube rack'" in out and "'reagent_rack'" in out

    def test_fallback_used_when_reasoning_blank(self):
        r = LabwareResolver(config=_DEFAULT_CONFIG)
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
        r = LabwareResolver(config=_DEFAULT_CONFIG)
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
        resolver = LabwareResolver(config=_DEFAULT_CONFIG, client=client)
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
        resolver = LabwareResolver(config=_DEFAULT_CONFIG, client=client)
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
        resolver = LabwareResolver(config=_DEFAULT_CONFIG, client=client)
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
        resolver = LabwareResolver(config=_DEFAULT_CONFIG, client=client)
        spec = _make_min_spec("tube rack")

        resolved = resolver._llm_resolve(["tube rack"], spec)
        assert "tube rack" not in resolved

    def test_client_exception_returns_empty(self):
        # Network error or malformed response → empty dict (existing
        # exception-swallow path, preserved).
        client = FakeAnthropic(raise_exc=RuntimeError("network down"))
        resolver = LabwareResolver(config=_DEFAULT_CONFIG, client=client)
        spec = _make_min_spec("tube rack")

        resolved = resolver._llm_resolve(["tube rack"], spec)
        assert resolved == {}

    def test_no_client_returns_empty(self):
        # Without a client (test-fake path), returns empty dict and
        # makes no calls.
        resolver = LabwareResolver(config=_DEFAULT_CONFIG, client=None)
        spec = _make_min_spec("tube rack")

        resolved = resolver._llm_resolve(["tube rack"], spec)
        assert resolved == {}


# ============================================================================
# suggest() integration — LabwareSuggestion produced for each description
# ============================================================================


class TestSuggestIntegration:
    """`suggest()` builds a LabwareSuggestion per unique description,
    threading the LLM's reasoning into `positive_reasoning`."""

    def test_llm_reasoning_threaded_into_suggestion(self):
        response = json.dumps({
            "assignments": {
                "tube rack": {
                    "label": "reagent_rack",
                    "reasoning": "Only tuberack-typed labware in config.",
                },
                "plate": {
                    "label": "wellplate_96",
                    "reasoning": "Only 96-well container in config.",
                },
            }
        })
        client = FakeAnthropic(response_text=response)
        resolver = LabwareResolver(config=_DEFAULT_CONFIG, client=client)
        spec = _make_min_spec("tube rack")

        suggestions = resolver.suggest(spec)
        sug = suggestions["tube rack"]
        assert sug.suggested_label == "reagent_rack"
        assert "Only tuberack-typed labware in config." in sug.positive_reasoning
        # Mapping prefix retained.
        assert "'tube rack'" in sug.positive_reasoning
        assert "opentrons_24_tuberack" in sug.positive_reasoning

    def test_legacy_shape_yields_fallback_reasoning(self):
        response = json.dumps({
            "assignments": {
                "tube rack": "reagent_rack",
                "plate": "wellplate_96",
            }
        })
        client = FakeAnthropic(response_text=response)
        resolver = LabwareResolver(config=_DEFAULT_CONFIG, client=client)
        spec = _make_min_spec("tube rack")

        suggestions = resolver.suggest(spec)
        sug = suggestions["tube rack"]
        # Label still resolved.
        assert sug.suggested_label == "reagent_rack"
        # Reasoning is the honest fallback, not the old template.
        assert "Reasoning was not surfaced" in sug.positive_reasoning
        assert "based on description text" not in sug.positive_reasoning

    def test_null_label_yields_unresolvable_suggestion(self):
        response = json.dumps({
            "assignments": {
                "tube rack": {"label": None,
                                "reasoning": "No tuberack-like labware in config."},
                "plate": {"label": "wellplate_96",
                            "reasoning": "Only 96-well container in config."},
            }
        })
        client = FakeAnthropic(response_text=response)
        resolver = LabwareResolver(config=_DEFAULT_CONFIG, client=client)
        spec = _make_min_spec("tube rack")

        suggestions = resolver.suggest(spec)
        sug = suggestions["tube rack"]
        assert sug.suggested_label is None
        assert sug.positive_reasoning is None
        assert sug.confidence == 0.0
        # Candidates still surfaced for the dropdown.
        assert "reagent_rack" in sug.candidates
        assert "wellplate_96" in sug.candidates

    def test_prompt_includes_new_shape_instructions(self):
        # Sanity: the prompt actually asks for {label, reasoning}.
        # If someone reverts the prompt to legacy shape without updating
        # the parser, this test catches it.
        response = json.dumps({"assignments": {}})
        client = FakeAnthropic(response_text=response)
        resolver = LabwareResolver(config=_DEFAULT_CONFIG, client=client)
        spec = _make_min_spec("tube rack")

        resolver.suggest(spec)
        assert len(client.create_calls) == 1
        prompt = client.create_calls[0]["messages"][0]["content"]
        # The new prompt names both fields and warns against the
        # specific bad phrasings.
        assert '"label"' in prompt
        assert '"reasoning"' in prompt
        assert "Based on description text and step context" in prompt
        # The user's flagged anti-pattern phrase appears in the
        # "do NOT write these" section.
