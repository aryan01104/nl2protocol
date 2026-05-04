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
