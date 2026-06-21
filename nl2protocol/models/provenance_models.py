from typing import Annotated, Any, List, Optional, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from nl2protocol.constants import TRASH_LABEL, is_discard_description


WellName = Annotated[str, Field(pattern=r'^[A-P](1[0-9]|2[0-4]|[1-9])$')]

# ============================================================================
# PROVENANCE FOR PARMETERS
# ============================================================================

ReviewStatus = Literal[
    "original",
    "reviewed_agree",
    "reviewed_disagree",
    "user_confirmed",
    "user_edited",
    "user_accepted_suggestion",
    "user_skipped",
    "user_overrode_fabrication",
]


class ProvenanceBase(BaseModel):
    """Shared base for the source-discriminated Provenance union.

    Holds the fields common to both members — `confidence` and the review
    lifecycle (`review_status` + `reviewer_objection`). The two review fields
    are `SkipJsonSchema`: present on the model for validation and storage but
    omitted from the JSON schema handed to the extractor LLM, which plays no
    part in the review lifecycle and must not emit them. They default to the
    lowest-trust state, so a freshly extracted value is always 'original'
    until a reviewer or user action stamps it.

    Not instantiated directly — construct `InstructionProvenance` /
    `InferredProvenance` (or call `make_provenance`). Use `isinstance(x,
    ProvenanceBase)` to test "is this any Provenance".
    """
    confidence: float = Field(..., ge=0.0, le=1.0, description=(
        "How confident this value is correct. "
        "1.0 = user literally wrote it. "
        "0.8 = standard protocol default, clearly that protocol. "
        "0.6 = reasonable inference from context. "
        "0.4 = plausible guess with weak support. "
        "Below 0.4 = not sure."
    ))
    review_status: SkipJsonSchema[ReviewStatus] = Field("original", description=(
        "Where this Provenance sits in the gap-resolver review lifecycle. "
        "'original'                  = just extracted or just suggested; not yet reviewed. "
        "'reviewed_agree'            = independent reviewer confirmed the claims. "
        "'reviewed_disagree'         = reviewer flagged a concern (see reviewer_objection). "
        "'user_confirmed'            = user saw the value and kept it as-is. "
        "'user_edited'               = user typed a new value. "
        "'user_accepted_suggestion'  = user took the suggester's value verbatim. "
        "'user_skipped'              = user explicitly skipped the gap (value remains original). "
        "'user_overrode_fabrication' = system flagged the value's cited_text as fabricated, "
        "                              the FabricationRetrySuggester couldn't resolve it, and "
        "                              the user chose to commit the value anyway (ADR-0012). "
        "                              Audit-visible flag: this value is ungrounded by the "
        "                              verifier but the user has accepted responsibility."
    ))
    reviewer_objection: SkipJsonSchema[Optional[str]] = Field(None, description=(
        "The independent reviewer's stated concern. REQUIRED when "
        "review_status == 'reviewed_disagree'; FORBIDDEN otherwise. Surfaces in the "
        "CLI prompt and HTML report so the user can see why the reviewer pushed back."
    ))

    @model_validator(mode='after')
    def require_reviewer_objection_iff_disagree(self) -> 'ProvenanceBase':
        """Enforce reviewer_objection ↔ review_status == 'reviewed_disagree'.

        Pre:    Pydantic-validated Provenance member instance.

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
                review state may carry an objection. Orthogonal to `source`,
                so it lives on the shared base rather than either member.

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


class InstructionProvenance(ProvenanceBase):
    """Provenance for a value the user literally wrote: grounded by citation.

    The citation IS the justification, so this member carries no reasoning
    fields — `positive_reasoning` / `why_not_in_instruction` simply do not
    exist here, which is how the old per-source field invariant is now
    enforced by the type rather than by a model validator.
    """
    source: Literal["instruction"] = Field(..., description=(
        "Where this value came from. 'instruction' = user literally wrote it; "
        "cited_text is required and is the sole justification."
    ))
    cited_text: List[str] = Field(..., min_length=1, description=(
        "A list of verbatim substrings from the instruction that ground this value. "
        "Non-empty list, each entry a non-empty string. "
        "Each entry must appear verbatim in the instruction (case-insensitive, "
        "whitespace-normalized). For numbers, at least one cited substring should "
        "contain the value (e.g., for value=100uL, cited_text might be ['100uL of buffer']). "
        "Use multiple entries when the supporting text is spread across the instruction "
        "(e.g., a wells list captured across four bullet points: "
        "['Plasmid A1 to cells B1', 'Plasmid A2 to cells B2', ...])."
    ))

    @field_validator('cited_text', mode='before')
    @classmethod
    def _normalize_cited_text(cls, value):
        """Wrap a bare-string `cited_text` into a one-element list.

        Raises: Never.
        """
        if value is None:
            return None
        if isinstance(value, str):
            return [value]
        return value


class InferredProvenance(ProvenanceBase):
    """Provenance for a value not lifted verbatim: grounded by reasoning.

    Covers source 'domain_default' (standard practice for a named protocol)
    and 'inferred' (reasoning or guess with no direct support). Carries
    `positive_reasoning` (required) and `why_not_in_instruction` (optional);
    it has no `cited_text` field, so a non-instruction value cannot ground in
    user-quoted text.
    """
    source: Literal["domain_default", "inferred", "initial_state"] = Field(..., description=(
        "Where this value came from. "
        "'domain_default' = standard practice for a named protocol. "
        "'inferred' = reasoning or guess with no direct support. "
        "'initial_state' = SYSTEM-ONLY: read from the operator's uploaded "
        "initial-state map (e.g. a substance taken from the source well's "
        "known contents). NEVER emit this during extraction — only the "
        "pipeline sets it. All require positive_reasoning."
    ))
    positive_reasoning: str = Field(..., description=(
        "One sentence answering: 'why is THIS the right value?'. "
        "For 'domain_default': cite the protocol and standard practice. "
        "For 'inferred': state the reasoning chain that yields this specific value."
    ))
    why_not_in_instruction: Optional[str] = Field(None, description=(
        "One sentence answering: 'why did I have to infer this instead of cite it?'. "
        "Examples: 'instruction names the substance but not its source labware — "
        "looked up via config' / 'instruction does not specify temperature for wait — "
        "inherited from prior set_temperature step'. Strongly recommended (but not "
        "schema-enforced) when an instruction exists; the extractor and suggesters "
        "enforce this at their boundary."
    ))

Provenance = Annotated[
    Union[InstructionProvenance, InferredProvenance],
    Field(discriminator="source"),
]
_PROVENANCE_ADAPTER: TypeAdapter = TypeAdapter(Provenance)


def validate_provenance(data) -> ProvenanceBase:
    """Validate a dict / JSON value into the correct Provenance union member.

    Replaces `Provenance.model_validate(...)`; the discriminator on `source`
    routes to the matching member. Accepts the output of any member's
    `model_dump()`, so the stamp paths in gap_resolution can round-trip a
    Provenance through a dict to overwrite review-lifecycle fields.
    """
    return _PROVENANCE_ADAPTER.validate_python(data)


# ============================================================================
# PROVENANCE MODEL FOR STEP
# ============================================================================

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

class VolumeBasis(BaseModel):
    """Marks a volume as DERIVED from a well's current contents, resolved to a
    number at build time rather than guessed independently.

    Set when the instruction defines the amount by the well, not by a figure —
    "transfer the supernatant" (= all of the source well), "resuspend the beads"
    (= stir most of the destination well). The literal `value` on the carrying
    ProvenancedVolume is a fallback; `spec_to_schema` overwrites it with
    `fraction × (current tracked contents of the step's <location> well)`, which
    makes physically-coupled volumes consistent by construction.
    """
    kind: Literal["well_contents"] = Field("well_contents", description=(
        "Basis kind. Only 'well_contents' today — a fraction of the well's "
        "current volume."
    ))
    location: Literal["source", "destination"] = Field(..., description=(
        "Which of the step's LocationRefs names the well to read: 'source' for "
        "a removal (transfer the supernatant out of it), 'destination' for a "
        "mix that stirs the well it acts on."
    ))
    fraction: float = Field(1.0, gt=0, le=1.0, description=(
        "Scale on the well's current contents. 1.0 = all of it (removal); "
        "~0.8 = a resuspend mix that stays below the meniscus."
    ))


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
    basis: Optional[VolumeBasis] = Field(None, description=(
        "When set, this volume is derived at build time from a well's current "
        "contents (see VolumeBasis); `value` is a fallback used only if the "
        "well is empty/unknown at resolution time."
    ))
    prior_revisions: List["ProvenancedVolume"] = Field(default_factory=list, description=(
        "Append-only history of prior states. Each entry is a snapshot of "
        "(value, unit, exact, provenance) taken just before a write replaced "
        "them. Oldest first. The head (this object) owns the chain; entries "
        "themselves carry empty `prior_revisions`."
    ))

    @field_validator('prior_revisions')
    @classmethod
    def _no_nested_history(cls, v):
        if any(rev.prior_revisions for rev in v):
            raise ValueError(
                "prior_revisions entries must themselves have empty "
                "prior_revisions — only the head owns the chain"
            )
        return v


class ProvenancedDuration(BaseModel):
    """A time duration with provenance tracking."""
    value: float = Field(..., gt=0, description="Copy the user's number exactly.")
    unit: Literal["seconds", "minutes", "hours"]
    provenance: Provenance
    prior_revisions: List["ProvenancedDuration"] = Field(default_factory=list, description=(
        "Append-only history of prior states. Same shape and invariant as "
        "ProvenancedVolume.prior_revisions."
    ))

    @field_validator('prior_revisions')
    @classmethod
    def _no_nested_history(cls, v):
        if any(rev.prior_revisions for rev in v):
            raise ValueError(
                "prior_revisions entries must themselves have empty "
                "prior_revisions — only the head owns the chain"
            )
        return v


class ProvenancedTemperature(BaseModel):
    """A temperature in Celsius with provenance tracking."""
    value: float = Field(..., description="Temperature in degrees Celsius.")
    provenance: Provenance
    prior_revisions: List["ProvenancedTemperature"] = Field(default_factory=list, description=(
        "Append-only history of prior states. Same shape and invariant as "
        "ProvenancedVolume.prior_revisions."
    ))

    @field_validator('prior_revisions')
    @classmethod
    def _no_nested_history(cls, v):
        if any(rev.prior_revisions for rev in v):
            raise ValueError(
                "prior_revisions entries must themselves have empty "
                "prior_revisions — only the head owns the chain"
            )
        return v


class ProvenancedString(BaseModel):
    """A string value with provenance tracking."""
    value: str
    provenance: Provenance
    prior_revisions: List["ProvenancedString"] = Field(default_factory=list, description=(
        "Append-only history of prior states. Same shape and invariant as "
        "ProvenancedVolume.prior_revisions."
    ))

    @field_validator('prior_revisions')
    @classmethod
    def _no_nested_history(cls, v):
        if any(rev.prior_revisions for rev in v):
            raise ValueError(
                "prior_revisions entries must themselves have empty "
                "prior_revisions — only the head owns the chain"
            )
        return v
