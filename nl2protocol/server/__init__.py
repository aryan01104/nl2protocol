"""
nl2protocol.server — live-mode FastAPI server (ADR-0013).

Per ADR-0013, live mode is the primary user surface. `nl2protocol serve`
starts the server, opens the browser, runs the pipeline server-side,
and streams stage events over WebSocket to the page as they fire.

Phase 3a (this module) builds the backend network layer:
  - `WebSocketReporter` implements the Reporter protocol; pipeline thread
    pushes events into a thread-safe queue.
  - `LiveModeApp` is the FastAPI app wrapper: GET / serves a placeholder
    page; WS /events streams events to the browser; POST /start kicks
    off the pipeline in a worker thread.

Phase 3b will replace the placeholder page with the real five-column
rendering that consumes WebSocket events incrementally.
Phase 3c will add interactive confirmation (HTMLConfirmationHandler).
"""

from nl2protocol.server.reporter import (
    CompositeReporter,
    WebSocketReporter,
)
from nl2protocol.server.app import LiveModeApp, run_serve

__all__ = [
    "CompositeReporter",
    "WebSocketReporter",
    "LiveModeApp",
    "run_serve",
]
