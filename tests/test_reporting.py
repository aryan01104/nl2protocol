"""
Contract tests for nl2protocol.reporting.

Covers StageEvent dataclass, ConsoleReporter (silent default), and
CapturingReporter (test sink + base for buffering reporters like HTMLReporter).
"""

from datetime import datetime

import pytest

from nl2protocol.reporting import (
    CapturingReporter,
    ConsoleReporter,
    Reporter,
    StageEvent,
)


# ============================================================================
# StageEvent
# ============================================================================

class TestStageEvent:
    """Contract tests for the StageEvent dataclass."""

    def test_constructs_with_required_fields(self):
        ev = StageEvent(kind="info", data={"message": "hi"})
        assert ev.kind == "info"
        assert ev.data == {"message": "hi"}
        assert ev.stage_name is None
        assert isinstance(ev.timestamp, datetime)

    def test_optional_stage_name_is_stored(self):
        ev = StageEvent(kind="extracted_spec", data={"spec": None}, stage_name="stage_2")
        assert ev.stage_name == "stage_2"

    def test_timestamp_defaults_to_now(self):
        before = datetime.now()
        ev = StageEvent(kind="info", data={})
        after = datetime.now()
        assert before <= ev.timestamp <= after


# ============================================================================
# ConsoleReporter — silent default
# ============================================================================

class TestConsoleReporter:
    """ConsoleReporter is silent on every event by design — CLI output is
    handled by the existing _log/_stage helpers, not by the reporter."""

    @pytest.mark.parametrize("kind", [
        "stage_start", "stage_complete", "stage_failed",
        "raw_instruction", "extracted_spec", "completed_spec",
        "generated_script", "warning", "error", "info",
    ])
    def test_emit_produces_no_output(self, kind, capsys):
        cr = ConsoleReporter()
        cr.emit(StageEvent(kind=kind, data={"message": "anything", "stage": "Test"}))
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_finalize_is_a_no_op(self, capsys):
        ConsoleReporter().finalize()
        assert capsys.readouterr().out == ""


# ============================================================================
# CapturingReporter — buffers events for tests / future HTMLReporter
# ============================================================================

class TestCapturingReporter:
    """CapturingReporter collects every emitted event in order."""

    def test_starts_with_empty_event_list(self):
        assert CapturingReporter().events == []

    def test_emit_appends_in_order(self):
        cr = CapturingReporter()
        cr.emit(StageEvent(kind="info", data={"message": "first"}))
        cr.emit(StageEvent(kind="warning", data={"message": "second"}))
        cr.emit(StageEvent(kind="info", data={"message": "third"}))
        assert len(cr.events) == 3
        assert [e.data["message"] for e in cr.events] == ["first", "second", "third"]

    def test_finalize_does_not_clear_events(self):
        cr = CapturingReporter()
        cr.emit(StageEvent(kind="info", data={}))
        cr.finalize()
        assert len(cr.events) == 1

    def test_events_of_kind_filters_by_kind(self):
        cr = CapturingReporter()
        cr.emit(StageEvent(kind="info", data={"i": 1}))
        cr.emit(StageEvent(kind="warning", data={"i": 2}))
        cr.emit(StageEvent(kind="info", data={"i": 3}))
        infos = cr.events_of_kind("info")
        warnings = cr.events_of_kind("warning")
        assert [e.data["i"] for e in infos] == [1, 3]
        assert [e.data["i"] for e in warnings] == [2]

    def test_first_of_kind_returns_first_match_or_none(self):
        cr = CapturingReporter()
        assert cr.first_of_kind("info") is None
        cr.emit(StageEvent(kind="warning", data={"x": "skip"}))
        cr.emit(StageEvent(kind="info", data={"x": "want"}))
        cr.emit(StageEvent(kind="info", data={"x": "later"}))
        assert cr.first_of_kind("info").data["x"] == "want"


# ============================================================================
# Reporter Protocol — duck-typing check
# ============================================================================

class TestReporterProtocol:
    """ConsoleReporter and CapturingReporter both satisfy the Reporter Protocol."""

    def test_console_reporter_satisfies_protocol(self):
        cr: Reporter = ConsoleReporter()  # type: ignore — duck typing
        cr.emit(StageEvent(kind="info", data={}))
        cr.finalize()

    def test_capturing_reporter_satisfies_protocol(self):
        cr: Reporter = CapturingReporter()  # type: ignore — duck typing
        cr.emit(StageEvent(kind="info", data={}))
        cr.finalize()


# ============================================================================
# HTMLReporter — renders self-contained HTML report at finalize()
# ============================================================================

from nl2protocol.reporting import HTMLReporter


class TestHTMLReporterEmptyEvents:
    """HTMLReporter must render valid HTML even when no events were captured.
    Defensive: a failed pipeline run should still produce a (mostly empty) report.
    """

    def test_finalize_with_no_events_writes_valid_html(self, tmp_path):
        out = tmp_path / "report.html"
        HTMLReporter(str(out)).finalize()
        assert out.exists()
        content = out.read_text()
        assert content.startswith("<!DOCTYPE html>")
        assert "</html>" in content
        assert "Status:" in content  # header rendered

    def test_finalize_creates_parent_directories(self, tmp_path):
        # Reporter must not fail if the output directory doesn't yet exist.
        nested = tmp_path / "deep" / "nested" / "out" / "report.html"
        HTMLReporter(str(nested)).finalize()
        assert nested.exists()


class TestHTMLReporterRendering:
    """HTMLReporter renders captured events into the 4-column layout with
    color-coded provenance per spec value."""

    def _build_minimal_spec(self):
        """Construct a minimal ProtocolSpec with one transfer step.
        All values carry instruction-source provenance per ADR-0005:
        cited_text required for instruction-sourced; structured Q1+Q2
        composite provenance."""
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, LocationRef, Provenance,
            ProtocolSpec, ProvenancedVolume,
        )

        prov = Provenance(source="instruction", cited_text="user said so", confidence=1.0)
        step = ExtractedStep(
            order=1,
            action="transfer",
            volume=ProvenancedVolume(value=100.0, unit="uL", exact=True, provenance=prov),
            source=LocationRef(description="source_plate", well="A1", provenance=prov),
            destination=LocationRef(description="dest_plate", well="B1", provenance=prov),
            composition_provenance=CompositionProvenance(
                step_cited_text="user explicitly requested transfer",
                parameters_cited_texts=["user explicitly requested transfer"],
                parameters_reasoning="single user phrase grounds all transfer parameters",
                grounding=["instruction"],
                confidence=1.0,
            ),
        )
        return ProtocolSpec(summary="test", steps=[step])

    def test_renders_instruction_text(self, tmp_path):
        out = tmp_path / "report.html"
        r = HTMLReporter(str(out))
        r.emit(StageEvent(
            kind="raw_instruction",
            data={"instruction": "Transfer 100uL from A1 to B1"},
        ))
        r.finalize()
        content = out.read_text()
        assert "Transfer 100uL from A1 to B1" in content

    def test_renders_extracted_spec_with_provenance_colors(self, tmp_path):
        out = tmp_path / "report.html"
        r = HTMLReporter(str(out))
        r.emit(StageEvent(
            kind="raw_instruction",
            data={"instruction": "Transfer 100uL from A1 to B1"},
        ))
        r.emit(StageEvent(
            kind="extracted_spec",
            data={"spec": self._build_minimal_spec()},
        ))
        r.finalize()
        content = out.read_text()

        # Spec column has the step with prov-instruction color class on the volume.
        assert "prov-instruction" in content
        assert "100.0 uL" in content
        # Step header shows the new Q1 cite (step_cited_text).
        assert "user explicitly requested transfer" in content
        # Step block carries the grounded-instruction CSS class.
        assert "step-grounded-instruction" in content
        # Q2 (parameters_reasoning) appears in the cohesion section.
        assert "single user phrase grounds all transfer parameters" in content

    def test_renders_generated_script(self, tmp_path):
        out = tmp_path / "report.html"
        r = HTMLReporter(str(out))
        r.emit(StageEvent(
            kind="generated_script",
            data={"script": "def run(protocol):\n    pipette.transfer(100, ...)"},
        ))
        r.finalize()
        content = out.read_text()
        assert "def run(protocol):" in content
        assert "pipette.transfer" in content

    def test_compound_grounding_renders_with_mixed_class(self, tmp_path):
        """A step with grounding=['instruction', 'domain_default'] (a domain-
        expanded named protocol step) renders with the 'mixed' CSS class +
        the step_reasoning explanation surfaces in the header. Note: orphan
        steps (grounding without 'instruction') are no longer possible —
        rejected at parse time per ADR-0005's invariant."""
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, ProtocolSpec,
        )
        step = ExtractedStep(
            order=1,
            action="delay",
            composition_provenance=CompositionProvenance(
                step_cited_text="do a Bradford assay",
                step_reasoning="Bradford workflow includes a 5-min incubation between dye and read",
                parameters_cited_texts=["do a Bradford assay"],
                parameters_reasoning="5-min duration is the canonical Bradford incubation per Bio-Rad",
                grounding=["instruction", "domain_default"],
                confidence=0.8,
            ),
        )
        spec = ProtocolSpec(summary="compound grounding test", steps=[step])

        out = tmp_path / "report.html"
        r = HTMLReporter(str(out))
        r.emit(StageEvent(kind="extracted_spec", data={"spec": spec}))
        r.finalize()
        content = out.read_text()

        # Compound-grounded steps render with the 'mixed' class.
        assert "step-grounded-mixed" in content
        # The step_cited_text (Q1 cite) appears as the trigger.
        assert "do a Bradford assay" in content
        # The step_reasoning (Q1 explanation) appears for domain expansions.
        assert "Bradford workflow includes a 5-min incubation" in content

    def test_orphan_steps_are_impossible_per_schema_invariant(self):
        """Per ADR-0005 the schema rejects steps without instruction grounding
        at parse time. This is no longer a runtime concern for the renderer —
        document the invariant via a Pydantic-validation assertion."""
        from nl2protocol.models.spec import CompositionProvenance
        with pytest.raises(Exception, match="instruction"):
            CompositionProvenance(
                step_cited_text="orphan",
                parameters_cited_texts=["orphan"],
                parameters_reasoning="no instruction backing",
                grounding=["domain_default"],   # rejected
                confidence=0.5,
            )


# ============================================================================
# Phase 3: Span recovery + arrow-rendering helpers
# ============================================================================

from nl2protocol.reporting import (
    _collect_arrow_targets,
    _find_cite_position,
    _palette_class,
    _render_instruction_with_marks,
)


class TestPaletteClass:
    """Per-citation hue: each unique cited_text deterministically maps to
    one of palette-0 .. palette-7. Same cite → same hue across runs."""

    def test_returns_palette_class_string(self):
        cls = _palette_class("100uL")
        assert cls.startswith("palette-")
        assert cls[len("palette-"):].isdigit()
        assert 0 <= int(cls[len("palette-"):]) < 8

    def test_deterministic_across_calls(self):
        # md5-based, so the same string always lands on the same hue
        # (Python's built-in hash() randomizes per-process).
        assert _palette_class("100uL") == _palette_class("100uL")
        assert _palette_class("from A1") == _palette_class("from A1")

    def test_empty_returns_palette_zero_safe_default(self):
        assert _palette_class("") == "palette-0"


class TestGracefulDegradeNoDataProvId:
    """When a cited_text cannot be located in the instruction, the spec
    value span MUST omit data-prov-id — promising a hover link we can't
    deliver is the bug we're avoiding."""

    def test_unrecoverable_cite_omits_data_prov_id(self):
        from nl2protocol.reporting import _render_provenanced_value
        from nl2protocol.models.spec import Provenance
        # cited_text is NOT in the instruction.
        prov = Provenance(source="instruction", cited_text="absent phrase", confidence=1.0)
        out = _render_provenanced_value(
            "100 uL", prov, prov_id="s0-volume",
            instruction="Transfer 100uL from A1 to B1.",
        )
        assert "data-prov-id" not in out
        # But the value is still rendered with provenance metadata for the tooltip.
        assert "data-prov-source=\"instruction\"" in out

    def test_recoverable_cite_emits_data_prov_id_and_palette(self):
        from nl2protocol.reporting import _render_provenanced_value
        from nl2protocol.models.spec import Provenance
        prov = Provenance(source="instruction", cited_text="100uL", confidence=1.0)
        out = _render_provenanced_value(
            "100 uL", prov, prov_id="s0-volume",
            instruction="Transfer 100uL from A1 to B1.",
        )
        assert 'data-prov-id="s0-volume"' in out
        # Recoverable instruction-sourced cites get a palette class.
        assert "palette-" in out

    def test_non_instruction_source_skips_palette(self):
        from nl2protocol.reporting import _render_provenanced_value
        from nl2protocol.models.spec import Provenance
        prov = Provenance(
            source="domain_default",
            reasoning="standard incubation per Bio-Rad",
            confidence=0.7,
        )
        out = _render_provenanced_value(
            "5 minutes", prov, prov_id="s0-duration", instruction="incubate at 37C",
        )
        # Categorical class only — no palette, dotted underline comes from CSS.
        assert "prov-domain_default" in out
        assert "palette-" not in out
        # No data-prov-id — this value isn't sourced from the instruction.
        assert "data-prov-id" not in out


class TestFindCitePosition:
    """Locating cited_text substrings in the instruction (case-insensitive,
    whitespace-tolerant)."""

    def test_exact_match_returns_position(self):
        instr = "Transfer 100uL from A1 to B1."
        assert _find_cite_position(instr, "100uL") == (9, 14)

    def test_case_insensitive_match(self):
        instr = "Transfer 100uL from A1 to B1."
        assert _find_cite_position(instr, "TRANSFER") == (0, 8)

    def test_no_match_returns_none(self):
        instr = "Transfer 100uL from A1 to B1."
        assert _find_cite_position(instr, "absent text") is None

    def test_first_match_when_substring_appears_twice(self):
        instr = "100uL of buffer; then add 100uL of water"
        # First occurrence at position 0, not the second.
        start, end = _find_cite_position(instr, "100uL")
        assert (start, end) == (0, 5)

    def test_empty_cite_returns_none(self):
        assert _find_cite_position("anything", "") is None


class TestRenderInstructionWithMarks:
    """The instruction column gets <span data-cite-id="..."> wrappers around
    each cited substring; un-cited text is plain (HTML-escaped)."""

    def test_no_targets_returns_escaped_plain_text(self):
        out = _render_instruction_with_marks("Plain <text> here", [])
        # HTML escaping happens; no cite spans inserted.
        assert "&lt;text&gt;" in out
        assert "data-cite-id" not in out

    def test_single_target_wraps_substring(self):
        targets = [{"prov_id": "x1", "cited_text": "100uL", "kind": "atomic"}]
        out = _render_instruction_with_marks("Transfer 100uL from A1 to B1.", targets)
        assert 'data-cite-id="x1"' in out
        assert "100uL" in out

    def test_overlapping_targets_both_emitted(self):
        # Overlap is now ALLOWED. The instruction is segmented at every span
        # boundary; segments covered by multiple cites carry a space-separated
        # data-cite-id list (matched in CSS/JS via ~= token selector).
        targets = [
            {"prov_id": "long", "cited_text": "100uL of buffer", "kind": "atomic"},
            {"prov_id": "short", "cited_text": "100uL", "kind": "atomic"},
        ]
        out = _render_instruction_with_marks("Add 100uL of buffer to well A1.", targets)
        # Both cites are reachable — segment "100uL" carries both ids,
        # the trailing " of buffer" carries only "long".
        assert "long" in out
        assert "short" in out
        # The shared segment carries both ids in one data-cite-id attribute.
        assert 'data-cite-id="long short"' in out or 'data-cite-id="short long"' in out


class TestCollectArrowTargets:
    """_collect_arrow_targets walks a spec and produces a list of
    (prov_id, cited_text, kind) records — one per instruction-sourced
    citation."""

    def _spec_with_one_step(self):
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, LocationRef, Provenance,
            ProtocolSpec, ProvenancedVolume,
        )
        prov = Provenance(source="instruction", cited_text="100uL", confidence=1.0)
        step = ExtractedStep(
            order=1,
            action="transfer",
            volume=ProvenancedVolume(value=100.0, unit="uL", exact=True, provenance=prov),
            source=LocationRef(description="src", well="A1",
                               provenance=Provenance(source="instruction", cited_text="from A1", confidence=1.0)),
            destination=LocationRef(description="dst", well="B1",
                                     provenance=Provenance(source="instruction", cited_text="to B1", confidence=1.0)),
            composition_provenance=CompositionProvenance(
                step_cited_text="Transfer 100uL from A1 to B1",
                parameters_cited_texts=["Transfer 100uL from A1 to B1"],
                parameters_reasoning="single phrase grounds all params",
                grounding=["instruction"],
                confidence=1.0,
            ),
        )
        return ProtocolSpec(summary="t", steps=[step])

    def test_emits_composite_step_target(self):
        spec = self._spec_with_one_step()
        targets = _collect_arrow_targets(spec)
        composite_steps = [t for t in targets if t["kind"] == "composite-step"]
        assert len(composite_steps) == 1
        assert composite_steps[0]["prov_id"] == "s0-step-trigger"
        assert composite_steps[0]["cited_text"] == "Transfer 100uL from A1 to B1"

    def test_emits_q2_cohesion_target(self):
        # Q2 (parameter cohesion) cites become hover-only highlights — no arrow.
        # They carry a step_id so JS can light up all Q2 cites for a step in unison.
        spec = self._spec_with_one_step()
        targets = _collect_arrow_targets(spec)
        q2 = [t for t in targets if t["kind"] == "q2-cohesion"]
        assert len(q2) == 1
        assert q2[0]["prov_id"] == "s0-q2-0"
        assert q2[0]["step_id"] == "s0"

    def test_emits_atomic_color_targets_for_each_provenanced_field(self):
        # Atomic cites are color-matched in the instruction column (no arrow).
        # They still need data-cite-id ↔ data-prov-id wiring for hover emphasis.
        spec = self._spec_with_one_step()
        targets = _collect_arrow_targets(spec)
        atomic_ids = sorted(t["prov_id"] for t in targets if t["kind"] == "atomic-color")
        # volume + source + destination, in some order.
        assert atomic_ids == ["s0-destination", "s0-source", "s0-volume"]

    def test_skips_non_instruction_provenance(self):
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, LocationRef, Provenance,
            ProtocolSpec, ProvenancedDuration,
        )
        # Duration is domain_default-sourced; should NOT be in the arrow targets.
        step = ExtractedStep(
            order=1,
            action="delay",
            duration=ProvenancedDuration(
                value=5, unit="minutes",
                provenance=Provenance(
                    source="domain_default",
                    reasoning="standard incubation",
                    confidence=0.7,
                ),
            ),
            composition_provenance=CompositionProvenance(
                step_cited_text="incubate",
                parameters_cited_texts=["incubate"],
                parameters_reasoning="cited",
                grounding=["instruction"],
                confidence=1.0,
            ),
        )
        spec = ProtocolSpec(summary="t", steps=[step])
        targets = _collect_arrow_targets(spec)
        # No atomic target for the domain-default-sourced duration.
        assert not any(t["prov_id"] == "s0-duration" for t in targets)
        # Composite Q1 + Q2 are still emitted.
        assert any(t["kind"] == "composite-step" for t in targets)


class TestHTMLReporterArrowsEnd2End:
    """End-to-end: the HTMLReporter produces a report with cite-marker spans
    in the instruction, matching data-prov-id spans in the spec, and the SVG
    overlay + JS for arrow rendering.

    Also includes the legacy 'status reflects pipeline completion' and
    'self-contained' tests — they ended up grouped here as part of the same
    end-to-end output verification."""

    def _build_minimal_spec(self):
        """Same minimal spec used by TestHTMLReporterRendering — duplicated
        here so the legacy end-to-end tests can construct their own input."""
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, LocationRef, Provenance,
            ProtocolSpec, ProvenancedVolume,
        )
        prov = Provenance(source="instruction", cited_text="100uL", confidence=1.0)
        step = ExtractedStep(
            order=1,
            action="transfer",
            volume=ProvenancedVolume(value=100.0, unit="uL", exact=True, provenance=prov),
            source=LocationRef(description="src", well="A1", provenance=prov),
            destination=LocationRef(description="dst", well="B1", provenance=prov),
            composition_provenance=CompositionProvenance(
                step_cited_text="user explicitly requested transfer",
                parameters_cited_texts=["user explicitly requested transfer"],
                parameters_reasoning="single user phrase",
                grounding=["instruction"],
                confidence=1.0,
            ),
        )
        return ProtocolSpec(summary="t", steps=[step])

    def test_report_has_arrow_overlay_and_matching_spans(self, tmp_path):
        # Use non-overlapping cites — composite cites the high-level phrase,
        # atomic cites grab distinct substrings. The "first match wins" greedy
        # rule documented in _render_instruction_with_marks otherwise drops
        # overlapping atomic cites; this test verifies the non-overlapping
        # happy path that's the most common case in real LLM output.
        from nl2protocol.models.spec import (
            CompositionProvenance, ExtractedStep, LocationRef, Provenance,
            ProtocolSpec, ProvenancedVolume,
        )
        # Atomic destination cite ("to well B1") is fully separate from the
        # composite cite ("Add liquid"), so both get rendered.
        step = ExtractedStep(
            order=1,
            action="transfer",
            volume=ProvenancedVolume(
                value=100.0, unit="uL", exact=True,
                provenance=Provenance(source="instruction", cited_text="100uL", confidence=1.0),
            ),
            destination=LocationRef(
                description="dest_plate", well="B1",
                provenance=Provenance(source="instruction", cited_text="to well B1", confidence=1.0),
            ),
            composition_provenance=CompositionProvenance(
                step_cited_text="Add liquid",
                parameters_cited_texts=["Add liquid"],
                parameters_reasoning="composite cite grounds the action",
                grounding=["instruction"],
                confidence=1.0,
            ),
        )
        spec = ProtocolSpec(summary="t", steps=[step])

        out = tmp_path / "report.html"
        r = HTMLReporter(str(out))
        r.emit(StageEvent(kind="raw_instruction",
                          data={"instruction": "Add liquid: pipette 100uL to well B1."}))
        r.emit(StageEvent(kind="extracted_spec", data={"spec": spec}))
        r.finalize()
        content = out.read_text()

        # Cite spans appear as tokens in data-cite-id attrs. The composite
        # "Add liquid" cite shares its anchor with the Q2 cite (same substring),
        # so the segment may carry multiple ids — we check token-membership
        # rather than exact attribute equality.
        import re
        cite_attrs = re.findall(r'data-cite-id="([^"]+)"', content)
        cite_tokens = {tok for attr in cite_attrs for tok in attr.split()}
        assert "s0-step-trigger" in cite_tokens
        assert "s0-destination" in cite_tokens
        # Spec column: data-prov-id on the destination value and the step block.
        assert 'data-prov-id="s0-destination"' in content
        assert 'data-prov-id="s0-step-trigger"' in content
        # SVG overlay + arrow-rendering JS still wired.
        assert 'id="arrow-overlay"' in content
        assert "renderArrows" in content

    def test_status_reflects_pipeline_completion(self, tmp_path):
        """If a generated_script event was captured, status = success.
        Otherwise (extraction-only or earlier), status = failed."""
        # Failed run — only extracted_spec, no script
        out_fail = tmp_path / "fail.html"
        r = HTMLReporter(str(out_fail))
        r.emit(StageEvent(kind="extracted_spec", data={"spec": self._build_minimal_spec()}))
        r.finalize()
        assert "failed" in out_fail.read_text()

        # Success run — script generated
        out_success = tmp_path / "success.html"
        r2 = HTMLReporter(str(out_success))
        r2.emit(StageEvent(kind="generated_script", data={"script": "def run(): pass"}))
        r2.finalize()
        assert "success" in out_success.read_text()

    def test_html_is_self_contained_no_external_resources(self, tmp_path):
        """The report should be a single file with no external CSS/JS/images."""
        out = tmp_path / "report.html"
        HTMLReporter(str(out)).finalize()
        content = out.read_text()
        # Inline CSS only — no <link rel="stylesheet">
        assert "<link rel=\"stylesheet\"" not in content.lower()
        # No external scripts (Phase 2 has no JS at all)
        assert "<script src=" not in content.lower()
        # No external images either
        assert "<img " not in content.lower()
