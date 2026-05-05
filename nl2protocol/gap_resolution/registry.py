"""
Detector registry — runs every registered GapDetector and returns the union
of Gaps in deterministic order (ADR-0008 PR1).

PR1 scope: just gather. No suggestion, no review, no resolution. PR2's
orchestrator wraps this with the rest of the loop.

Design notes:
  - Order across detectors is fixed (registration order) so re-runs produce
    the same Gap sequence; helps debugging and matches the orchestrator's
    "stable id across iterations" requirement.
  - The registry has no opinion on which detectors are "right" — it just
    runs whatever is passed in. Deciding the default detector set is the
    caller's job (typically `default_extractor_detectors` from detectors.py
    plus future suggester-equivalents from later PRs).
"""

from __future__ import annotations

from typing import List

from nl2protocol.gap_resolution.protocols import GapDetector
from nl2protocol.gap_resolution.types import Gap


def detect_all(spec, context: dict, detectors: List[GapDetector]) -> List[Gap]:
    """Run every detector against the spec; concatenate their gaps in
    detector order.

    Pre:    `spec` is a parsed ProtocolSpec.
            `context` is a dict carrying cross-cutting state (instruction,
            config, etc.); detectors pluck what they need.
            `detectors` is a non-empty list of GapDetector implementations.
    Post:   Returns a flat list of Gap. Order is deterministic: detectors
            run in registry order, each detector's gaps in its own
            deterministic order. Empty list means a clean spec.
    Side effects: None on `spec`. Detectors are guaranteed not to mutate
            (per the GapDetector protocol contract).
    Raises: Propagates any exception a detector raises (treated as a
            programming error — detectors should not raise on well-formed
            input).
    """
    gaps: List[Gap] = []
    for detector in detectors:
        gaps.extend(detector.detect(spec, context))
    return gaps
