"""
spec.py — The "science language" intermediate representation.

ProtocolSpec is what the LLM produces: structured steps with provenance
tracking that describe what the scientist wants in domain terms.

Every extracted value carries a Provenance tag saying where it came from
(instruction, config, domain_default, inferred) so downstream stages
can verify claims and route uncertain values for user confirmation.
"""

from dataclasses import dataclass
from typing import Annotated, Any, List, Optional, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from nl2protocol.constants import TRASH_LABEL, is_discard_description

from .provenance_models import (
    Provenance, ProvenanceBase, InstructionProvenance, InferredProvenance,
    validate_provenance, CompositionProvenance, VolumeBasis,
    ProvenancedDuration, ProvenancedString, ProvenancedTemperature, ProvenancedVolume,
)


WellName = Annotated[str, Field(pattern=r'^[A-P](1[0-9]|2[0-4]|[1-9])$')]

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
    description_provenance: Provenance = Field(..., description=(
        "How the labware DESCRIPTION (the user's wording for the labware) was determined. "
        "REQUIRED on every LocationRef. If the instruction names the labware (e.g. "
        "'tube rack'), use source='instruction' with cited_text='tube rack'. If the "
        "labware is implicit at this step (e.g. the instruction names it earlier and "
        "this step continues with it), use source='inferred' with positive_reasoning "
        "explaining the connection and why_not_in_instruction explaining what's missing "
        "at this step's clause."
    ))
    wells_provenance: Optional[Provenance] = Field(None, description=(
        "How the WELL(S) were determined. Separate from description_provenance because "
        "the labware label and the well positions are typically cited from different "
        "parts of the instruction (e.g. labware named in step 1, wells named in step 5). "
        "REQUIRED when any of well/wells/well_range is populated; null otherwise."
    ))
    resolved_label_provenance: Optional[Provenance] = Field(None, description=(
        "How the resolved_label was picked from the lab config. Distinct from "
        "`description_provenance` (which is about how the user described the location). "
        "Filled by the labware resolver when it picks a config label; left null "
        "during extraction. Carries the resolver's positive_reasoning / "
        "why_not_in_instruction so the IndependentReviewSuggester can verify "
        "the pick — same reviewer machinery as inferred spec values per ADR-0009. "
        "Subject to the same review_status lifecycle (reviewed_agree / "
        "reviewed_disagree / user_confirmed / user_edited)."
    ))
    prior_revisions: List["LocationRef"] = Field(default_factory=list, description=(
        "Append-only history of prior LocationRef states. Each entry is a "
        "full snapshot of the labware reference (description, well/wells/"
        "well_range, resolved_label, and all three provenances) taken just "
        "before a write replaced them. Oldest first. Each LocationRef sub-"
        "field evolves on the SAME chain — a write that changes both "
        "wells and description shows up as one revision, not two, "
        "preserving the temporal coupling. The head (this object) owns "
        "the chain; entries themselves carry empty `prior_revisions`."
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

    @model_validator(mode='before')
    @classmethod
    def _strip_sentinel_well_strings(cls, data):
        """Normalize sentinel strings on `well` / `wells` to None before regex validation.

        Pre:    `data` is the raw input passed to LocationRef construction
                (dict from JSON, or already-constructed model on revalidation).

        Post:   When `data` is a dict: any `well` value whose case-folded /
                whitespace-stripped form is in the sentinel set
                {"unknown", "unspecified", "n/a", "na", "none", "null",
                "?", ""} is replaced with None. For `wells` (a list),
                sentinel entries are filtered out; an empty filtered list
                becomes None. `well_range`, `description`, and every
                provenance field are untouched. Non-dict input is returned
                unchanged so revalidation passes through.

        Why: The extractor LLM occasionally emits "unknown" as a well
        fallback when uncertain. Without this normalization the regex
        rejects it with a ValidationError; with it, the field becomes
        null and the downstream gap-resolution stage surfaces a missing-
        value gap (the desired behavior). Runs in mode='before' so the
        sentinel never reaches the pattern check.
        """
        if not isinstance(data, dict):
            return data
        sentinels = {"unknown", "unspecified", "n/a", "na", "none", "null", "?", ""}

        def _is_sentinel(v):
            return isinstance(v, str) and v.strip().lower() in sentinels

        well = data.get("well")
        if _is_sentinel(well):
            data["well"] = None
        wells = data.get("wells")
        if isinstance(wells, list):
            filtered = [w for w in wells if not _is_sentinel(w)]
            data["wells"] = filtered if filtered else None
        return data

    @model_validator(mode='after')
    def require_wells_provenance_when_wells_present(self) -> 'LocationRef':
        """Wells must carry provenance when any well/wells/well_range is set.

        Pre:    LocationRef instance after Pydantic field validation.

        Post:   When at least one of well, wells, well_range is non-null,
                wells_provenance must be non-null. When all three are null
                (labware-only ref, e.g. a temperature module referenced by
                name with no well position), wells_provenance may stay null.
                Returns self unchanged in both valid cases.

        Raises: ValueError when a well-bearing LocationRef has
                wells_provenance=None.
        """
        has_wells = self.well or self.wells or self.well_range
        if has_wells and self.wells_provenance is None:
            raise ValueError(
                "LocationRef.wells_provenance is required when "
                "well/wells/well_range is populated. Got "
                f"well={self.well!r}, wells={self.wells!r}, "
                f"well_range={self.well_range!r}."
            )
        return self


class PostAction(BaseModel):
    """Post-transfer action like mixing."""
    action: Literal["mix", "blow_out", "touch_tip"]
    repetitions: Optional[int] = Field(None, description=(
        "Number of mix cycles. Set only if the user specified a count. Do not infer a default — leave null."
    ))
    repetitions_provenance: Optional[Provenance] = Field(None, description=(
        "How the mix-cycle count was determined. Required when repetitions is "
        "non-null AND came from the instruction: source='instruction' with "
        "cited_text the verbatim count phrase (e.g. 'mix 5 times'). Leave null "
        "when repetitions is null; the gap-resolver stamps inferred/"
        "domain_default when a suggester fills it."
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


# ADR-0015 — action-field pruning
#
# `ExtractedStep` is a one-class-fits-all model the LLM fills. The final
# command layer (`nl2protocol/models/schema.py`) has a tight per-action
# class for each ActionType. The LLM regularly fills fields the action
# doesn't consume (e.g. `destination` on `set_temperature`). Pydantic
# accepts them; downstream consumers walk the fields polymorphically and
# treat the LLM-imagined values as real (the labware matcher surfaces
# them as phantom rows in the assignments modal). `_ACTION_KEEPS` names
# the fields each action keeps; the `prune_irrelevant_fields_by_action`
# validator on `ExtractedStep` nulls everything outside that set and
# records the scrubbed value in `ExtractedStep.pruned_fields` for later
# analysis. The matrix is derived from the schema-layer command classes
# plus the fields `extraction/schema_builder.py` actually reads off an
# `ExtractedStep` when building each command.
_PRUNABLE_FIELDS: frozenset = frozenset({
    "substance", "volume", "temperature", "duration",
    "source", "destination", "post_actions", "replicates", "note",
    "repetitions",
    # Sibling provenance is pruned with its count so it can't survive orphaned.
    "repetitions_provenance", "replicates_provenance",
})

_ACTION_KEEPS: dict = {
    "transfer":             {"volume", "substance", "source", "destination",
                              "post_actions", "replicates", "replicates_provenance"},
    "distribute":           {"volume", "substance", "source", "destination",
                              "post_actions", "replicates", "replicates_provenance"},
    "consolidate":          {"volume", "substance", "source", "destination",
                              "post_actions", "replicates", "replicates_provenance"},
    "serial_dilution":      {"volume", "substance", "source", "destination",
                              "post_actions", "replicates", "replicates_provenance",
                              "repetitions", "repetitions_provenance"},
    "mix":                  {"volume", "substance", "destination",
                              "repetitions", "repetitions_provenance"},
    "aspirate":             {"volume", "substance", "source"},
    "dispense":             {"volume", "substance", "destination"},
    "blow_out":             {"destination"},
    "touch_tip":            {"destination"},
    "delay":                {"duration", "note"},
    "pause":                {"duration", "note", "substance"},
    "comment":              {"note", "substance"},
    "set_temperature":      {"temperature"},
    "wait_for_temperature": {"temperature"},
    "engage_magnets":       set(),
    "disengage_magnets":    set(),
    "deactivate":           set(),
}


class PrunedFieldRecord(BaseModel):
    """One field-scrub record kept on `ExtractedStep.pruned_fields`.

    The pruner only fires on non-null fields outside the action's keep
    set, so every record represents an LLM-filled value that the action
    doesn't consume. Preserved for later analysis (state-log dumps,
    extraction-quality metrics) — not surfaced to the user.
    """
    field_name: str = Field(..., description=(
        "Which `ExtractedStep` field was scrubbed (e.g. 'destination', "
        "'volume'). Matches the attribute name on the model exactly."
    ))
    value: Any = Field(..., description=(
        "The original value the LLM filled, before pruning. Sub-models "
        "(LocationRef, ProvenancedVolume, ...) serialize via pydantic; "
        "primitives pass through. Used as audit data, never re-applied."
    ))


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
    repetitions: Optional[int] = Field(None, description=(
        "Number of mix cycles for a standalone 'mix' action (one up-and-down "
        "pipetting pass per cycle). Set ONLY if the user stated a count, e.g. "
        "'pipette up and down 10 times' → repetitions: 10. Leave null when no "
        "count is given — do not infer a default. Only consumed by the 'mix' "
        "action; for a mix attached to a transfer, use PostAction.repetitions "
        "instead."
    ))
    repetitions_provenance: Optional[Provenance] = Field(None, description=(
        "How the mix-cycle count was determined. Required when repetitions is "
        "non-null AND came from the instruction: source='instruction' with "
        "cited_text the verbatim phrase (e.g. 'up and down 10 times'). Leave "
        "null when repetitions is null; the gap-resolver stamps inferred/"
        "domain_default when a suggester fills the count."
    ))
    replicates: Optional[int] = Field(None, description=(
        "Number of replicate destination columns per source well. Must be >= 2 (1 is not replication). "
        "Set only when the user explicitly says 'in triplicate' (3), 'in duplicate' (2), etc. "
        "Leave null if no replication is mentioned. "
        "Example: 'test each sample in triplicate' → replicates: 3. "
        "Example: 'transfer to column 11 and 12' → replicates: null (this is just two destinations, not replication)."
    ))
    replicates_provenance: Optional[Provenance] = Field(None, description=(
        "How the replicate count was determined. Required when replicates is "
        "non-null AND came from the instruction: source='instruction' with "
        "cited_text the verbatim phrase (e.g. 'in triplicate'). Leave null when "
        "replicates is null."
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

        Side effects: May mutate `self.replicates` (sets to None when < 2) and
                clears `self.replicates_provenance` alongside it, so a count and
                its provenance never disagree.

        Raises: Never.
        """
        if self.replicates is not None and self.replicates < 2:
            self.replicates = None
            self.replicates_provenance = None
        return self
    tip_strategy: Optional[Literal["new_tip_each", "same_tip", "unspecified"]] = None
    pipette_hint: Optional[Literal["p20", "p300", "p1000"]] = Field(None, description=(
        "Set only if the user explicitly named a pipette. Do not infer from volumes or context."
    ))
    note: Optional[str] = Field(None, description=(
        "Additional context from the instruction, verbatim. Do not summarize or paraphrase."
    ))
    pruned_fields: List[PrunedFieldRecord] = Field(default_factory=list, description=(
        "Append-only audit log of fields the pruner scrubbed because the "
        "action does not consume them (per ADR-0015's _ACTION_KEEPS matrix). "
        "Populated automatically by `prune_irrelevant_fields_by_action`. "
        "Empty list for clean LLM output; non-empty when the LLM filled a "
        "field outside the action's keep-set. Preserved in state-log dumps "
        "for later extraction-quality analysis; not surfaced to the user."
    ))

    @model_validator(mode='after')
    def prune_irrelevant_fields_by_action(self) -> 'ExtractedStep':
        """Null any field the action does not consume; record the scrub on
        `self.pruned_fields`. Single source of truth for the per-action
        keep-set is the module-level `_ACTION_KEEPS` dict (per ADR-0015).

        Pre:    ExtractedStep instance with all fields populated by Pydantic.
                `self.action` is a member of `ActionType`. `self.pruned_fields`
                is an empty list (the default — callers must not pre-populate it).
        Post:   For every field name in `_PRUNABLE_FIELDS` that is NOT in
                `_ACTION_KEEPS[self.action]`: if the current value is not
                None / not an empty list, a `PrunedFieldRecord(field_name,
                value)` is appended to `self.pruned_fields` and the attr
                is set to None. Fields in the keep-set are untouched. The
                method returns `self`. Other fields (order, action,
                composition_provenance, tip_strategy, pipette_hint) are
                never pruned because they aren't in `_PRUNABLE_FIELDS`.
        Side effects: Mutates the step instance (nulls scrubbed fields,
                appends to `pruned_fields`). Re-running on an already-
                pruned step is a no-op (every scrubbed field is None,
                falls through the non-null guard).
        Raises: Never. Unknown actions (shouldn't happen post-Literal
                validation) fall through with no scrub.
        """
        keep = _ACTION_KEEPS.get(self.action)
        if keep is None:
            return self
        for field_name in _PRUNABLE_FIELDS:
            if field_name in keep:
                continue
            value = getattr(self, field_name, None)
            if value is None or value == []:
                continue
            self.pruned_fields.append(
                PrunedFieldRecord(field_name=field_name, value=value)
            )
            setattr(self, field_name, None)
        return self



class WellContents(BaseModel):
    """Initial contents of a well/tube before the protocol starts."""
    labware: str = Field(..., description=(
        "How the user referred to this labware. Copy their wording exactly. "
        "Do not translate to config labels or load names."
    ))
    well: Optional[WellName] = Field(None, description=(
        "Well position matching pattern [A-P][1-24]. Leave null when the "
        "instruction names a labware containing a substance but does not "
        "name the well (e.g. 'the 100uL fragmented DNA sample' with no "
        "well coordinate). The InitialContentsWellDetector flags null "
        "entries as gaps and gap-resolution prompts the user (or, when a "
        "suggester is later added, proposes the first vacant well). "
        "Symmetric with WellContents.volume_ul. Examples: 'A1', 'B2', 'H12'."
    ))
    substance: str = Field(..., description=(
        "Copy the substance name as the user wrote it. Do not normalize or abbreviate."
    ))
    volume_ul: Optional[float] = Field(None, description=(
        "Volume in uL if the user stated it. Do not infer — leave null if not stated."
    ))
    volume_ul_provenance: Optional[Provenance] = Field(None, description=(
        "Required when volume_ul is non-null AND came from the instruction: "
        "set source='instruction' and cited_text to the verbatim substring(s) "
        "the volume came from (e.g., '50uL aliquots'). Leave null when "
        "volume_ul is null OR was filled in by a suggester. The IC confirmation "
        "modal surfaces cited_text as a per-row audit trail so users can "
        "cross-check the system's reading of their volume against their wording."
    ))
    prior_revisions: List["WellContents"] = Field(default_factory=list, description=(
        "Append-only history of prior WellContents states. Each entry is a "
        "full snapshot taken just before a write replaced any of labware / "
        "well / substance / volume_ul / volume_ul_provenance. Oldest first. "
        "The head owns the chain; entries themselves carry empty "
        "`prior_revisions`."
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
    volume_ul: Optional[float] = Field(None, description=(
        "Volume per well in uL. Leave null when the instruction names a "
        "prefilled labware without stating the per-well volume — the "
        "InitialContentsVolumeDetector flags null entries as gaps and "
        "the orchestrator's WellCapacitySuggester proposes a default "
        "(or the user types one in confirmation). Symmetric with "
        "WellContents.volume_ul, which has been Optional since the "
        "orchestrator landed."
    ))
    prior_revisions: List["LabwarePrefill"] = Field(default_factory=list, description=(
        "Append-only history of prior LabwarePrefill states. Each entry "
        "is a snapshot taken just before a write replaced any of "
        "labware / substance / volume_ul. Stage 2.5 labware-assignment "
        "rewrites this row's `labware` to a config key; the chain "
        "captures the user-facing description that was replaced."
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


class ProtocolSpec(BaseModel):
    """The structured intermediate representation of the user's intent.

    Contains both the LLM's chain-of-thought reasoning (visible, loggable)
    and the structured specification (validatable, constrains generation).
    """
    protocol_type: Optional[str] = Field(None, description="High-level type: 'serial_dilution', 'pcr_setup', 'bradford_assay'")
    summary: str = Field(..., description="One-sentence summary of what the user wants")
    reasoning: str = Field("", description="The LLM's chain-of-thought reasoning")
    steps: List[ExtractedStep] = Field(..., min_length=1)
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
                  * `mix` and `serial_dilution` additionally require
                    `step.repetitions` (the mix cycle count) to be non-None.
                  * Transfer-like actions {transfer, distribute,
                    serial_dilution, consolidate} additionally require
                    BOTH `step.source` and `step.destination` to be non-None.
                  * Single-location actions {mix, aspirate, dispense,
                    touch_tip} require at least ONE of `step.source` /
                    `step.destination` to be non-None (they act in place),
                    and at least one present ref to carry a
                    well/wells/well_range. Missing location → "missing
                    location"; present-but-welless → "missing well(s)".
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
        errors = missing_fields(self)
        if errors:
            raise ValueError(
                f"Spec is incomplete ({len(errors)} issue(s)): "
                + "; ".join(mf.message() for mf in errors)
            )

        return self


# ============================================================================
# COMPLETENESS WALK (structured)
# ============================================================================

@dataclass(frozen=True)
class MissingField:
    """A structured completeness deficiency: which spec field is missing, and why.

    `field_path` is the dotted spec address the gap-resolution apply layer
    writes to (e.g. "steps[12].destination"); `detail` is the human phrase
    rendered after the "Step N (action): " prefix. The gap detector reads
    `field_path` directly — no message re-parsing — so the producer (this
    walk, which knows the field) is the single source of truth for the
    address, not a downstream keyword table.
    """

    step_order: Optional[int]
    action: Optional[str]
    field_path: str
    detail: str
    kind: str = "missing"
    severity: str = "blocker"

    def message(self) -> str:
        """Render the human-readable error string for display / the raise."""
        return f"Step {self.step_order} ({self.action}): {self.detail}"


def missing_fields(spec) -> List[MissingField]:
    """Return one MissingField per action-specific completeness deficiency.

    Pure walk over `spec.steps`; the single source of truth for both the
    `CompleteProtocolSpec` construction guard (rendered to a ValueError) and
    the gap detector (which reads `field_path` directly). Every issue carries
    a concrete `field_path` — single-location actions ("mix" et al.) route
    their location to `destination` by convention rather than an unmappable
    placeholder.
    """
    out: List[MissingField] = []

    def add(step, suffix: str, detail: str) -> None:
        out.append(MissingField(
            step_order=step.order,
            action=step.action,
            field_path=f"steps[{step.order - 1}]{suffix}",
            detail=detail,
        ))

    liquid_actions = {"transfer", "distribute", "consolidate", "aspirate",
                      "dispense", "mix", "serial_dilution"}
    transfer_actions = {"transfer", "distribute", "serial_dilution", "consolidate"}
    single_location_actions = {"mix", "aspirate", "dispense", "touch_tip"}
    temperature_actions = {"set_temperature", "wait_for_temperature"}

    for step in spec.steps:
        substance_hint = f" for '{step.substance.value}'" if step.substance else ""

        if step.action in liquid_actions and step.volume is None:
            add(step, ".volume", f"missing volume{substance_hint}")

        # Mix cycle count: standalone 'mix' and the per-transfer mixing in
        # 'serial_dilution' both need a count. Left null by extraction (the
        # model says "do not infer"); surfaced here so the user controls it
        # via the gap modal instead of codegen silently using the default.
        if step.action in {"mix", "serial_dilution"}:
            if step.repetitions is None:
                add(step, ".repetitions", "missing mix cycle count")
            elif step.repetitions <= 0:
                add(step, ".repetitions", "mix cycle count must be at least 1")

        if step.action in transfer_actions:
            if step.source is None:
                add(step, ".source",
                    f"no source for '{step.substance.value}' — add it to your config"
                    if step.substance else "missing source location")
            if step.destination is None:
                add(step, ".destination", f"missing destination location{substance_hint}")
            # A populated LocationRef with no well/wells/well_range is
            # half-specified — the constraint checker can't validate absent
            # wells (only out-of-range ones), so without this check the spec
            # passes completeness with an unusable location.
            for role in ("source", "destination"):
                ref = getattr(step, role, None)
                if ref is None:
                    continue
                # A discard destination (waste/trash) needs no well — the
                # OT-2 fixed trash is a single-well sink resolved off-config.
                if role == "destination" and (
                    ref.resolved_label == TRASH_LABEL
                    or is_discard_description(ref.description)
                ):
                    continue
                if ref.well is None and not ref.wells and ref.well_range is None:
                    add(step, f".{role}.wells", f"missing {role} well(s){substance_hint}")

        if step.action in single_location_actions:
            if step.source is None and step.destination is None:
                # Single-location actions act in place; by convention the
                # location lives in `destination`.
                add(step, ".destination", f"missing location{substance_hint}")
            elif not any(
                ref is not None and (ref.well is not None or ref.wells or ref.well_range is not None)
                for ref in (step.source, step.destination)
            ):
                role = "source" if step.source is not None else "destination"
                add(step, f".{role}.wells", f"missing well(s){substance_hint}")

        if step.action in temperature_actions and step.temperature is None:
            add(step, ".temperature", "missing temperature target")

        if step.action == "delay" and step.duration is None:
            add(step, ".duration", "missing duration")

        if step.action == "pause" and step.duration is None and not step.note:
            add(step, ".duration|.note",
                "pause requires either duration (timed) or note (user-driven)")

        if step.action == "comment" and not step.note:
            add(step, ".note", "missing note")

    return out


# ============================================================================
# REVISION HISTORY HELPER
# ============================================================================

def push_revision(field, **new_state) -> None:
    """Snapshot the current state of `field` into its `prior_revisions`,
    then mutate the head fields to `new_state`.

    Pre:    `field` is a ProvenancedVolume / ProvenancedDuration /
            ProvenancedTemperature / ProvenancedString / LocationRef /
            WellContents instance — any type that exposes a
            `prior_revisions: List[Self]` field. `new_state` carries the
            fields to overwrite on the head (e.g. `value=15.0,
            provenance=Provenance(...)`). Empty `new_state` is permitted
            and produces a snapshot-only revision (a no-op write that
            still records the prior state).

    Post:   `field.prior_revisions` has one new entry appended at the
            end: a deep copy of `field`'s state at call time, with that
            entry's own `prior_revisions` set to `[]`. The head's named
            fields are updated to the values in `new_state`. Other head
            fields are unchanged. Order in `prior_revisions` is oldest-
            first; index 0 is the first state ever pushed (typically the
            extractor's output).

    Side effects: Mutates `field` in place. Deep-copies the head's state
            before mutation, so the snapshot is safe against subsequent
            in-place changes to mutable sub-objects (e.g. a Provenance
            instance shared between the head and the snapshot would
            otherwise be a foot-gun).

    Raises: pydantic.ValidationError if any of `new_state` violates the
            field's own validators (e.g. setting `value=-1` on a
            ProvenancedVolume with `gt=0`). The snapshot has already
            been pushed when the head mutation fails — callers
            mutating-then-recovering should treat this as the head
            being in an indeterminate state.
    """
    snapshot = field.model_copy(update={"prior_revisions": []}, deep=True)
    field.prior_revisions.append(snapshot)
    for k, v in new_state.items():
        setattr(field, k, v)


def replace_with_history_preserved(old_field, new_field):
    """Return a copy of `new_field` whose `prior_revisions` carries
    `old_field`'s history plus a snapshot of `old_field`'s current state.

    Use this when an apply path needs to swap one tracked field for a
    freshly-constructed one (typical pattern: a suggester emits a
    complete replacement object). A plain `setattr` would drop
    `old_field.prior_revisions` on the floor. This helper preserves
    the chain instead, appending the old field's pre-replacement
    state to it.

    Pre:    `old_field` and `new_field` are both instances of the same
            type (or at least both expose a `prior_revisions: List[...]`
            attribute). When either lacks `prior_revisions`, returns
            `new_field` unchanged — caller can still setattr it.

    Post:   Returns a new instance equivalent to `new_field` except that
            its `prior_revisions` equals
            `list(old_field.prior_revisions) + [snapshot_of_old_field]`,
            where the appended snapshot itself has empty
            `prior_revisions`. `new_field`'s own `prior_revisions`
            (typically empty when fresh from a suggester) is REPLACED,
            not extended — the contract is that the resulting head owns
            the chain that came from the predecessor.

    Side effects: None. Both inputs are left unchanged; a new model
            instance is returned for the caller to assign.
    """
    if not hasattr(old_field, "prior_revisions") or not hasattr(new_field, "prior_revisions"):
        return new_field
    snapshot = old_field.model_copy(update={"prior_revisions": []}, deep=True)
    return new_field.model_copy(update={
        "prior_revisions": list(old_field.prior_revisions) + [snapshot],
    })
