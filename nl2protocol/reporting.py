"""
reporting.py — Pluggable stage-event reporting for the pipeline.

The CLI runs `ProtocolAgent.run_pipeline` with a `ConsoleReporter` (default)
that prints stage banners and progress to stdout. Tests construct the agent
with a `CapturingReporter` to inspect the event stream without I/O. Future
HTML / TUI / web outputs implement the same `Reporter` Protocol and consume
the same structured events.

The reporter does NOT replace every `_log` call in the pipeline — only the
events that carry load-bearing data (stage transitions, the extracted spec,
the completed spec, the generated script, warnings, errors). Progress noise
("Reasoning through protocol...", "Validating config...") stays as `_log`
because it's not data downstream consumers need.

Why a structured event stream rather than free-text log lines: HTML / TUI
output sinks need typed access to spec, script, provenance — not a string
they have to parse. Structured events make the same data renderable in
multiple formats from one source.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Literal, Optional, Protocol


# ============================================================================
# Event taxonomy
# ============================================================================

# Output kinds an event may carry. Keep this list closed — adding a new kind
# means every Reporter implementation must decide how to render it.
EventKind = Literal[
    "stage_start",          # data = {"stage": "Stage 2/7", "label": "Reasoning through ..."}
    "stage_complete",       # data = {"stage": "Stage 2/7", "summary": "..."}
    "stage_failed",         # data = {"stage": "Stage 2/7", "reason": "..."}
    "raw_instruction",      # data = {"instruction": "..."}
    "extracted_spec",       # data = {"spec": ProtocolSpec}
    "completed_spec",       # data = {"spec": CompleteProtocolSpec}
    "generated_script",     # data = {"script": "..."}
    "warning",              # data = {"message": "...", "context": "..."}
    "error",                # data = {"message": "...", "stage": "..."}
    "info",                 # data = {"message": "..."}  — short status notes
]


@dataclass
class StageEvent:
    """Single structured event emitted at a meaningful pipeline checkpoint.

    `kind` says what happened; `data` is a kind-specific dict (typed loosely
    so consumers can grow without schema migration). `timestamp` and
    `stage_name` give consumers enough to render chronological / per-stage
    views.
    """

    kind: EventKind
    data: dict
    stage_name: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


# ============================================================================
# Reporter interface
# ============================================================================

class Reporter(Protocol):
    """Sink for pipeline stage events.

    `emit` is called by `ProtocolAgent.run_pipeline` at every meaningful
    checkpoint. `finalize` is called once at the end of a successful or
    failed run; sinks that buffer events (HTMLReporter, etc.) write their
    output here.
    """

    def emit(self, event: StageEvent) -> None:
        """Record a single stage event."""
        ...

    def finalize(self) -> None:
        """Called once at end-of-run. Sinks that buffer should write here."""
        ...


# ============================================================================
# ConsoleReporter — preserves current CLI behavior
# ============================================================================

class ConsoleReporter:
    """Default reporter — silent on all events.

    Rationale: the CLI already prints stage banners, progress messages, spec
    summaries etc. via the existing `_log` / `_stage` helpers in pipeline.py.
    ConsoleReporter exists as the default `Reporter` so the type is satisfied;
    it does NOT print anything itself, because doing so would duplicate the
    CLI output. The CLI flow stays exactly as today.

    Future text-renderers (e.g., a SilentReporter for tests, a JSONReporter
    for log aggregation) can implement Reporter without colliding with the
    CLI output. The HTMLReporter (Phase 2) buffers events via
    CapturingReporter and renders one HTML file at finalize().
    """

    def emit(self, event: StageEvent) -> None:
        pass  # silent — see class docstring

    def finalize(self) -> None:
        pass


# ============================================================================
# CapturingReporter — for tests + base for buffering reporters (HTMLReporter)
# ============================================================================

class CapturingReporter:
    """Collects all events to a list. Useful for tests + as the base for
    sinks that need to buffer the entire run before rendering (e.g.,
    HTMLReporter, which renders one HTML file at finalize()).

    Pre:    Constructed with no args.
    Post:   `events` is a list of every `StageEvent` passed to `emit`, in
            order. `finalize()` is a no-op for the base class — subclasses
            override to render at end-of-run.
    """

    def __init__(self):
        self.events: List[StageEvent] = []

    def emit(self, event: StageEvent) -> None:
        self.events.append(event)

    def finalize(self) -> None:
        pass

    # Convenience accessors for tests
    def events_of_kind(self, kind: EventKind) -> List[StageEvent]:
        return [e for e in self.events if e.kind == kind]

    def first_of_kind(self, kind: EventKind) -> Optional[StageEvent]:
        events = self.events_of_kind(kind)
        return events[0] if events else None
