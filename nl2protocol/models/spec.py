"""
spec.py — The "science language" intermediate representation.

ProtocolSpec is what the LLM produces: structured steps with provenance
tracking that describe what the scientist wants in domain terms.

Every extracted value carries a Provenance tag saying where it came from
(instruction, config, domain_default, inferred) so downstream stages
can verify claims and route uncertain values for user confirmation.
"""

from typing import Annotated, List, Optional, Literal

from pydantic import BaseModel, Field, model_validator


WellName = Annotated[str, Field(pattern=r'^[A-P](1[0-9]|2[0-4]|[1-9])$')]


# ============================================================================
# PROVENANCE
# ============================================================================

class Provenance(BaseModel):
    """Per-value provenance: where a single extracted/inferred value came from
    AND where it currently sits in the human-review lifecycle.

    Three orthogonal concerns on every Provenance:

    1. **Source attribution** — `source` + (`cited_text` | `positive_reasoning`):
         - source == "instruction":  cited_text required (verbatim quote from
                                     the user's instruction). The citation IS
                                     the justification — no reasoning fields.
         - source == "domain_default" / "inferred":
                                     positive_reasoning required.
                                     why_not_in_instruction strongly recommended
                                     when an instruction exists; the
                                     extractor/suggester boundary enforces that
                                     since the schema cannot tell on its own.

    2. **Reasoning split** (ADR-0009) — `positive_reasoning` + `why_not_in_instruction`:
       Pre-ADR-0009 used a single `reasoning` field. The split lets the
       independent reviewer verify two distinct claims separately
       (positive: 'why is X right?'; negative: 'why not just cite?') and lets
       the HTML report render them as labeled rows. Backwards compatibility:
       legacy `reasoning="..."` input migrates into positive_reasoning via
       `_migrate_legacy_reasoning`, and the read-only `.reasoning` property
       returns positive_reasoning so existing read-sites keep working.

    3. **Review lifecycle** (ADR-0009) — `review_status` + `reviewer_objection`:
       Tracks how this Provenance moved through the gap-resolver loop. New
       values default to "original"; reviewer/user actions stamp the
       appropriate state, so the audit trail survives into the spec, the
       JSON state dumps, and the HTML report.

    'config' is NOT a valid source for Provenance — the extractor LLM does
    not see the lab config. Suggesters that look up values from config write
    source="inferred" with positive_reasoning that cites the config path.
    See ADR-0005.
    """
    source: Literal["instruction", "domain_default", "inferred"] = Field(..., description=(
        "Where this value came from. "
        "'instruction' = user literally wrote it (cited_text required). "
        "'domain_default' = standard practice for a named protocol (positive_reasoning required). "
        "'inferred' = reasoning or guess with no direct support (positive_reasoning required)."
    ))
    cited_text: Optional[str] = Field(None, description=(
        "The verbatim substring from the instruction text that grounds this value. "
        "REQUIRED when source == 'instruction'. Must appear verbatim in the instruction "
        "(case-insensitive, whitespace-normalized). For numbers, the cited substring "
        "should contain the value (e.g., for value=100uL, cited_text might be "
        "'100uL of buffer'). Leave null when source is 'domain_default' or 'inferred'."
    ))
    positive_reasoning: Optional[str] = Field(None, description=(
        "One sentence answering: 'why is THIS the right value?'. "
        "REQUIRED when source in {'domain_default', 'inferred'}. "
        "For 'domain_default': cite the protocol and standard practice. "
        "For 'inferred': state the reasoning chain that yields this specific value. "
        "Leave null when source is 'instruction' (the citation IS the justification)."
    ))
    why_not_in_instruction: Optional[str] = Field(None, description=(
        "One sentence answering: 'why did I have to infer this instead of cite it?'. "
        "Examples: 'instruction names the substance but not its source labware — "
        "looked up via config' / 'instruction does not specify temperature for wait — "
        "inherited from prior set_temperature step'. Leave null when source is "
        "'instruction'. Strongly recommended (but not schema-enforced) for "
        "'inferred'/'domain_default' when an instruction exists; the extractor and "
        "suggesters enforce this at their boundary."
    ))
    review_status: Literal[
        "original",
        "reviewed_agree",
        "reviewed_disagree",
        "user_confirmed",
        "user_edited",
        "user_accepted_suggestion",
        "user_skipped",
    ] = Field("original", description=(
        "Where this Provenance sits in the gap-resolver review lifecycle. "
        "'original'                = just extracted or just suggested; not yet reviewed. "
        "'reviewed_agree'          = independent reviewer confirmed the claims. "
        "'reviewed_disagree'       = reviewer flagged a concern (see reviewer_objection). "
        "'user_confirmed'          = user saw the value and kept it as-is. "
        "'user_edited'             = user typed a new value. "
        "'user_accepted_suggestion'= user took the suggester's value verbatim. "
        "'user_skipped'            = user explicitly skipped the gap (value remains original)."
    ))
    reviewer_objection: Optional[str] = Field(None, description=(
        "The independent reviewer's stated concern. REQUIRED when "
        "review_status == 'reviewed_disagree'; FORBIDDEN otherwise. Surfaces in the "
        "CLI prompt and HTML report so the user can see why the reviewer pushed back."
    ))
    confidence: float = Field(..., ge=0.0, le=1.0, description=(
        "How confident this value is correct. "
        "1.0 = user literally wrote it. "
        "0.8 = standard protocol default, clearly that protocol. "
        "0.6 = reasonable inference from context. "
        "0.4 = plausible guess with weak support. "
        "Below 0.4 = not sure."
    ))

    @model_validator(mode='before')
    @classmethod
    def _migrate_legacy_reasoning(cls, data):
        """Migrate legacy `reasoning="..."` input to `positive_reasoning="..."`.

        Pre:    Raw input passed to the Provenance constructor — typically a
                dict (from kwargs, model_validate, or JSON deserialization).
                May also be an already-validated Provenance instance when
                Pydantic re-runs validation; in that case this is a no-op.

        Post:   When `data` is a dict containing the legacy `reasoning` key:
                  * Removes `reasoning` from the dict.
                  * If the legacy value is truthy AND `positive_reasoning` is
                    not already set, copies the legacy value into
                    `positive_reasoning`.
                  * If `positive_reasoning` is already set, the legacy value
                    is dropped (positive_reasoning takes precedence).
                Returns the (possibly mutated) `data` so subsequent validation
                operates on the migrated input.
                When `data` is anything else: returns it unchanged.

        Side effects: Mutates the input dict in place when migration applies.

        Raises: Never.
        """
        if isinstance(data, dict) and "reasoning" in data:
            legacy = data.pop("reasoning")
            if legacy and not data.get("positive_reasoning"):
                data["positive_reasoning"] = legacy
        return data

    @model_validator(mode='after')
    def require_appropriate_field_for_source(self) -> 'Provenance':
        """Enforce per-source field invariants on cite + reasoning fields.

        Pre:    Pydantic-validated Provenance instance.

        Post:   Returns self when the source/cite/reasoning fields are
                self-consistent. Specifically:
                  * source == 'instruction'  → cited_text required;
                                                positive_reasoning AND
                                                why_not_in_instruction must
                                                both be empty (the citation
                                                IS the justification).
                  * source != 'instruction'  → positive_reasoning required;
                                                cited_text must be empty
                                                (non-instruction-sourced
                                                values cannot ground in
                                                user-quoted text).
                why_not_in_instruction is allowed but not required for
                non-instruction sources at the schema layer; the extractor
                and suggesters enforce its presence when an instruction exists.

        Raises: ValueError describing the offending field combination.
        """
        if self.source == "instruction":
            if not self.cited_text:
                raise ValueError(
                    "Provenance with source='instruction' requires cited_text "
                    "(the verbatim substring from the instruction). "
                    f"Got cited_text={self.cited_text!r}."
                )
            if self.positive_reasoning:
                raise ValueError(
                    "Provenance with source='instruction' must NOT carry "
                    "positive_reasoning — the citation IS the justification."
                )
            if self.why_not_in_instruction:
                raise ValueError(
                    "Provenance with source='instruction' must NOT carry "
                    "why_not_in_instruction — the value WAS in the instruction "
                    "(that's why source is 'instruction')."
                )
        else:  # domain_default or inferred
            if not self.positive_reasoning:
                raise ValueError(
                    f"Provenance with source='{self.source}' requires "
                    "positive_reasoning (an explanation of how this value follows "
                    "from domain knowledge or inference). "
                    f"Got positive_reasoning={self.positive_reasoning!r}."
                )
            if self.cited_text:
                raise ValueError(
                    f"Provenance with source='{self.source}' must NOT carry "
                    "cited_text — non-instruction-sourced values cannot ground "
                    "in user-quoted text."
                )
        return self

    @model_validator(mode='after')
    def require_reviewer_objection_iff_disagree(self) -> 'Provenance':
        """Enforce reviewer_objection ↔ review_status == 'reviewed_disagree'.

        Pre:    Pydantic-validated Provenance instance.

        Post:   Returns self when EITHER:
                  * review_status == 'reviewed_disagree' AND reviewer_objection
                    is non-empty,
                  OR
                  * review_status != 'reviewed_disagree' AND reviewer_objection
                    is None.
                Mirrors the same biconditional that
                gap_resolution.types.ReviewResult enforces — once a
                disagreement has been stamped onto a Provenance, the
                objection rationale must accompany it; conversely, no other
                review state may carry an objection.

        Raises: ValueError on either side of the biconditional being violated.
        """
        if self.review_status == "reviewed_disagree":
            if not self.reviewer_objection:
                raise ValueError(
                    "Provenance with review_status='reviewed_disagree' requires "
                    "reviewer_objection (the reviewer's stated concern). "
                    f"Got reviewer_objection={self.reviewer_objection!r}."
                )
        else:
            if self.reviewer_objection:
                raise ValueError(
                    f"Provenance with review_status='{self.review_status}' must NOT "
                    "carry reviewer_objection — only 'reviewed_disagree' carries one."
                )
        return self

    @property
    def reasoning(self) -> Optional[str]:
        """DEPRECATED read-only alias for positive_reasoning.

        Pre-ADR-0009 callers read `prov.reasoning`. ADR-0009 split the field
        into positive_reasoning + why_not_in_instruction. This property keeps
        legacy READS working (returns positive_reasoning); legacy WRITES are
        handled by `_migrate_legacy_reasoning`. New code should read
        `positive_reasoning` directly. NOT a Pydantic field — does not appear
        in model_dump() output.
        """
        return self.positive_reasoning


class CompositionProvenance(BaseModel):
    """Per-step provenance: answers TWO questions about why a step exists as a unit.

    Q1 (step existence): Why does a step of this kind exist at all?
       Answered by: step_cited_text (the user phrase that triggered it)
                  + optional step_reasoning (how a domain expansion produced this step type).

    Q2 (parameter cohesion): Why do these specific parameter values belong to this same step?
       Answered by: parameters_cited_texts (one or more user phrases grounding the values)
                  + parameters_reasoning (how the cites combine into one operation).

    Both questions must be answered. The split makes the provenance debuggable and
    machine-renderable as visualization arrows (see HTML report Phase 3 + ADR-0005).

    Architectural invariant: every step MUST be grounded in 'instruction'. The LLM
    is permitted to interpret natural language and expand named protocols via domain
    knowledge, but it is NOT permitted to inject steps the user did not ask for.

    'config' is NOT a valid grounding source — the extractor LLM does not have
    access to the lab config.
    """
    # Q1: Why this step exists
    step_cited_text: str = Field(..., description=(
        "The verbatim phrase from the instruction that triggered this kind of step "
        "(e.g., 'Add 2uL of plasmid DNA' for a transfer step, or 'do a Bradford assay' "
        "for a step expanded from a named protocol). MUST appear verbatim in the "
        "instruction text."
    ))
    step_reasoning: Optional[str] = Field(None, description=(
        "Optional explanation of how the cited instruction phrase expanded into THIS "
        "step type. Only used when grounding includes 'domain_default' — explains the "
        "domain-knowledge step (e.g., 'Bradford workflow includes a 5-min incubation "
        "between dye and absorbance read'). Leave null when the step is grounded "
        "purely in instruction (the cite is sufficient)."
    ))

    # Q2: Why these specific parameter values cohere as one step
    parameters_cited_texts: List[str] = Field(..., min_length=1, description=(
        "One or more verbatim phrases from the instruction that ground the specific "
        "parameter values for this step (volume + source + destination + substance + "
        "duration etc.). Often a single phrase covers all parameters; for complex "
        "steps, multiple phrases combine. Each must appear verbatim in the instruction."
    ))
    parameters_reasoning: str = Field(..., description=(
        "One paragraph explaining how the parameters_cited_texts combine to fully "
        "specify this step's parameters. For named-protocol expansions where some "
        "parameter values come from domain defaults, explain which parts are user-stated "
        "vs domain-defaulted."
    ))

    grounding: List[Literal["instruction", "domain_default"]] = Field(..., description=(
        "Which sources contributed to this step's existence. MUST include 'instruction' "
        "— every step traces back to something the user asked for. May additionally "
        "include 'domain_default' when expanding a named protocol."
    ))
    confidence: float = Field(..., ge=0.0, le=1.0, description=(
        "How confident this step should exist. "
        "1.0 = user explicitly described this exact step. "
        "0.8 = standard part of a named protocol the user invoked. "
        "0.5 = seems necessary but user didn't mention it."
    ))

    @model_validator(mode='after')
    def require_instruction_grounding(self) -> 'CompositionProvenance':
        """Every step must trace back to user instruction.

        Raises ValueError if 'instruction' is not in self.grounding. A step
        grounded only in domain_default would be a step the LLM injected
        without instruction backing — exactly the hallucination pattern
        we removed Stage 8 for in ADR-0004.
        """
        if "instruction" not in self.grounding:
            raise ValueError(
                f"composition_provenance.grounding must include 'instruction' "
                f"— every step must trace back to something the user asked for. "
                f"Got grounding={self.grounding}. If a step is purely domain "
                f"knowledge with no instruction origin, it should not be added "
                f"to the spec — surface it to the user instead."
            )
        return self

    @model_validator(mode='after')
    def require_step_reasoning_for_domain_expansion(self) -> 'CompositionProvenance':
        """If the step's existence depends on domain knowledge, step_reasoning must explain why.

        Pre:    Pydantic-validated CompositionProvenance with grounding populated.

        Post:   When 'domain_default' is in grounding, step_reasoning is required
                — the step exists because of domain knowledge expansion, and that
                expansion must be explained. When grounding is just ['instruction'],
                step_reasoning is optional (the cite is sufficient).

        Raises: ValueError when domain_default is in grounding but step_reasoning
                is missing.
        """
        if "domain_default" in self.grounding and not self.step_reasoning:
            raise ValueError(
                "composition_provenance.step_reasoning is required when "
                "grounding includes 'domain_default' — explain how the cited "
                "instruction phrase expanded into this step via domain knowledge. "
                f"Got step_reasoning={self.step_reasoning!r}."
            )
        return self


# ============================================================================
# PROVENANCED TYPES
# ============================================================================

class ProvenancedVolume(BaseModel):
    """A volume with provenance tracking."""
    value: float = Field(..., gt=0, description="Numeric volume. Copy the user's number exactly — never round or adjust.")
    unit: Literal["uL", "mL"] = Field(..., description="Required. Must be 'uL' or 'mL' — never inferred or defaulted.")
    exact: bool = Field(True, description=(
        "True if the user stated this exact number ('100uL'). "
        "False if the user hedged ('about 100uL', '~50uL') or if this value was inferred. "
        "This is independent of provenance — a value can come from the instruction but still not be exact."
    ))
    provenance: Provenance


class ProvenancedDuration(BaseModel):
    """A time duration with provenance tracking."""
    value: float = Field(..., gt=0, description="Copy the user's number exactly.")
    unit: Literal["seconds", "minutes", "hours"]
    provenance: Provenance


class ProvenancedTemperature(BaseModel):
    """A temperature in Celsius with provenance tracking."""
    value: float = Field(..., description="Temperature in degrees Celsius.")
    provenance: Provenance


class ProvenancedString(BaseModel):
    """A string value with provenance tracking."""
    value: str
    provenance: Provenance


# ============================================================================
# LOCATION AND ACTION TYPES
# ============================================================================

class LocationRef(BaseModel):
    """A reference to a labware location, as the user described it."""
    description: str = Field(..., description=(
        "How the user referred to this labware. Copy their wording exactly — "
        "'reservoir', 'PCR plate', 'the tube rack'. Do not translate to config labels or load names. "
        "Examples: 'reservoir', 'PCR plate', 'tube rack'."
    ))
    well: Optional[WellName] = Field(None, description=(
        "Use for a single well position matching pattern [A-P][1-24]. "
        "Do not use alongside wells or well_range — pick exactly one. "
        "Examples: 'A1', 'B6', 'H12'."
    ))
    wells: Optional[List[WellName]] = Field(None, description=(
        "Use for an explicit list of individual wells, each matching pattern [A-P][1-24]. "
        "Do not use alongside well or well_range — pick exactly one. "
        "Examples: ['A1', 'B1', 'C1'], ['A1', 'A2', 'A3'], ['D4', 'E5', 'F6']."
    ))
    well_range: Optional[str] = Field(None, description=(
        "Use for contiguous ranges or natural-language region descriptions. "
        "Do not use alongside well or wells — pick exactly one. "
        "Examples: 'A1-A12', 'column 1', 'columns 2-8'."
    ))
    resolved_label: Optional[str] = Field(None, description=(
        "Config labware key. Filled automatically by the labware resolver — leave null during extraction."
    ))
    provenance: Provenance = Field(..., description=(
        "How this location reference was determined. Covers the labware + well(s) as a unit. "
        "REQUIRED — every populated LocationRef must carry provenance, matching the field-level "
        "requirement on every other provenance-carrying value (volume/duration/temperature/substance). "
        "If the wells were derived from a prior step (e.g. 'Mix each tube' inheriting B1-B4), "
        "use source='instruction' and cite the substring naming those wells. If genuinely inferred "
        "from context with no cite, use source='inferred' with reasoning."
    ))


class PostAction(BaseModel):
    """Post-transfer action like mixing."""
    action: Literal["mix", "blow_out", "touch_tip"]
    repetitions: Optional[int] = Field(None, description=(
        "Number of mix cycles. Set only if the user specified a count. Do not infer a default — leave null."
    ))
    volume: Optional[ProvenancedVolume] = Field(None, description=(
        "Volume per mix cycle. Set only if the user specified a mix volume. Do not infer a default — leave null."
    ))


ActionType = Literal[
    "transfer", "distribute", "consolidate", "serial_dilution",
    "mix", "delay", "pause", "comment",
    "aspirate", "dispense", "blow_out", "touch_tip",
    "set_temperature", "wait_for_temperature",
    "engage_magnets", "disengage_magnets", "deactivate",
]


class ExtractedStep(BaseModel):
    """One logical step in the protocol."""
    order: int = Field(..., ge=1)
    action: ActionType = Field(..., description=(
        "The protocol action. Must be one of the allowed values. Do not invent new action names."
    ))
    composition_provenance: CompositionProvenance = Field(..., description=(
        "What justifies this step's existence? What reasoning links these parameters "
        "into one distinct action? Cite instruction text, domain knowledge, and/or "
        "config constraints. Be conservative: if in doubt, lower confidence."
    ))
    substance: Optional[ProvenancedString] = Field(None, description=(
        "What is being moved or acted on. Copy the substance name as the user wrote it. "
        "Do not normalize, translate, or infer if unspecified — leave null."
    ))
    volume: Optional[ProvenancedVolume] = Field(None, description=(
        "Liquid volume for pipetting (uL or mL). Only for liquid-handling actions "
        "(transfer, distribute, mix, etc.). Do NOT put temperature values here — "
        "use the temperature field for set_temperature/wait_for_temperature steps."
    ))
    temperature: Optional[ProvenancedTemperature] = Field(None, description=(
        "Temperature in Celsius. Only for set_temperature and wait_for_temperature steps. "
        "Do NOT put this in the volume or note fields."
    ))
    duration: Optional[ProvenancedDuration] = Field(None, description=(
        "For delay, pause, or incubation steps only. Do not put time values in the volume field."
    ))
    source: Optional[LocationRef] = None
    destination: Optional[LocationRef] = None
    post_actions: Optional[List[PostAction]] = None
    replicates: Optional[int] = Field(None, description=(
        "Number of replicate destination columns per source well. Must be >= 2 (1 is not replication). "
        "Set only when the user explicitly says 'in triplicate' (3), 'in duplicate' (2), etc. "
        "Leave null if no replication is mentioned. "
        "Example: 'test each sample in triplicate' → replicates: 3. "
        "Example: 'transfer to column 11 and 12' → replicates: null (this is just two destinations, not replication)."
    ))

    @model_validator(mode='after')
    def coerce_replicates(self) -> 'ExtractedStep':
        """Normalize `replicates` so that "no replication" is uniformly None.

        Pre:    ExtractedStep instance with all fields populated by Pydantic;
                `self.replicates` is None or any int (the field has no `ge=`
                bound, so 0 and negative ints reach this validator).

        Post:   If `self.replicates` is None: unchanged.
                If `self.replicates >= 2`: unchanged (real replication count).
                If `self.replicates < 2` (i.e. 1, 0, or negative): coerced
                to None — 1 replicate means no replication, and 0/negative
                are silently normalized rather than rejected (lenient
                normalization for hallucinated LLM output). Returns self.

        Side effects: May mutate `self.replicates` (sets to None when < 2).

        Raises: Never.
        """
        if self.replicates is not None and self.replicates < 2:
            self.replicates = None
        return self
    tip_strategy: Optional[Literal["new_tip_each", "same_tip", "unspecified"]] = None
    pipette_hint: Optional[Literal["p20", "p300", "p1000"]] = Field(None, description=(
        "Set only if the user explicitly named a pipette. Do not infer from volumes or context."
    ))
    note: Optional[str] = Field(None, description=(
        "Additional context from the instruction, verbatim. Do not summarize or paraphrase."
    ))



class WellContents(BaseModel):
    """Initial contents of a well/tube before the protocol starts."""
    labware: str = Field(..., description=(
        "How the user referred to this labware. Copy their wording exactly. "
        "Do not translate to config labels or load names."
    ))
    well: WellName = Field(..., description=(
        "Well position matching pattern [A-P][1-24]. Examples: 'A1', 'B2', 'H12'."
    ))
    substance: str = Field(..., description=(
        "Copy the substance name as the user wrote it. Do not normalize or abbreviate."
    ))
    volume_ul: Optional[float] = Field(None, description=(
        "Volume in uL if the user stated it. Do not infer — leave null if not stated."
    ))


class LabwarePrefill(BaseModel):
    """Declares that an entire labware starts pre-filled with a uniform substance and volume.

    Use this when the instruction says something like "plate contains 100uL media per well"
    rather than listing every individual well. The well state tracker expands this into
    per-well state during initialization.
    """
    labware: str = Field(..., description=(
        "How the user referred to this labware. Copy their wording exactly. "
        "Example: 'cell plate', 'assay plate'. Same convention as LocationRef.description."
    ))
    substance: str = Field(..., description=(
        "What the labware is pre-filled with. Copy the user's wording."
    ))
    volume_ul: float = Field(..., description=(
        "Volume per well in uL. Must be explicitly stated in the instruction."
    ))


class ProtocolSpec(BaseModel):
    """The structured intermediate representation of the user's intent.

    Contains both the LLM's chain-of-thought reasoning (visible, loggable)
    and the structured specification (validatable, constrains generation).
    """
    protocol_type: Optional[str] = Field(None, description="High-level type: 'serial_dilution', 'pcr_setup', 'bradford_assay'")
    summary: str = Field(..., description="One-sentence summary of what the user wants")
    reasoning: str = Field("", description="The LLM's chain-of-thought reasoning")
    steps: List[ExtractedStep] = Field(..., min_length=1)
    explicit_volumes: List[float] = Field(default_factory=list, description="All volumes found in instruction text via regex")
    initial_contents: List[WellContents] = Field(default_factory=list, description="What's in wells/tubes before the protocol starts")
    prefilled_labware: List[LabwarePrefill] = Field(default_factory=list, description="Labware that starts uniformly pre-filled (e.g. 'cell plate has 100uL media per well')")

    @model_validator(mode='after')
    def validate_step_ordering(self) -> 'ProtocolSpec':
        """Require step.order values to form a permutation of {1, 2, ..., N}.

        Pre:    ProtocolSpec instance with `self.steps` populated; each
                `step.order` is an int (Pydantic's `ge=1` on ExtractedStep
                already guarantees order >= 1).

        Post:   If `sorted([s.order for s in self.steps])` equals
                `[1, 2, ..., len(steps)]`: returns self unchanged.
                Otherwise raises ValueError. Permutations are allowed
                (e.g. orders `[3, 1, 2]` pass — only the multiset matters,
                not the list-position order). Forbidden cases include
                gaps (`[1, 3]`), duplicates (`[1, 1, 2]`), 0-based
                (`[0, 1, 2]`), and not-starting-at-1 (`[2, 3, 4]`).

        Side effects: None. Read-only validation.

        Raises: ValueError with the offending order list embedded in the
                message, formatted as
                "Step orders must be consecutive 1..N, got [list]".
        """
        orders = [s.order for s in self.steps]
        if sorted(orders) != list(range(1, len(orders) + 1)):
            raise ValueError(f"Step orders must be consecutive 1..N, got {orders}")
        return self


class CompleteProtocolSpec(ProtocolSpec):
    """A ProtocolSpec that has been validated for completeness.

    Enforces that all fields required for code generation are present:
      - Liquid-handling actions must have a volume
      - Transfer-like actions must have source and destination

    Use ProtocolSpec for extraction output (gaps allowed).
    Promote to CompleteProtocolSpec after gap-filling succeeds.
    """

    @model_validator(mode='after')
    def validate_completeness(self) -> 'CompleteProtocolSpec':
        """Require every step to carry the fields codegen consumes for its action.

        Per ADR-0006, this contract MATCHES the type's name: every action
        defined in ActionType has an explicit per-action rule based on what
        the schema builder actually reads. Steps not listed in any rule
        (engage_magnets, disengage_magnets, deactivate) impose no
        per-action requirements — those module commands take only their
        target module label, not a parameter value.

        Pre:    CompleteProtocolSpec instance with `self.steps` populated.
                `validate_step_ordering` (inherited from ProtocolSpec) has
                already passed.

        Post:   For each step, action-specific completeness rules apply:
                  * Liquid-handling actions {transfer, distribute, consolidate,
                    aspirate, dispense, mix, serial_dilution} require
                    `step.volume` to be non-None.
                  * Transfer-like actions {transfer, distribute,
                    serial_dilution, consolidate} additionally require
                    BOTH `step.source` and `step.destination` to be non-None.
                  * `set_temperature` and `wait_for_temperature` require
                    `step.temperature` to be non-None.
                  * `delay` requires `step.duration` to be non-None.
                  * `pause` requires `step.duration` OR `step.note`
                    (one or the other — timed pauses populate duration,
                    user-driven pauses populate note explaining the
                    user action).
                  * `comment` requires `step.note` to be non-None.
                  * Other actions impose no per-action requirements.
                If every step satisfies its rules: returns self unchanged.
                Otherwise: collects ALL issues across ALL steps (does not
                short-circuit on the first), then raises a single ValueError
                whose message starts with "Spec is incomplete (N issue(s)):"
                followed by issues joined by "; ". When `step.substance`
                is set, liquid-action issue messages include a `for 'X'`
                substance hint where X is `step.substance.value`.

        Side effects: None. Read-only validation.

        Raises: ValueError listing every incomplete-field issue across all
                steps (single raise; never multiple).
        """
        errors = []
        liquid_actions = {"transfer", "distribute", "consolidate", "aspirate",
                          "dispense", "mix", "serial_dilution"}
        transfer_actions = {"transfer", "distribute", "serial_dilution", "consolidate"}
        temperature_actions = {"set_temperature", "wait_for_temperature"}

        for step in self.steps:
            prefix = f"Step {step.order} ({step.action})"
            substance_hint = f" for '{step.substance.value}'" if step.substance else ""

            if step.action in liquid_actions and step.volume is None:
                errors.append(f"{prefix}: missing volume{substance_hint}")

            if step.action in transfer_actions:
                if step.source is None:
                    errors.append(f"{prefix}: no source for '{step.substance.value}' — add it to your config" if step.substance else f"{prefix}: missing source location")
                if step.destination is None:
                    errors.append(f"{prefix}: missing destination location{substance_hint}")

            if step.action in temperature_actions and step.temperature is None:
                errors.append(f"{prefix}: missing temperature target")

            if step.action == "delay" and step.duration is None:
                errors.append(f"{prefix}: missing duration")

            if step.action == "pause" and step.duration is None and not step.note:
                errors.append(f"{prefix}: pause requires either duration (timed) or note (user-driven)")

            if step.action == "comment" and not step.note:
                errors.append(f"{prefix}: missing note")

        if errors:
            raise ValueError(
                f"Spec is incomplete ({len(errors)} issue(s)): " + "; ".join(errors)
            )

        return self
