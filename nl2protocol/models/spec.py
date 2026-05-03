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
    """Tracks where a single extracted value came from.

    source is the primary signal (categorical, verifiable by grounding checks).
    confidence is secondary (rank-usable for UX routing, not absolute-calibrated).
    """
    source: Literal["instruction", "config", "domain_default", "inferred"] = Field(..., description=(
        "Where this value came from. "
        "'instruction' = user literally wrote it. "
        "'config' = read from the lab config. "
        "'domain_default' = standard practice for a named protocol. "
        "'inferred' = reasoning or guess with no direct support."
    ))
    reason: str = Field(..., description=(
        "One sentence explaining why this value. "
        "For 'instruction': cite the phrase from the text. "
        "For 'config': cite the config key. "
        "For 'domain_default': cite the protocol and standard practice. "
        "For 'inferred': state the reasoning chain."
    ))
    confidence: float = Field(..., ge=0.0, le=1.0, description=(
        "How confident this value is correct. "
        "1.0 = user literally wrote it. "
        "0.8 = standard protocol default, clearly that protocol. "
        "0.6 = reasonable inference from context. "
        "0.4 = plausible guess with weak support. "
        "Below 0.4 = not sure."
    ))


class CompositionProvenance(BaseModel):
    """Tracks why a step exists — what reasoning justifies linking its parameters together.

    Unlike Provenance (single source for a single value), a step's existence
    is a synthesis: the user said 'Bradford assay' (instruction) + Bradford
    requires incubation (domain_default) + config has a temp module (config).
    Multiple sources combine through first-principle reasoning.
    """
    justification: str = Field(..., description=(
        "What reasoning connects these parameters into one step? "
        "Cite the instruction phrase, domain knowledge, and/or config constraints "
        "that justify this step's existence as a distinct action."
    ))
    grounding: List[Literal["instruction", "config", "domain_default"]] = Field(..., description=(
        "Which sources contributed to this step's existence. A step can be grounded "
        "in multiple sources simultaneously. "
        "Example: ['instruction', 'domain_default'] for a step the user implied via a named protocol."
    ))
    confidence: float = Field(..., ge=0.0, le=1.0, description=(
        "How confident this step should exist. "
        "1.0 = user explicitly described this exact step. "
        "0.8 = standard part of a named protocol the user invoked. "
        "0.5 = seems necessary but user didn't mention it."
    ))


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
    provenance: Optional[Provenance] = Field(None, description=(
        "How this location reference was determined. Covers the labware + well(s) as a unit."
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
        """Require every step to have all fields its action needs for codegen.

        Pre:    CompleteProtocolSpec instance with `self.steps` populated.
                `validate_step_ordering` (inherited from ProtocolSpec) has
                already passed.

        Post:   For each step, action-specific completeness rules apply:
                  * "Liquid" actions {transfer, distribute, consolidate,
                    aspirate, dispense, mix, serial_dilution} require
                    `step.volume` to be non-None.
                  * "Transfer-like" actions {transfer, distribute,
                    serial_dilution, consolidate} additionally require
                    BOTH `step.source` and `step.destination` to be non-None.
                  * Other actions (e.g. delay, set_temperature) impose no
                    completeness requirements.
                If every step satisfies its rules: returns self unchanged.
                Otherwise: collects ALL issues across ALL steps (does not
                short-circuit on the first), then raises a single ValueError
                whose message starts with "Spec is incomplete (N issue(s)):"
                followed by issues joined by "; ". When `step.substance`
                is set, the issue message includes a `for 'X'` substance
                hint where X is `step.substance.value`.

        Side effects: None. Read-only validation.

        Raises: ValueError listing every incomplete-field issue across all
                steps (single raise; never multiple).
        """
        errors = []
        liquid_actions = {"transfer", "distribute", "consolidate", "aspirate",
                          "dispense", "mix", "serial_dilution"}
        transfer_actions = {"transfer", "distribute", "serial_dilution", "consolidate"}

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

        if errors:
            raise ValueError(
                f"Spec is incomplete ({len(errors)} issue(s)): " + "; ".join(errors)
            )

        return self
