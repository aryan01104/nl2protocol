"""
Detector adapters around existing detection functions (ADR-0008 PR1).

Each adapter wraps an existing function in nl2protocol that already finds
deficiencies, and translates its output into `Gap` records. No new
detection logic. No behavior change in the underlying functions.

This is the skeleton: detectors are defined and tested, but NOT wired
into the production pipeline yet. PR2 adds suggesters + an orchestrator
that consumes these detectors. PR3 cuts over.

Design notes:
  - Adapters call the underlying function with the same arguments that
    pipeline.py uses today, so the gap stream is byte-identical to what
    the current pipeline collects (just typed instead of dict/string).
  - `MissingFieldsDetector` regex-parses the validate_completeness error
    strings. Brittle but pinned by tests. Parsing rather than rewriting
    the validator keeps PR1 a no-behavior-change skeleton.
  - Constraint, initial-volume, and labware-ambiguity detectors are
    deferred to PR2 — those underlying functions are larger refactors
    to wrap cleanly. PR1 covers the four extractor-based ones.
"""

from __future__ import annotations

import re
from typing import List, Optional

from nl2protocol.gap_resolution.types import Gap, GapKind, GapSeverity


# ============================================================================
# MissingFieldsDetector — wraps SemanticExtractor.missing_fields
# ============================================================================

# Format produced by CompleteProtocolSpec.validate_completeness:
#   "Step N (action): description"
# e.g. "Step 3 (transfer): no source for 'water' — add it to your config"
#      "Step 13 (mix): missing volume"
_MISSING_FIELD_PATTERN = re.compile(r"^Step\s+(\d+)\s+\(([^)]+)\):\s*(.+)$")


def _missing_field_to_path(action: str, description: str) -> str:
    """Best-effort mapping from validate_completeness's free-text description
    to a dotted field path. Falls back to including the description verbatim
    when the field isn't recognizable.

    Examples:
      "missing volume"                                   → ".volume"
      "missing destination location"                     → ".destination"
      "no source for 'X' — add it to your config"        → ".source"
      "missing temperature target"                       → ".temperature"
      "missing duration"                                 → ".duration"
      "missing note"                                     → ".note"
      "pause requires either duration (timed) or ..."    → ".duration|.note"
    """
    desc_lower = description.lower()
    if "missing volume" in desc_lower:
        return ".volume"
    if "missing destination" in desc_lower or "destination location" in desc_lower:
        return ".destination"
    if "no source for" in desc_lower or "missing source" in desc_lower:
        return ".source"
    if "missing temperature" in desc_lower:
        return ".temperature"
    if "missing duration" in desc_lower:
        return ".duration"
    if "missing note" in desc_lower:
        return ".note"
    if "duration" in desc_lower and "note" in desc_lower:
        # pause polymorphic case
        return ".duration|.note"
    return ".unknown"


def _missing_field_kind_and_severity(description: str) -> tuple:
    """All validate_completeness errors are 'missing' kind + 'blocker'
    severity by definition (the validator only fires when a required
    field is absent)."""
    return "missing", "blocker"


class MissingFieldsDetector:
    """Wraps `SemanticExtractor.missing_fields`.

    Calls `missing_fields(spec)` (which attempts CompleteProtocolSpec
    promotion and collects validation errors), then translates each
    error string into a `Gap`.

    Pre:    `spec` is a parsed ProtocolSpec.
    Post:   Returns one Gap per error string from the underlying function.
            Each Gap has stable id `"step{N}.{field}"` derivable from the
            error message; `kind="missing"`, `severity="blocker"`.
    Side effects: None.
    """

    def detect(self, spec, context: dict) -> List[Gap]:
        from nl2protocol.extraction import SemanticExtractor

        gaps: List[Gap] = []
        for msg in SemanticExtractor.missing_fields(spec):
            m = _MISSING_FIELD_PATTERN.match(msg)
            if m:
                step_order = int(m.group(1))
                action = m.group(2)
                description = m.group(3)
                field_path_suffix = _missing_field_to_path(action, description)
                kind, severity = _missing_field_kind_and_severity(description)
                gap_id = f"step{step_order}{field_path_suffix}"
                gaps.append(Gap(
                    id=gap_id,
                    step_order=step_order,
                    field_path=f"steps[{step_order - 1}]{field_path_suffix}",
                    kind=kind,
                    current_value=None,
                    description=msg,
                    severity=severity,
                ))
            else:
                # Unparseable error — emit as a spec-level gap so it isn't
                # silently dropped. id needs to be unique-enough that
                # iteration matching works.
                gaps.append(Gap(
                    id=f"unparseable:{hash(msg) & 0xffffffff:08x}",
                    step_order=None,
                    field_path="unknown",
                    kind="missing",
                    current_value=None,
                    description=msg,
                    severity="blocker",
                ))
        return gaps


# ============================================================================
# ProvenanceWarningDetector — wraps SemanticExtractor.verify_provenance_claims
# ============================================================================

_SEVERITY_MAP = {
    # underlying severity → (Gap.kind, Gap.severity)
    "fabrication":     ("fabricated",     "blocker"),
    "low_confidence":  ("low_confidence", "suggestion"),
    "unverified":      ("low_confidence", "quality"),
}


def _warning_to_field_path(warning: dict) -> str:
    """Best-effort dotted-path for a provenance warning."""
    step = warning.get("step")
    field = (warning.get("field") or "").strip()
    if step == 0:
        # Cross-check warnings have step=0 — they're spec-level.
        return f"cross_check.{field}"
    if not field:
        return f"steps[{step - 1}].unknown"
    # Field strings include things like "source labware", "source wells",
    # "mix volume". Normalize to a dotted suffix.
    field_norm = field.replace(" ", "_")
    return f"steps[{step - 1}].{field_norm}"


def _warning_id(warning: dict) -> str:
    step = warning.get("step", 0)
    field = (warning.get("field") or "unknown").replace(" ", "_")
    severity = warning.get("severity", "unknown")
    return f"step{step}.{field}.{severity}"


class ProvenanceWarningDetector:
    """Wraps `SemanticExtractor.verify_provenance_claims`.

    Calls the public verifier (which internally runs fabrication detection
    on instruction- and config-claimed values, plus uncertainty flagging
    for low-confidence inferred / domain_default values), and translates
    each warning dict into a Gap.

    Severity mapping per ADR-0008:
      "fabrication"    → kind="fabricated",     severity="blocker"
      "low_confidence" → kind="low_confidence", severity="suggestion"
      "unverified"     → kind="low_confidence", severity="quality"

    Pre:    `spec` is a parsed ProtocolSpec; `context` includes
            "instruction" (raw text) and "config" (lab config dict).
            The underlying verifier requires both.
    Post:   Returns one Gap per warning. Gap.id is deterministic from
            (step, field, severity) so re-detection across iterations
            can match.
    Side effects: None.
    """

    def __init__(self, extractor):
        # Underlying function is an instance method — needs the SemanticExtractor.
        self._extractor = extractor

    def detect(self, spec, context: dict) -> List[Gap]:
        instruction = context.get("instruction", "")
        config = context.get("config", {})
        warnings = self._extractor.verify_provenance_claims(spec, instruction, config)
        gaps: List[Gap] = []
        for w in warnings:
            severity_str = w.get("severity", "unverified")
            mapped = _SEVERITY_MAP.get(severity_str)
            if mapped is None:
                # Unknown severity: keep as quality-level flag rather than dropping.
                kind, gap_severity = "unverifiable", "quality"
            else:
                kind, gap_severity = mapped
            gaps.append(Gap(
                id=_warning_id(w),
                step_order=w.get("step") if w.get("step", 0) > 0 else None,
                field_path=_warning_to_field_path(w),
                kind=kind,
                current_value=w.get("value"),
                description=w.get("message", ""),
                severity=gap_severity,
                metadata={
                    "claimed_source": w.get("claimed_source"),
                    "underlying_severity": severity_str,
                },
            ))
        return gaps


# ============================================================================
# Adapter list for the registry
# ============================================================================

def default_extractor_detectors(extractor) -> List:
    """Return the PR1-implemented detectors that wrap extractor functions.

    The registry (registry.py) uses this as one of its sources. PR2 adds
    constraint, initial-volume, and labware-ambiguity detectors.
    """
    return [
        MissingFieldsDetector(),
        ProvenanceWarningDetector(extractor),
    ]
