"""
WebSocketReporter — pushes pipeline events into a thread-safe queue
that the FastAPI WebSocket handler drains and forwards to the browser.

Per ADR-0013 Phase 3a, this bridges the pipeline thread (synchronous,
calls emit() as events fire) to the FastAPI event loop (async, awaits
queue items via run_in_executor and sends them over WebSocket as JSON).
"""

from __future__ import annotations

import queue
from typing import Any, List

from nl2protocol.reporting import StageEvent


# Sentinel value pushed onto the queue when the pipeline finishes.
# The WebSocket handler reads this and closes the connection cleanly.
PIPELINE_DONE_SENTINEL: Any = "<<pipeline_done>>"


class WebSocketReporter:
    """Reporter implementation for live mode.

    Pre:    `event_queue` is a `queue.Queue` (thread-safe) used as the
            bridge between the pipeline thread (which calls emit/finalize)
            and the FastAPI event loop (which drains and sends).

    Post:   Each `emit(event)` call pushes the event onto the queue.
            `finalize()` pushes the PIPELINE_DONE_SENTINEL so the
            WebSocket handler knows to close the connection. Events are
            ALSO buffered locally on `self.events` for tests + archive
            (matches the existing `CapturingReporter` shape).

    Thread-safety: `queue.Queue.put` and `.get` are thread-safe by Python
    standard library guarantee. No locks needed at this layer.
    """

    def __init__(self, event_queue: "queue.Queue[Any]"):
        self._queue = event_queue
        self.events: List[StageEvent] = []

    def emit(self, event: StageEvent) -> None:
        self.events.append(event)
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # Queue overflow: drop the event. This shouldn't happen with
            # a sized queue (10k items default) but if it does we'd
            # rather drop a stage event than crash the pipeline.
            pass

    def finalize(self) -> None:
        try:
            self._queue.put_nowait(PIPELINE_DONE_SENTINEL)
        except queue.Full:
            pass


class CompositeReporter:
    """Fan-out reporter that delegates emit/finalize to multiple sinks.

    Live mode wires both a `WebSocketReporter` (browser stream) AND an
    `HTMLReporter` (static replay artifact written to disk at end-of-run)
    so the user gets both the real-time view AND a shareable file. This
    composite is the wiring point.

    Pre:    `reporters` is one or more Reporter-protocol instances.
    Post:   `emit(event)` calls each reporter's `emit` in order.
            `finalize()` calls each reporter's `finalize` in order.
            Exceptions from one reporter don't prevent others from
            receiving the event (each is wrapped in try/except).
    """

    def __init__(self, *reporters: Any):
        self._reporters = reporters

    def emit(self, event: StageEvent) -> None:
        for r in self._reporters:
            try:
                r.emit(event)
            except Exception:
                # A failing reporter shouldn't kill the pipeline. Log
                # and continue. (For Phase 3a we silently swallow; future
                # work can route to a structured logger.)
                pass

    def finalize(self) -> None:
        for r in self._reporters:
            try:
                r.finalize()
            except Exception:
                pass

    @property
    def reporters(self):
        return self._reporters
