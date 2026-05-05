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
    # Per ADR-0008 PR2 lessons: only fabrications become Gaps under the new
    # architecture. Confidence gating happens at the orchestrator's classify
    # step (auto_accept_threshold + ALWAYS_CONFIRM), not at detect time —
    # otherwise auto-filled inferred values get re-flagged on the next
    # iteration, creating a circular detection loop.
    "fabrication":     ("fabricated",     "blocker"),
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
                # Non-fabrication warnings (low_confidence, unverified) are
                # intentionally NOT emitted as Gaps under the new architecture
                # (see _SEVERITY_MAP comment). Drop them — confidence gating
                # is the orchestrator's job at classify time.
                continue
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
# InitialContentsVolumeDetector — emits Gaps for null volume_ul entries
# ============================================================================

class InitialContentsVolumeDetector:
    """Emits one Gap per `WellContents` entry whose `volume_ul` is null.

    Per ADR-0008 PR3a, replaces today's `_build_initial_volume_queue` (which
    builds an ad-hoc legacy-CLI queue post-stage-4). The same data flows
    through the orchestrator's uniform Gap → Suggester → Resolution loop:
    `WellCapacitySuggester` (already in suggesters.py) proposes the
    labware's capacity as a default; the user accepts, edits, or skips
    via the unified ConfirmationHandler.

    Pre:    `spec` is a ProtocolSpec; `spec.initial_contents` may have
            entries with null `volume_ul` (the LLM extracted "well X has
            substance Y" without stating a volume). `context` is unused.
    Post:   Returns one Gap per null-volume entry, with:
              * `id`          = f"initial_contents[{N}]" (stable handle so
                                re-detect across iterations matches).
              * `field_path`  = f"initial_contents[{N}].volume_ul" (matches
                                WellCapacitySuggester's regex AND
                                default_apply_resolution's path-shape).
              * `step_order`  = None (this is a spec-level gap, not tied
                                to any step).
              * `kind`        = "missing".
              * `severity`    = "blocker" — codegen needs the volume for
                                initial-state tracking and well-capacity
                                bookkeeping.
            Entries whose volume_ul is set are skipped silently.
    Side effects: None (read-only).
    """

    def detect(self, spec, context: dict) -> List[Gap]:
        gaps: List[Gap] = []
        for idx, ic in enumerate(spec.initial_contents):
            if ic.volume_ul is not None:
                continue
            gaps.append(Gap(
                id=f"initial_contents[{idx}]",
                step_order=None,
                field_path=f"initial_contents[{idx}].volume_ul",
                kind="missing",
                current_value=None,
                description=(
                    f"Initial contents entry {idx}: well {ic.well} of "
                    f"'{ic.labware}' contains '{ic.substance}' but no "
                    f"volume is stated."
                ),
                severity="blocker",
            ))
        return gaps


# ============================================================================
# ConstraintViolationDetector — wraps ConstraintChecker
# ============================================================================

class ConstraintViolationDetector:
    """Wraps `ConstraintChecker.check_all` to emit Gaps for ERROR-severity
    violations.

    Per ADR-0008 PR3a, lifts today's stage-4 hard-block (the "proceed anyway?"
    interactive prompt with no suggester support) into the orchestrator's
    uniform Gap → Suggester loop. ERROR violations become Gaps with
    kind="constraint_violation", severity="blocker"; `WellRangeClipSuggester`
    proposes a clipped well-list for the `WELL_INVALID` (list) case;
    other violation types (pipette capacity, labware-not-found, etc.) emit
    Gaps but no suggester fires — they fall through to the user.

    WARNING-severity violations are NOT emitted (informational only — same
    as today's pipeline behavior).

    Pre:    `spec` is a ProtocolSpec; `context["config"]` is the lab config
            dict — required, since ConstraintChecker needs it. If absent,
            returns [] (defensive — orchestrator-with-no-config simply
            skips constraint detection).
    Post:   Returns one Gap per ERROR-severity ConstraintViolation. Each Gap:
              * `id`          = f"constraint.step{step}.{violation_type}"
                                (stable across re-detect iterations).
              * `step_order`  = violation.step.
              * `field_path`  = violation-type-specific. For WELL_INVALID
                                with a `wells` list, walks `spec.steps[N]`
                                to determine whether the offending wells
                                are on source or destination, returning
                                `steps[{N-1}].source.wells` or
                                `steps[{N-1}].destination.wells` so the
                                eventual apply lands on the right ref.
                                Fallback for other types: a generic
                                `steps[{N-1}].constraint` placeholder
                                (apply path-shape match: no-op write,
                                safe — the user must edit the upstream
                                config to fix these classes of violation).
              * `kind`        = "constraint_violation".
              * `severity`    = "blocker".
              * `description` = `{what}. {suggestion}` (combined so the
                                WellRangeClipSuggester's regex finds both
                                the offending-wells list and the valid range).
              * `metadata`    = {"violation_type": str, "values": dict}
                                (machine-readable details from the
                                underlying ConstraintViolation).
    Side effects: None (read-only).
    """

    def detect(self, spec, context: dict) -> List[Gap]:
        from nl2protocol.validation.constraints import ConstraintChecker, Severity
        config = context.get("config", {})
        if not config:
            return []
        result = ConstraintChecker(config).check_all(spec)
        gaps: List[Gap] = []
        for v in result.violations:
            if v.severity != Severity.ERROR:
                continue
            description = f"{v.what}. {v.suggestion}"
            gap_id = f"constraint.step{v.step}.{v.violation_type.value}"
            gaps.append(Gap(
                id=gap_id,
                step_order=v.step,
                field_path=self._field_path_for(v, spec),
                kind="constraint_violation",
                current_value=None,
                description=description,
                severity="blocker",
                metadata={
                    "violation_type": v.violation_type.value,
                    "values": dict(v.values),
                },
            ))
        return gaps

    @staticmethod
    def _field_path_for(v, spec) -> str:
        """Best-effort dotted path for a ConstraintViolation. Used by
        `default_apply_resolution` to land the value when a Suggestion
        is accepted; for violation types with no suggester, the path is
        just informational (the user reads the description, edits config,
        and re-runs)."""
        from nl2protocol.validation.constraints import ViolationType
        idx = max(0, v.step - 1)
        if idx >= len(spec.steps):
            return f"steps[{idx}].constraint"
        step = spec.steps[idx]
        if v.violation_type == ViolationType.WELL_INVALID:
            invalid = set(v.values.get("invalid_wells", []))
            single = v.values.get("well")
            # Single-well violation: target the .well subfield.
            if single is not None:
                if step.source and step.source.well == single:
                    return f"steps[{idx}].source.well"
                if step.destination and step.destination.well == single:
                    return f"steps[{idx}].destination.well"
                return f"steps[{idx}].source.well"
            # List-well violation: locate which ref carries the offending wells.
            if step.source and step.source.wells and any(w in invalid for w in step.source.wells):
                return f"steps[{idx}].source.wells"
            if step.destination and step.destination.wells and any(w in invalid for w in step.destination.wells):
                return f"steps[{idx}].destination.wells"
            return f"steps[{idx}].source.wells"
        if v.violation_type == ViolationType.LABWARE_NOT_FOUND:
            role = v.values.get("role")
            return f"steps[{idx}].{role}" if role in ("source", "destination") else f"steps[{idx}].constraint"
        if v.violation_type == ViolationType.PIPETTE_CAPACITY:
            return f"steps[{idx}].volume"
        return f"steps[{idx}].constraint"


# ============================================================================
# LabwareAmbiguityDetector — emits Gaps for unresolved LocationRefs
# ============================================================================

class LabwareAmbiguityDetector:
    """Emits one Gap per LocationRef whose `resolved_label is None` after
    the LabwareResolver has run.

    Per ADR-0008 PR3a step 3 + the user-validated design: the LLM resolver
    picks a config label per description; refs the resolver couldn't
    confidently match (returned null) are the only ones surfaced to the
    user. Refs the resolver picked are trusted (subject to the reviewer
    pass on `resolved_label_provenance` — verified by
    IndependentReviewSuggester downstream).

    The pipeline runs LabwareResolver BEFORE the orchestrator, so this
    detector sees post-resolver state. If the resolver hasn't run yet
    (e.g. no LLM client available), every LocationRef has
    `resolved_label is None` and every LocationRef becomes a Gap —
    defensive, surfaces all refs to the user rather than silently
    passing them through.

    Pre:    `spec` is a ProtocolSpec; `context` is unused (config is read
            by the suggester, not the detector — this stays decoupled
            from the lab config so the detector can be tested without
            one).
    Post:   Returns one Gap per LocationRef where `resolved_label is None`,
            with:
              * `id`         = f"labware.step{N}.{role}" (stable handle
                                so re-detect across iterations matches).
              * `step_order` = step.order.
              * `field_path` = f"steps[{N-1}].{role}.resolved_label"
                                (matches default_apply_resolution's
                                three-segment path-shape; the apply
                                writes the chosen label string into
                                the resolved_label subfield).
              * `kind`       = "ambiguous".
              * `severity`   = "blocker" — the spec cannot proceed to
                                schema generation without a resolved
                                config label per ref.
              * `description`= human-readable summary including the
                                user's description + the role + step.
              * `metadata`   = {"description": <user wording>, "role": "source"|"destination"}.
            Refs where `resolved_label` IS set are skipped (the resolver
            picked something; the reviewer pass on
            resolved_label_provenance handles second-opinion checking).
    Side effects: None (read-only).
    """

    def detect(self, spec, context: dict) -> List[Gap]:
        gaps: List[Gap] = []
        for step in spec.steps:
            idx = step.order - 1
            for role in ("source", "destination"):
                ref = getattr(step, role, None)
                if ref is None:
                    continue
                if ref.resolved_label is not None:
                    continue
                gaps.append(Gap(
                    id=f"labware.step{step.order}.{role}",
                    step_order=step.order,
                    field_path=f"steps[{idx}].{role}.resolved_label",
                    kind="ambiguous",
                    current_value=None,
                    description=(
                        f"Step {step.order} {role}: "
                        f"'{ref.description}' did not match any single config "
                        f"labware confidently — pick one from the available labware."
                    ),
                    severity="blocker",
                    metadata={
                        "description": ref.description,
                        "role": role,
                    },
                ))
        return gaps


# ============================================================================
# Adapter list for the registry
# ============================================================================

def default_extractor_detectors(extractor) -> List:
    """Return the PR1-implemented detectors that wrap extractor functions.

    The registry (registry.py) uses this as one of its sources. PR3a adds
    spec-walking detectors (InitialContentsVolume, ConstraintViolation,
    LabwareAmbiguity) — those don't need an extractor and so live in
    `default_spec_detectors()` below.
    """
    return [
        MissingFieldsDetector(),
        ProvenanceWarningDetector(extractor),
    ]


def default_spec_detectors() -> List:
    """Return the PR3a detectors that walk the spec directly (no extractor
    or LLM dependency). Composed with `default_extractor_detectors` to
    form the full detector chain. ConstraintViolationDetector reads
    `context["config"]` for the lab config it needs; the orchestrator
    threads it through automatically."""
    return [
        InitialContentsVolumeDetector(),
        ConstraintViolationDetector(),
        LabwareAmbiguityDetector(),
    ]
