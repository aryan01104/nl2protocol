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
