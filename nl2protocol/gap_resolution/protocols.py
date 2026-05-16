"""
Protocols (interfaces) for gap-resolution components (ADR-0008).

Three protocols, structurally typed (Python's `Protocol`). Implementations
register through the registry; the orchestrator calls them through these
interfaces with no knowledge of which concrete implementation is wired in.

This is what lets the same architecture serve both the CLI today and the
HTML confirmation UI later — they're both ConfirmationHandler implementations
consuming the same Gap and Suggestion objects.
"""

from __future__ import annotations

from typing import List, Optional, Protocol

from nl2protocol.gap_resolution.types import (
    Gap,
    Resolution,
    Suggestion,
)


class GapDetector(Protocol):
    """Detects deficiencies in a spec; produces Gap records.

    Implementations REPORT only — they MUST NOT mutate the spec. The
    orchestrator owns mutation via Resolution application.

    `context` carries cross-cutting state the detector might need
    (instruction text, config, prior pipeline-stage outputs). Detectors
    pluck what they need; unused keys are ignored.

    Pre:    `spec` is a fully-parsed ProtocolSpec (Pydantic-validated).
            `context` is a dict potentially containing:
              - "instruction": str  — raw user instruction
              - "config":      dict — lab config
              - other detector-specific keys
    Post:   Returns a list of Gap (possibly empty). Each Gap has a stable
            `id` so re-detection can match against prior iterations.
    Side effects: None.
    """

    def detect(self, spec, context: dict) -> List[Gap]: ...


class Suggester(Protocol):
    """Proposes a fill value for a Gap; returns None if it cannot help.

    Suggesters are tried in registry-defined precedence order — typically
    deterministic-first (config lookup, capacity defaults, action-semantics
    carryover) → LLM-spot (one Gap per call, focused context) → opt-in
    full-refine (ADR-0008's last resort).

    The first Suggester to return non-None wins for that Gap. Subsequent
    suggesters in the chain don't run. This is intentional — it makes the
    cost story predictable (cheap suggesters resolve most gaps; LLM calls
    happen only when deterministic fails).

    Pre:    `gap` is a Gap from a recent DETECT pass. `spec` and `context`
            same as GapDetector.
    Post:   Returns a Suggestion or None. If None, orchestrator tries the
            next suggester. If Suggestion, the orchestrator may auto-accept
            (subject to confidence + ALWAYS_CONFIRM rules) or escalate to
            ConfirmationHandler.
    Side effects: None on `spec`. Suggesters that call the LLM are allowed
            to log the call and consume API budget — that's an external
            side effect, not a spec mutation.
    """

    def suggest(self, gap: Gap, spec, context: dict) -> Optional[Suggestion]: ...


class ConfirmationHandler(Protocol):
    """Presents a Gap (with optional Suggestion) to the user; returns the
    user's Resolution.

    This is the swap point between CLI and HTML confirmation UIs. Both
    consume the same Gap and Suggestion shapes; their only difference is
    how they render the prompt and capture user input.

    The CLI handler shows the existing prompt formats (collapsed into one).
    The HTML handler (deferred — separate ADR) renders into the existing
    report DOM and accepts user input via a bridge mechanism.

    Pre:    `gap` is a Gap that needs user attention (orchestrator decided
            it's not auto-acceptable). `suggestion` is the best available
            suggester output, or None if no suggester could help.
    Post:   Returns a Resolution. If the user aborts (Resolution.action ==
            "abort"), the orchestrator halts. Otherwise the resolved value
            is written into the spec with the appropriate provenance tags.
    Side effects: I/O appropriate to the handler kind (terminal output,
            HTML rendering, etc.).
    """

    def present(self, gap: Gap, suggestion: Optional[Suggestion]) -> Resolution: ...
