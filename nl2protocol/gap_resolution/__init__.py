"""
nl2protocol.gap_resolution — unified gap-resolution interface (ADR-0008).

Public types and protocols. Implementation modules (detectors, suggesters,
reviewer, orchestrator, handlers) live as siblings and register themselves
through the registry. Nothing in this module is wired into the production
pipeline yet — see ADR-0008's three-PR migration plan. This module is the
PR1 skeleton: types + detector adapters, no behavior change.
"""

from nl2protocol.gap_resolution.types import (
    Gap,
    GapKind,
    GapSeverity,
    Suggestion,
    Resolution,
    ResolutionAction,
    ReviewResult,
)
from nl2protocol.gap_resolution.protocols import (
    GapDetector,
    Suggester,
    ConfirmationHandler,
)
from nl2protocol.gap_resolution.detectors import (
    MissingFieldsDetector,
    ProvenanceWarningDetector,
    default_extractor_detectors,
)
from nl2protocol.gap_resolution.suggesters import (
    CarryoverSuggester,
    ConfigLookupSuggester,
    IndependentReviewSuggester,
    LLMSpotSuggester,
    RegexFromNoteSuggester,
    WellCapacitySuggester,
    WellRangeClipSuggester,
)
from nl2protocol.gap_resolution.handlers import CLIConfirmationHandler
from nl2protocol.gap_resolution.orchestrator import (
    Orchestrator,
    OrchestratorOutcome,
    IterationResult,
    GapResolutionRecord,
    default_apply_resolution,
    DEFAULT_AUTO_ACCEPT_THRESHOLD,
    DEFAULT_MAX_ITERATIONS,
    ALWAYS_CONFIRM_KINDS,
    topo_sort_gaps,
)
from nl2protocol.gap_resolution.registry import detect_all

__all__ = [
    "Gap",
    "GapKind",
    "GapSeverity",
    "Suggestion",
    "Resolution",
    "ResolutionAction",
    "ReviewResult",
    "GapDetector",
    "Suggester",
    "ConfirmationHandler",
    "MissingFieldsDetector",
    "ProvenanceWarningDetector",
    "default_extractor_detectors",
    "detect_all",
    "ConfigLookupSuggester",
    "CarryoverSuggester",
    "WellCapacitySuggester",
    "RegexFromNoteSuggester",
    "WellRangeClipSuggester",
    "LLMSpotSuggester",
    "IndependentReviewSuggester",
    "CLIConfirmationHandler",
    "Orchestrator",
    "OrchestratorOutcome",
    "IterationResult",
    "GapResolutionRecord",
    "default_apply_resolution",
    "DEFAULT_AUTO_ACCEPT_THRESHOLD",
    "DEFAULT_MAX_ITERATIONS",
    "ALWAYS_CONFIRM_KINDS",
    "topo_sort_gaps",
]
