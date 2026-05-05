"""
Type definitions for the unified gap-resolution interface (ADR-0008).

Five core types:
  - Gap          — a typed deficiency in the spec
  - Suggestion   — a proposed fill for a Gap
  - ReviewResult — the IndependentReviewSuggester's verdict on a Suggestion
  - Resolution   — the user's decision (or auto-accept) on a Gap
  - GapKind / GapSeverity / ResolutionAction — enums spelling out
                  the closed sets used in the above

Pure data classes. No logic, no I/O, no LLM calls. Validated where validators
catch real misuse (severity-kind constraints, mutually-exclusive fields).

Design notes:
  - `field_path` uses dotted notation matching how the renderer addresses
    fields elsewhere ("steps[3].source", "initial_contents[0].volume_ul").
    Stable across pipeline stages so a Gap can be tracked across re-detect
    iterations.
  - Suggestion carries BOTH `positive_reasoning` and `why_not_in_instruction`
    per ADR-0009. The reviewer pass tests them as independently-falsifiable
    claims; bundling them into one string would defeat the review's purpose.
  - Resolution carries the PROVENANCE OF THE USER'S ACTION
    (user_confirmed / user_edited / user_accepted_suggestion / user_skipped),
    distinct from the suggester source. The orchestrator combines both into
    the final Provenance written back into the spec — see ADR-0009 for the
    full state diagram.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


# ============================================================================
# Enums (closed sets)
# ============================================================================

GapKind = Literal[
    "missing",                # field is null but required (or expected)
    "fabricated",             # cited_text claimed source="instruction" but cite not in text
    "low_confidence",         # source-tagged but confidence below threshold
    "ambiguous",              # multiple valid interpretations (e.g. labware mapping)
    "constraint_violation",   # spec violates a hardware/labware constraint
    "unverifiable",           # no way to check the claim at this stage
]

GapSeverity = Literal[
    "blocker",                # cannot proceed without resolution
    "suggestion",             # works but better with resolution
    "quality",                # optional review, not load-bearing
]

ResolutionAction = Literal[
    "accept_suggestion",      # take the Suggestion's value
    "edit",                   # user typed a replacement value
    "skip",                   # leave the gap unresolved (only valid for severity="quality")
    "abort",                  # halt the pipeline
]


# ============================================================================
# Gap
# ============================================================================

@dataclass(frozen=True)
class Gap:
    """A typed deficiency in the spec.

    `id` is a stable handle that survives across re-detect iterations so the
    orchestrator can tell "the same gap that wasn't resolved last round" from
    "a new gap that just appeared."

    Pre:    All required fields populated. `field_path` uses dotted notation
            matching the renderer's addressing scheme. `severity` matches the
            kind (blockers for missing/fabricated/constraint_violation;
            quality for low_confidence/ambiguous; etc.).
    Post:   Immutable record consumed by Suggesters and ConfirmationHandlers.
    """

    id: str                              # stable handle, e.g. "step3.source"
    step_order: Optional[int]            # None for spec-level gaps (e.g. initial_contents)
    field_path: str                      # dotted, e.g. "steps[3].source"
    kind: GapKind
    current_value: Any                   # what the field currently holds (often None)
    description: str                     # human-readable
    severity: GapSeverity
    metadata: dict = field(default_factory=dict)  # detector-specific extras (e.g. config_match_candidates)


# ============================================================================
# Suggestion
# ============================================================================

@dataclass(frozen=True)
class Suggestion:
    """A proposed fill for a Gap.

    Per ADR-0009: positive_reasoning and why_not_in_instruction are TWO
    independently-falsifiable claims. The IndependentReviewSuggester tests
    each separately. Bundling them into one fused reasoning string would
    leave the reviewer with only a vibe-check.

    `provenance_source` records which kind of source produced the suggestion:
      - "deterministic" — config lookup, capacity default, carryover from
        prior step, regex from note text. Correct-or-empty.
      - "domain_default" — LLM proposed based on standard practice.
      - "inferred" — LLM proposed based on context-specific reasoning.

    `confidence` is the suggester's self-assessment. The orchestrator uses
    it (combined with gap kind) to decide auto-accept vs user confirmation.

    Pre:    All fields populated. positive_reasoning and why_not_in_instruction
            are non-empty (vague boilerplate like "not specified" should not
            be acceptable; that's a prompt/suggester concern, not a type
            concern, but the type at least requires presence).
    Post:   Immutable record consumed by ReviewResult and ConfirmationHandler.
    """

    value: Any
    provenance_source: Literal["inferred", "domain_default", "deterministic"]
    positive_reasoning: str
    why_not_in_instruction: Optional[str]   # may be None for "deterministic" sources where it's tautological
    confidence: float                       # 0.0–1.0

    def __post_init__(self):
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"Suggestion.confidence must be in [0.0, 1.0]; got {self.confidence!r}"
            )
        if not self.positive_reasoning.strip():
            raise ValueError("Suggestion.positive_reasoning must be non-empty")


# ============================================================================
# ReviewResult
# ============================================================================

@dataclass(frozen=True)
class ReviewResult:
    """The IndependentReviewSuggester's structured verdict on a Suggestion.

    Two independently-confirmed claims, not a single agree/disagree. This
    is the key insight from ADR-0008: structured review beats vibe review
    because each claim has a specific falsifier (positive: domain norms;
    negative: instruction text).

    Pre:    field_path matches the Gap.id whose Suggestion was reviewed.
            objection populated iff one of the confirms_* is False.
    Post:   Immutable record. Orchestrator routes the Gap based on the
            two booleans:
              both True  → auto-accept candidate (subject to confidence check)
              either False → escalate to user with `objection` surfaced
    """

    field_path: str
    confirms_positive: bool          # is positive_reasoning sound (domain knowledge check)?
    confirms_negative: bool          # is why_not_in_instruction correct (text-grounding check)?
    objection: Optional[str]         # specific text only when either is False

    def __post_init__(self):
        either_disconfirmed = not (self.confirms_positive and self.confirms_negative)
        if either_disconfirmed and not self.objection:
            raise ValueError(
                "ReviewResult must set `objection` text when either "
                "confirms_positive or confirms_negative is False"
            )
        if (not either_disconfirmed) and self.objection:
            raise ValueError(
                "ReviewResult must NOT set `objection` when both claims are confirmed"
            )


# ============================================================================
# Resolution
# ============================================================================

@dataclass(frozen=True)
class Resolution:
    """The user's decision (or auto-accept) on a Gap.

    Returned by ConfirmationHandler.present(); consumed by the orchestrator
    which writes the result back into the spec with the appropriate
    provenance tags.

    `user_action_provenance` is what gets stamped into Provenance.review_status
    per ADR-0009 — distinguishes "accepted as-is", "user typed a new value",
    "accepted the suggester's proposal", and "user skipped this gap"
    (only valid for severity="quality").

    Pre:    `action` ∈ {accept_suggestion, edit, skip, abort}.
            `new_value` populated unless action="abort" or action="skip".
            `user_action_provenance` matches `action`.
    Post:   Immutable record. Orchestrator handles "abort" by halting the
            loop; otherwise writes `new_value` into the spec.
    """

    action: ResolutionAction
    new_value: Any                                          # may be None for skip/abort
    user_action_provenance: Literal[
        "user_confirmed",                                   # accepted the existing value as-is
        "user_edited",                                      # typed a replacement
        "user_accepted_suggestion",                         # accepted the suggester's value
        "user_skipped",                                     # left unresolved (severity=quality only)
        "user_aborted",                                     # halted the pipeline
    ]
