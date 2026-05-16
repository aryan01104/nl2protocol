"""
Contract tests for WebSocketReporter + CompositeReporter (ADR-0013 Phase 3a).

The reporter implementations are thin (push events to a queue / fan out
to multiple sinks). Tests pin:
  - emit() pushes events to the bound queue in order
  - finalize() pushes the PIPELINE_DONE_SENTINEL
  - the events are also buffered locally on .events (matches CapturingReporter shape)
  - CompositeReporter fans out to every reporter, even if one raises
  - A failing reporter doesn't break the pipeline (silently swallowed)

Network-level tests for the FastAPI app (/, /events, /start) live in
their own integration test file when 3a's worker thread + WebSocket
flow is meaningful to exercise end-to-end. For Phase 3a this unit
layer is sufficient.
"""

from __future__ import annotations

import queue

import pytest

from nl2protocol.reporting import StageEvent
from nl2protocol.server.reporter import (
    CompositeReporter,
    PIPELINE_DONE_SENTINEL,
    WebSocketReporter,
)


# ============================================================================
# WebSocketReporter
# ============================================================================

class TestWebSocketReporter:
    """WebSocketReporter implements the Reporter protocol; pushes events
    to a thread-safe queue + buffers locally."""

    def test_emit_pushes_event_to_queue(self):
        q: queue.Queue = queue.Queue()
        r = WebSocketReporter(q)
        ev = StageEvent(kind="info", data={"message": "hi"})
        r.emit(ev)
        assert q.get_nowait() is ev

    def test_emit_appends_to_local_events_list(self):
        # Same pattern as CapturingReporter — events are buffered on
        # .events for tests / state recovery.
        q: queue.Queue = queue.Queue()
        r = WebSocketReporter(q)
        ev1 = StageEvent(kind="info", data={"message": "1"})
        ev2 = StageEvent(kind="info", data={"message": "2"})
        r.emit(ev1)
        r.emit(ev2)
        assert r.events == [ev1, ev2]

    def test_emit_preserves_order(self):
        q: queue.Queue = queue.Queue()
        r = WebSocketReporter(q)
        events = [StageEvent(kind="info", data={"i": i}) for i in range(5)]
        for ev in events:
            r.emit(ev)
        # Drain queue; should be FIFO order matching emit order.
        drained = [q.get_nowait() for _ in range(5)]
        assert drained == events

    def test_finalize_pushes_sentinel(self):
        q: queue.Queue = queue.Queue()
        r = WebSocketReporter(q)
        r.emit(StageEvent(kind="info", data={}))
        r.finalize()
        # First item is the event; second is the sentinel.
        _ = q.get_nowait()
        assert q.get_nowait() is PIPELINE_DONE_SENTINEL

    def test_emit_drops_event_silently_when_queue_full(self):
        # Defensive: queue overflow shouldn't crash the pipeline.
        q: queue.Queue = queue.Queue(maxsize=1)
        r = WebSocketReporter(q)
        r.emit(StageEvent(kind="info", data={"i": 1}))
        # Second emit hits the full queue — drops silently, no exception.
        r.emit(StageEvent(kind="info", data={"i": 2}))
        # Local buffer still has both (the drop only affects queue stream).
        assert len(r.events) == 2


# ============================================================================
# CompositeReporter
# ============================================================================

class TestCompositeReporter:
    """CompositeReporter fans events out to multiple reporter instances.
    A failing reporter doesn't break the pipeline (errors swallowed)."""

    def test_emit_fans_out_to_all_reporters(self):
        q1: queue.Queue = queue.Queue()
        q2: queue.Queue = queue.Queue()
        r1 = WebSocketReporter(q1)
        r2 = WebSocketReporter(q2)
        composite = CompositeReporter(r1, r2)
        ev = StageEvent(kind="info", data={"message": "fan"})
        composite.emit(ev)
        # Both reporters received it.
        assert q1.get_nowait() is ev
        assert q2.get_nowait() is ev

    def test_finalize_fans_out_to_all_reporters(self):
        q1: queue.Queue = queue.Queue()
        q2: queue.Queue = queue.Queue()
        composite = CompositeReporter(WebSocketReporter(q1), WebSocketReporter(q2))
        composite.finalize()
        # Both queues got the sentinel.
        assert q1.get_nowait() is PIPELINE_DONE_SENTINEL
        assert q2.get_nowait() is PIPELINE_DONE_SENTINEL

    def test_emit_swallows_exceptions_from_failing_reporter(self):
        # Defensive: a failing sink shouldn't kill the pipeline.
        class FailingReporter:
            def emit(self, event):
                raise RuntimeError("boom")
            def finalize(self):
                raise RuntimeError("boom-final")

        q: queue.Queue = queue.Queue()
        good = WebSocketReporter(q)
        composite = CompositeReporter(FailingReporter(), good)
        # Should NOT raise.
        ev = StageEvent(kind="info", data={})
        composite.emit(ev)
        composite.finalize()
        # The good reporter still received the event.
        assert q.get_nowait() is ev

    def test_no_reporters_emit_is_noop(self):
        composite = CompositeReporter()
        # Should not raise.
        composite.emit(StageEvent(kind="info", data={}))
        composite.finalize()

    def test_reporters_property_exposes_constituents(self):
        q1: queue.Queue = queue.Queue()
        q2: queue.Queue = queue.Queue()
        r1 = WebSocketReporter(q1)
        r2 = WebSocketReporter(q2)
        composite = CompositeReporter(r1, r2)
        assert composite.reporters == (r1, r2)
