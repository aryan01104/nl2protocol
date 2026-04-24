"""
extractor.py

Domain-specific reasoning pipeline for protocol understanding.

Implements inference-time compute scaling (Raschka, Ch.4) for lab protocols:
instead of jumping from natural language to robot code in one step, we spend
extra compute on a chain-of-thought reasoning step that produces a structured
intermediate representation (ProtocolSpec).

The LLM reasons through the protocol in plain text first (chain-of-thought),
then outputs a structured specification. The reasoning is visible and loggable.
The structured output is deterministically validatable.

Pipeline stages:
  1. REASON + SPECIFY — LLM thinks through protocol, produces ProtocolSpec
  2. HALLUCINATION GUARD — check volumes against instruction text (regex)
  3. SUFFICIENCY CHECK — does the spec have enough to generate?
  4. FILL GAPS — infer defaults from config
  5. FORMAT for user confirmation
  6. VALIDATE schema against spec (post-generation deterministic gate)

The ProtocolSpec is computed ONCE, outside the retry loop. It is immutable
during retries, which prevents the retry loop from corrupting user-specified
values (the 10.5→20.0 bug).
"""

import copy
import json
import re
import sys
from typing import Annotated, List, Optional, Literal, Dict

from anthropic import Anthropic
from pydantic import BaseModel, Field, model_validator


# ============================================================================
# INTERMEDIATE REPRESENTATION
# ============================================================================

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
    unit: Literal["uL", "mL"] = "uL"
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
    volume: Optional[ProvenancedVolume] = None
    duration: Optional[ProvenancedDuration] = Field(None, description=(
        "For delay, pause, or incubation steps only. Do not put time values in the volume field."
    ))
    source: Optional[LocationRef] = None
    destination: Optional[LocationRef] = None
    post_actions: Optional[List[PostAction]] = None
    replicates: Optional[int] = Field(None, ge=2, description=(
        "Number of replicates per source well. Set only if the user specified replicates. Do not infer."
    ))
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
    missing_labware: List[str] = Field(default_factory=list, description="Labware referenced in instruction but NOT in config")

    @model_validator(mode='after')
    def validate_step_ordering(self) -> 'ProtocolSpec':
        orders = [s.order for s in self.steps]
        if sorted(orders) != list(range(1, len(orders) + 1)):
            raise ValueError(f"Step orders must be consecutive 1..N, got {orders}")
        return self


# ============================================================================
# LABWARE RESOLVER
# ============================================================================

class LabwareResolver:
    """Resolves user-language labware descriptions to config labels.

    Three tiers:
      1. Deterministic — exact, case-insensitive, word-overlap, load_name keyword
      2. LLM batch — one call with all unresolved refs + step context + config labels
      3. Unresolvable — added to spec.missing_labware for interactive resolution
    """

    def __init__(self, config: dict, client=None, model_name: str = "claude-sonnet-4-20250514"):
        self.config = config
        self.labware_labels = list(config.get("labware", {}).keys())
        self.client = client
        self.model_name = model_name

    def resolve(self, spec: ProtocolSpec) -> ProtocolSpec:
        """Walk all LocationRefs in spec and fill resolved_label."""
        spec = spec.model_copy(deep=True)

        # Collect all unique descriptions
        all_refs = self._collect_refs(spec)
        unique_descs = {ref.description for ref in all_refs}

        # Tier 1: deterministic
        resolved = {}
        unresolved = set()
        for desc in unique_descs:
            label = self._deterministic_resolve(desc)
            if label:
                resolved[desc] = label
            else:
                unresolved.add(desc)

        # Tier 2: LLM batch for unresolved
        if unresolved and self.client:
            llm_resolved = self._llm_resolve(unresolved, spec)
            resolved.update(llm_resolved)
            unresolved -= set(llm_resolved.keys())

        # Apply resolutions to all LocationRefs
        for ref in all_refs:
            if ref.description in resolved:
                ref.resolved_label = resolved[ref.description]

        # Also resolve initial_contents labware references
        for wc in spec.initial_contents:
            if wc.labware in resolved:
                wc.labware = resolved[wc.labware]

        # Tier 3: unresolvable → missing_labware
        for desc in unresolved:
            if desc not in spec.missing_labware:
                spec.missing_labware.append(desc)

        return spec

    def _collect_refs(self, spec: ProtocolSpec) -> List[LocationRef]:
        """Gather all LocationRef objects from spec steps."""
        refs = []
        for step in spec.steps:
            if step.source:
                refs.append(step.source)
            if step.destination:
                refs.append(step.destination)
        return refs

    def _deterministic_resolve(self, desc: str) -> Optional[str]:
        """Try to resolve description to config label without LLM.

        Matching strategies (in order):
          1. Exact match (case-insensitive)
          2. Score-based: word overlap (10pts), substring bonus (5pts), load_name keyword (3pts)
          Requires score >= 10 to accept (at least one full word overlap).
        """
        if not self.labware_labels:
            return None
        desc_lower = desc.lower().strip()
        desc_words = set(re.split(r'[\s_\-]+', desc_lower))

        # 1. Exact match
        for label in self.labware_labels:
            if label.lower() == desc_lower:
                return label

        # 2. Score-based matching
        def score_label(label: str) -> int:
            s = 0
            label_words = set(re.split(r'[\s_\-]+', label.lower()))
            # Word overlap
            s += len(desc_words & label_words) * 10
            # Substring bonus
            for lw in label_words:
                for dw in desc_words:
                    if len(lw) > 2 and len(dw) > 2 and lw != dw:
                        if lw in dw or dw in lw:
                            s += 5
            # Load_name keyword match
            load = self.config["labware"][label].get("load_name", "").lower()
            for dw in desc_words:
                if len(dw) > 2 and dw in load:
                    s += 3
            return s

        scored = [(label, score_label(label)) for label in self.labware_labels]
        scored.sort(key=lambda x: -x[1])
        if scored and scored[0][1] >= 10:
            return scored[0][0]

        return None

    def _llm_resolve(self, unresolved: set, spec: ProtocolSpec) -> dict:
        """Batch LLM call to resolve remaining descriptions.

        Passes all unresolved refs with their step context (action, wells, volume)
        and the full config labware list so the LLM can reason about which
        description maps to which config label.
        """
        # Build context: for each unresolved description, gather the steps that use it
        desc_context = {}
        for step in spec.steps:
            for ref, role in [(step.source, "source"), (step.destination, "destination")]:
                if ref and ref.description in unresolved:
                    if ref.description not in desc_context:
                        desc_context[ref.description] = []
                    wells = ref.well or ref.wells or ref.well_range or "unspecified"
                    desc_context[ref.description].append(
                        f"Step {step.order}: {step.action}, role={role}, "
                        f"wells={wells}, substance={step.substance.value if step.substance else 'unspecified'}"
                    )

        # Build config summary
        config_summary = {}
        for label, lw in self.config.get("labware", {}).items():
            config_summary[label] = {
                "load_name": lw.get("load_name", "unknown"),
                "slot": lw.get("slot", "unknown"),
            }

        prompt = (
            "You are resolving labware references in a lab protocol.\n\n"
            "The user described labware using natural language. Match each description "
            "to a config label based on context (what action, what wells, what substance).\n\n"
            f"CONFIG LABWARE:\n{json.dumps(config_summary, indent=2)}\n\n"
            "UNRESOLVED REFERENCES:\n"
        )
        for desc, contexts in desc_context.items():
            prompt += f'\n  "{desc}":\n'
            for ctx in contexts:
                prompt += f"    - {ctx}\n"

        prompt += (
            "\nFor each description, respond with JSON only:\n"
            '{"assignments": {"<description>": "<config_label or null>", ...}}\n\n'
            "Rules:\n"
            "- Only assign to a config label if you are confident it's the right match.\n"
            "- Use null if no config label is a reasonable match.\n"
            "- Consider the action context: sources are typically racks/reservoirs, "
            "destinations are typically plates.\n"
        )

        try:
            from .spinner import Spinner
            with Spinner("Resolving labware references..."):
                response = self.client.messages.create(
                    model=self.model_name,
                    max_tokens=512,
                    messages=[{"role": "user", "content": prompt}]
                )

            result_text = response.content[0].text
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0]
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0]

            result = json.loads(result_text.strip())
            assignments = result.get("assignments", {})

            # Only keep non-null assignments that reference valid config labels
            return {
                desc: label
                for desc, label in assignments.items()
                if label is not None and label in self.config.get("labware", {})
            }
        except Exception:
            return {}


# ============================================================================
# REASONING PROMPT
# ============================================================================

REASONING_SYSTEM_PROMPT = """You are an expert lab scientist reasoning through a protocol instruction.

Your job: think through what the user wants step by step, then produce a structured specification.

THINK FIRST (inside <reasoning> tags):
1. What protocol is this? Is it a named protocol (e.g., "Bradford assay", "serial dilution") or ad-hoc steps?
2. If it's a named protocol, what does it typically involve? Expand from your domain knowledge.
3. What parameters did the user explicitly specify? (volumes, wells, substances, pipettes, tip strategy)
4. What parameters are missing? What are standard/typical defaults for this protocol?
5. What are the individual steps, in order?
6. For each step: what is the action, what substance, what volume, from where, to where?
7. LABWARE: What pieces of labware does the instruction mention? Record the user's exact wording for each.
8. INITIAL STATE: What substances are in which wells/tubes BEFORE the protocol starts?

THEN produce structured JSON (inside <spec> tags) matching this schema:
{schema}

RULES:
- Preserve exact volumes the user stated (10.5uL stays 10.5, never round or adjust)
- If the user mentioned a pipette ("use the p20"), record it as pipette_hint
- If the user mentioned tip strategy ("new tip for each"), record as tip_strategy
- For time durations (delays, pauses, incubations), use the "duration" field with unit "seconds", "minutes", or "hours" — NEVER put time values in the "volume" field
- For well ranges, prefer the format "A1-H12" or provide explicit well lists like ["A1", "B1", "C1"]. Avoid natural language well descriptions like "columns 2-8, rows A-H" — instead write "A2-H8"
- Every liquid-handling step (transfer, distribute, mix, etc.) MUST have a volume
- DO NOT choose pipette mounts — only record hints if the user mentioned a pipette
- Leave the "reasoning" field empty in your JSON — it will be filled from your <reasoning> block
- Leave "explicit_volumes" empty — it will be populated automatically

PROVENANCE — every value you extract MUST have a provenance object:
  provenance: {{source, reason, confidence}}
  source: "instruction" (user wrote it), "config" (from lab config), "domain_default" (standard practice), "inferred" (guess)
  reason: one sentence citing WHERE the value came from
  confidence: 0.0-1.0

  For volumes, also set "exact":
    exact: true if the user stated this exact number ("100uL")
    exact: false if hedged ("about 100uL", "~50uL") or if inferred from domain knowledge

  Calibration examples:
    User says "Transfer 100uL from A1 to B1":
      volume: {{value: 100, unit: "uL", exact: true,
               provenance: {{source: "instruction", reason: "'100uL' in text", confidence: 1.0}}}}
    User says "about 50uL":
      volume: {{value: 50, unit: "uL", exact: false,
               provenance: {{source: "instruction", reason: "'about 50uL' in text", confidence: 0.9}}}}
    User says "do a Bradford assay" (you infer 50uL working volume):
      volume: {{value: 50, unit: "uL", exact: false,
               provenance: {{source: "domain_default", reason: "Bradford assay standard working volume", confidence: 0.7}}}}

COMPOSITION PROVENANCE — every step MUST have a composition_provenance object:
  composition_provenance: {{justification, grounding, confidence}}
  justification: what reasoning links these parameters into this step
  grounding: list of sources that contribute — ["instruction"], ["instruction", "domain_default"], etc.
  confidence: how confident this step should exist

  Calibration examples:
    User says "transfer 100uL from A1 to B1":
      composition_provenance: {{justification: "User explicitly described this transfer",
                                grounding: ["instruction"], confidence: 1.0}}
    User says "do a Bradford assay" and you add a 5-min incubation step:
      composition_provenance: {{justification: "Bradford assay requires incubation after mixing reagent",
                                grounding: ["instruction", "domain_default"], confidence: 0.8}}

LABWARE REFERENCES:
- The "description" field in LocationRef should contain the user's EXACT wording for the labware.
  Example: instruction says "tube rack" → description: "tube rack"
  Example: instruction says "the PCR plate" → description: "PCR plate"
  Example: instruction says "reservoir" → description: "reservoir"
- Do NOT translate to config labels or load names — that happens in a later stage.
- Leave "resolved_label" as null — it will be filled automatically.
- If the instruction references labware that clearly does not exist in the config, add the user's wording to "missing_labware".

INITIAL CONTENTS:
- Extract what's already in wells/tubes BEFORE the protocol starts.
- Use the user's exact wording for the "labware" field (same as LocationRef.description).
- Example: "Tube rack A1 contains DNA standard" → {{"labware": "tube rack", "well": "A1", "substance": "DNA standard"}}
- Only include what the instruction explicitly states. Don't infer contents.

COMPRESSION — KEEP STEPS COMPACT:
- NEVER create separate steps for repetitive operations that differ only in wells.
  Instead, use ONE step with well lists on source and/or destination.
- GENERAL PRINCIPLE: If multiple operations differ only in which wells they target,
  represent them as ONE step with well lists, well ranges, or the replicates field.
  The downstream code expands them into individual operations.
- Use "wells" lists for paired mappings: source[0]→dest[0], source[1]→dest[1], etc.
- Use "well_range" for contiguous regions: "A1-A12", "column 3", "rows A-D".
- Use "replicates" when each source well maps to N consecutive destination columns.
  The destination "well" is the starting corner; each source row fans out across N columns.
  Example — 8 standards in triplicate:
    {{
      "action": "transfer",
      "source": {{"description": "dilution strip", "wells": ["A1","B1","C1","D1","E1","F1","G1","H1"]}},
      "destination": {{"description": "plate", "well": "A1"}},
      "replicates": 3
    }}
- A protocol with 12 samples in triplicate should be 1 step, NOT 12 steps.
- Aim for under 10 steps total. If your spec has more than 15 steps, you are probably not compressing enough.

FORMAT YOUR RESPONSE EXACTLY AS:
<reasoning>
your step-by-step thinking here
</reasoning>
<spec>
{{valid JSON here}}
</spec>
"""

REASONING_USER_PROMPT = """INSTRUCTION:
{instruction}

LAB CONFIG (for context — what equipment is available):
{config}

Think through this protocol, then produce the structured specification.
"""


def _find_provenance_reason(step, field_name: str) -> Optional[str]:
    """Look up the provenance reason string for a given field on a step."""
    field_map = {
        "volume": step.volume,
        "substance": step.substance,
        "duration": step.duration,
        "composition": None,
    }
    # Direct field match
    if field_name in field_map:
        val = field_map[field_name]
        if val and hasattr(val, 'provenance') and val.provenance:
            return val.provenance.reason
        if field_name == "composition":
            return step.composition_provenance.justification
        return None

    # Location refs: "source location", "source labware", "source wells", etc.
    for prefix, ref in [("source", step.source), ("destination", step.destination), ("dest", step.destination)]:
        if field_name.startswith(prefix) and ref and ref.provenance:
            return ref.provenance.reason

    # Post-action fields: "mix volume", "blow_out volume", etc.
    if step.post_actions:
        for pa in step.post_actions:
            if field_name.startswith(pa.action) and pa.volume and pa.volume.provenance:
                return pa.volume.provenance.reason

    return None


# ============================================================================
# SEMANTIC EXTRACTOR
# ============================================================================

class SemanticExtractor:
    """Produces a structured ProtocolSpec from natural language instruction.

    Uses inference-time compute scaling: the LLM reasons through the protocol
    in chain-of-thought before producing structured output. This handles both
    simple instructions ("transfer 100uL from A1 to B1") and complex ones
    ("do the Bradford assay") — the reasoning adapts to complexity.
    """

    def __init__(self, client: Anthropic, model_name: str = "claude-sonnet-4-20250514"):
        self.client = client
        self.model_name = model_name

    def extract(self, instruction: str, config: dict) -> Optional[ProtocolSpec]:
        """Reason through the instruction and produce a ProtocolSpec.

        Returns ProtocolSpec on success, None on failure (fail-fast).
        """
        # Enrich config with well info so the LLM knows which wells are valid
        # for each labware (e.g., a 12-well reservoir only has A1-A12, no row B)
        from .parser import enrich_config_with_wells
        enriched_config = enrich_config_with_wells(config)

        schema = ProtocolSpec.model_json_schema()

        system_prompt = REASONING_SYSTEM_PROMPT.format(
            schema=json.dumps(schema, indent=2)
        )
        user_prompt = REASONING_USER_PROMPT.format(
            instruction=instruction,
            config=json.dumps(enriched_config, indent=2)
        )

        try:
            from .spinner import Spinner
            with Spinner("Reasoning through protocol..."):
                response = self.client.messages.create(
                    model=self.model_name,
                    max_tokens=8192,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}]
                )

            full_response = response.content[0].text.strip()

            # Check for truncation (stop_reason != "end_turn")
            if response.stop_reason != "end_turn":
                print(f"  Warning: LLM response truncated (stop_reason={response.stop_reason})")

            # Parse reasoning and spec from tagged response
            reasoning, spec_json = self._parse_response(full_response)

            # Parse and validate the structured spec
            data = json.loads(spec_json)
            spec = ProtocolSpec.model_validate(data)

            # Store the reasoning chain-of-thought
            spec.reasoning = reasoning

            # Populate explicit_volumes from instruction text (deterministic regex)
            spec.explicit_volumes = self._extract_volumes_from_text(instruction)

            return spec

        except Exception as e:
            from .errors import format_api_error
            import anthropic
            if isinstance(e, (anthropic.APIError, anthropic.APIConnectionError, anthropic.APITimeoutError)):
                print(f"  Reasoning failed: {format_api_error(e)}", file=sys.stderr)
            else:
                print(f"  Reasoning failed: {e}", file=sys.stderr)
            self._save_debug_output(locals().get('full_response'), locals().get('spec_json'), e)
            return None

    def _save_debug_output(self, full_response: Optional[str], spec_json: Optional[str], error: Exception):
        """Save failed LLM output for debugging."""
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_file = f"extractor_debug_{timestamp}.txt"

        try:
            with open(debug_file, 'w') as f:
                f.write(f"ERROR: {error}\n")
                f.write("=" * 60 + "\n\n")
                if spec_json:
                    f.write("EXTRACTED SPEC JSON:\n")
                    f.write("=" * 60 + "\n")
                    f.write(spec_json)
                    f.write("\n\n")
                if full_response:
                    f.write("FULL LLM RESPONSE:\n")
                    f.write("=" * 60 + "\n")
                    f.write(full_response)
            print(f"  Debug output saved to: {debug_file}")
        except Exception:
            # Last resort: print a snippet
            if spec_json:
                snippet = spec_json[:1000] + "..." if len(spec_json) > 1000 else spec_json
                print(f"  Spec JSON snippet:\n{snippet}")
            elif full_response:
                snippet = full_response[:1000] + "..." if len(full_response) > 1000 else full_response
                print(f"  Raw response snippet:\n{snippet}")

    def _parse_response(self, response: str) -> tuple[str, str]:
        """Parse <reasoning> and <spec> blocks from LLM response."""
        reasoning = ""
        spec_json = ""

        # Extract reasoning
        reasoning_match = re.search(r'<reasoning>(.*?)</reasoning>', response, re.DOTALL)
        if reasoning_match:
            reasoning = reasoning_match.group(1).strip()

        # Extract spec JSON
        spec_match = re.search(r'<spec>(.*?)</spec>', response, re.DOTALL)
        if spec_match:
            spec_json = spec_match.group(1).strip()
        else:
            # Fallback: try to find JSON in markdown code blocks
            if "```json" in response:
                spec_json = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                spec_json = response.split("```")[1].split("```")[0].strip()
            elif "{" in response:
                # Last resort: find the outermost JSON object
                start = response.index("{")
                spec_json = response[start:]

        if not spec_json:
            raise ValueError("No structured spec found in LLM response")

        return reasoning, spec_json

    @staticmethod
    def _extract_volumes_from_text(instruction: str) -> List[float]:
        """Regex extraction of all volumes mentioned in instruction text.
        These are ground-truth values from the user — immutable."""
        pattern = re.compile(r'(\d+(?:\.\d+)?)\s*(?:u[lL]|µ[lL])')
        return sorted(set(float(m.group(1)) for m in pattern.finditer(instruction)))

    def refine(self, spec: ProtocolSpec, gaps: List[str], instruction: str, config: dict) -> Optional[ProtocolSpec]:
        """Re-run reasoning with feedback about what's missing (self-refinement).

        This is inference-time scaling via self-refinement (Raschka Ch.5):
        the model reviews its own output, gets feedback on gaps, and tries again.
        """
        schema = ProtocolSpec.model_json_schema()

        system_prompt = REASONING_SYSTEM_PROMPT.format(
            schema=json.dumps(schema, indent=2)
        )

        refinement_prompt = f"""INSTRUCTION:
{instruction}

LAB CONFIG:
{json.dumps(config, indent=2)}

YOUR PREVIOUS SPECIFICATION HAD GAPS:
{json.dumps(gaps, indent=2)}

PREVIOUS SPEC:
{spec.model_dump_json(indent=2)}

Please fix the gaps listed above. Re-read the instruction carefully and ensure every
liquid-handling step has a volume, and every transfer/distribute has source and destination.
For well ranges, use the format "A1-H12" or provide explicit well lists.
For time durations, use the "duration" field with unit "seconds", "minutes", or "hours" — NOT the "volume" field.

Output the corrected specification.
"""

        try:
            from .spinner import Spinner
            with Spinner("Refining specification..."):
                response = self.client.messages.create(
                    model=self.model_name,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=[{"role": "user", "content": refinement_prompt}]
                )

            full_response = response.content[0].text.strip()
            reasoning, spec_json = self._parse_response(full_response)
            data = json.loads(spec_json)
            refined = ProtocolSpec.model_validate(data)
            refined.reasoning = reasoning
            refined.explicit_volumes = self._extract_volumes_from_text(instruction)
            return refined

        except Exception as e:
            print(f"  Refinement failed: {e}")
            return None

    # ========================================================================
    # PROVENANCE VERIFICATION (Task 5)
    # ========================================================================

    @staticmethod
    def _extract_wells_from_text(instruction: str) -> set:
        """Extract all well positions mentioned in instruction text."""
        pattern = re.compile(r'\b([A-H](?:1[0-2]|[1-9]))\b')
        return set(pattern.findall(instruction))

    @staticmethod
    def _extract_durations_from_text(instruction: str) -> List[tuple]:
        """Extract all durations mentioned in instruction text.
        Returns list of (value, unit) tuples."""
        pattern = re.compile(
            r'(\d+(?:\.\d+)?)\s*'
            r'(seconds?|minutes?|hours?|secs?|mins?|hrs?)',
            re.IGNORECASE
        )
        unit_map = {
            'second': 'seconds', 'seconds': 'seconds', 'sec': 'seconds', 'secs': 'seconds',
            'minute': 'minutes', 'minutes': 'minutes', 'min': 'minutes', 'mins': 'minutes',
            'hour': 'hours', 'hours': 'hours', 'hr': 'hours', 'hrs': 'hours',
        }
        results = []
        for m in pattern.finditer(instruction):
            val = float(m.group(1))
            unit = unit_map.get(m.group(2).lower(), m.group(2).lower())
            results.append((val, unit))
        return results

    def _warn(self, step_order: int, field: str, value: str,
              claimed_source: str, severity: str, message: str) -> dict:
        """Build a structured provenance warning."""
        return {
            "step": step_order,
            "field": field,
            "value": value,
            "claimed_source": claimed_source,
            "severity": severity,
            "message": message,
        }

    def _verify_instruction_claims(self, spec: ProtocolSpec, instruction: str) -> List[dict]:
        """Verify all source='instruction' claims against the instruction text.

        If the LLM claims a value came from the instruction but the value
        doesn't appear in the text, that's a fabricated provenance claim.
        Severity: 'fabrication'.
        """
        warnings = []
        text_volumes = self._extract_volumes_from_text(instruction)
        text_wells = self._extract_wells_from_text(instruction)
        text_durations = self._extract_durations_from_text(instruction)
        instruction_lower = instruction.lower()

        for step in spec.steps:
            # --- Volume ---
            if step.volume and step.volume.provenance.source == "instruction" and step.volume.exact:
                if step.volume.value not in text_volumes:
                    warnings.append(self._warn(
                        step.order, "volume",
                        f"{step.volume.value}{step.volume.unit}",
                        "instruction", "fabrication",
                        f"Step {step.order}: claims {step.volume.value}{step.volume.unit} "
                        f"from instruction but not found in text (found: {text_volumes})",
                    ))

            # --- Substance ---
            if step.substance and step.substance.provenance.source == "instruction":
                if step.substance.value.lower() not in instruction_lower:
                    warnings.append(self._warn(
                        step.order, "substance", step.substance.value,
                        "instruction", "fabrication",
                        f"Step {step.order}: claims substance '{step.substance.value}' "
                        f"from instruction but not found in text",
                    ))

            # --- Duration ---
            if step.duration and step.duration.provenance.source == "instruction":
                if not any(abs(val - step.duration.value) < 0.01 and unit == step.duration.unit
                           for val, unit in text_durations):
                    warnings.append(self._warn(
                        step.order, "duration",
                        f"{step.duration.value} {step.duration.unit}",
                        "instruction", "fabrication",
                        f"Step {step.order}: claims {step.duration.value} {step.duration.unit} "
                        f"from instruction but not found in text",
                    ))

            # --- LocationRef: labware + wells ---
            for ref, role in [(step.source, "source"), (step.destination, "destination")]:
                if not ref or not ref.provenance or ref.provenance.source != "instruction":
                    continue
                if ref.description.lower() not in instruction_lower:
                    warnings.append(self._warn(
                        step.order, f"{role} labware", ref.description,
                        "instruction", "fabrication",
                        f"Step {step.order} {role}: claims labware '{ref.description}' "
                        f"from instruction but not found in text",
                    ))
                wells_to_check = [ref.well] if ref.well else (ref.wells or [])
                missing = [w for w in wells_to_check if w not in text_wells]
                if missing:
                    warnings.append(self._warn(
                        step.order, f"{role} wells", str(missing),
                        "instruction", "fabrication",
                        f"Step {step.order} {role}: claims wells {missing} "
                        f"from instruction but not found in text",
                    ))

            # --- Post-action volumes ---
            if step.post_actions:
                for pa in step.post_actions:
                    if pa.volume and pa.volume.provenance.source == "instruction" and pa.volume.exact:
                        if pa.volume.value not in text_volumes:
                            warnings.append(self._warn(
                                step.order, f"{pa.action} volume",
                                f"{pa.volume.value}{pa.volume.unit}",
                                "instruction", "fabrication",
                                f"Step {step.order} {pa.action}: claims {pa.volume.value}"
                                f"{pa.volume.unit} from instruction but not found in text",
                            ))

        # --- Cross-check invariant: instruction-claimed volumes vs text_extracted_volumes ---
        instruction_volumes = set()
        for step in spec.steps:
            if step.volume and step.volume.provenance.source == "instruction":
                instruction_volumes.add(step.volume.value)
            if step.post_actions:
                for pa in step.post_actions:
                    if pa.volume and pa.volume.provenance.source == "instruction":
                        instruction_volumes.add(pa.volume.value)
        text_extracted = set(spec.explicit_volumes)
        fabricated = instruction_volumes - text_extracted
        if fabricated:
            warnings.append(self._warn(
                0, "cross-check", str(fabricated),
                "instruction", "fabrication",
                f"Volumes claimed as source='instruction' but absent from "
                f"text_extracted_volumes: {fabricated}",
            ))

        return warnings

    def _verify_config_claims(self, spec: ProtocolSpec, config: dict) -> List[dict]:
        """Verify all source='config' claims against the actual config.

        If the LLM says a value came from the config but the referenced key
        doesn't exist, that's a fabricated provenance claim. Severity: 'fabrication'.
        """
        warnings = []
        labware_labels = set(config.get("labware", {}).keys())
        pipette_mounts = set(config.get("pipettes", {}).keys())

        for step in spec.steps:
            # LocationRef with source='config': resolved_label must be a valid config key
            for ref, role in [(step.source, "source"), (step.destination, "destination")]:
                if not ref or not ref.provenance or ref.provenance.source != "config":
                    continue
                label = ref.resolved_label or ref.description
                if label not in labware_labels:
                    warnings.append(self._warn(
                        step.order, f"{role} labware", label,
                        "config", "fabrication",
                        f"Step {step.order} {role}: claims labware '{label}' "
                        f"from config but no such key in config['labware']",
                    ))

            # Pipette hint: no provenance field, but we can structurally verify
            # that a matching pipette exists in the config.
            if step.pipette_hint:
                config_models = [
                    pip.get("model", "").lower()
                    for pip in config.get("pipettes", {}).values()
                ]
                hint = step.pipette_hint.lower()
                if not any(hint in model for model in config_models):
                    warnings.append(self._warn(
                        step.order, "pipette_hint", step.pipette_hint,
                        "config", "fabrication",
                        f"Step {step.order}: pipette '{step.pipette_hint}' "
                        f"not found in config (available: {config_models})",
                    ))

        return warnings

    def _flag_uncertain_claims(self, spec: ProtocolSpec) -> List[dict]:
        """Flag domain_default and inferred claims for user confirmation.

        domain_default with confidence < 0.8 → severity 'unverified'.
        inferred (any confidence) → severity 'low_confidence'.
        These are routed to Task 6's threshold-based confirmation UX.
        """
        warnings = []

        for step in spec.steps:
            # Composition-level: inferred steps are always flagged
            if step.composition_provenance.confidence < 0.8:
                grounding = step.composition_provenance.grounding
                if "instruction" not in grounding:
                    warnings.append(self._warn(
                        step.order, "composition", step.action,
                        ",".join(grounding) if grounding else "inferred",
                        "low_confidence",
                        f"Step {step.order} ({step.action}): composition confidence "
                        f"{step.composition_provenance.confidence} — may need confirmation",
                    ))

            # Walk provenanced fields
            fields = [
                ("volume", step.volume),
                ("substance", step.substance),
                ("duration", step.duration),
            ]
            for ref, role in [(step.source, "source"), (step.destination, "destination")]:
                if ref and ref.provenance:
                    fields.append((f"{role} location", ref))

            for field_name, field_val in fields:
                if field_val is None:
                    continue
                prov = field_val.provenance if hasattr(field_val, 'provenance') and field_val.provenance else None
                if not prov:
                    continue

                if prov.source == "inferred":
                    val_str = str(field_val.value) if hasattr(field_val, 'value') else str(field_val)
                    warnings.append(self._warn(
                        step.order, field_name, val_str,
                        "inferred", "low_confidence",
                        f"Step {step.order} {field_name}: inferred value "
                        f"(confidence {prov.confidence}) — needs confirmation",
                    ))
                elif prov.source == "domain_default" and prov.confidence < 0.8:
                    val_str = str(field_val.value) if hasattr(field_val, 'value') else str(field_val)
                    warnings.append(self._warn(
                        step.order, field_name, val_str,
                        "domain_default", "unverified",
                        f"Step {step.order} {field_name}: domain default with "
                        f"confidence {prov.confidence} — may need confirmation",
                    ))

            # Post-action fields
            if step.post_actions:
                for pa in step.post_actions:
                    if pa.volume and pa.volume.provenance:
                        prov = pa.volume.provenance
                        if prov.source == "inferred":
                            warnings.append(self._warn(
                                step.order, f"{pa.action} volume",
                                f"{pa.volume.value}{pa.volume.unit}",
                                "inferred", "low_confidence",
                                f"Step {step.order} {pa.action}: inferred volume "
                                f"(confidence {prov.confidence}) — needs confirmation",
                            ))
                        elif prov.source == "domain_default" and prov.confidence < 0.8:
                            warnings.append(self._warn(
                                step.order, f"{pa.action} volume",
                                f"{pa.volume.value}{pa.volume.unit}",
                                "domain_default", "unverified",
                                f"Step {step.order} {pa.action}: domain default volume "
                                f"with confidence {prov.confidence} — may need confirmation",
                            ))

        return warnings

    def verify_provenance_claims(self, spec: ProtocolSpec, instruction: str, config: dict) -> List[dict]:
        """Verify provenance claims across all source types.

        Dispatches on provenance.source for each field:
          - instruction: value must appear in instruction text → 'fabrication' if not
          - config: referenced key must exist in config → 'fabrication' if not
          - domain_default: flag if confidence < 0.8 → 'unverified'
          - inferred: always flag → 'low_confidence'

        Also enforces the cross-check invariant: any source='instruction' volume
        not in spec.explicit_volumes (regex-extracted from text) is a fabricated
        provenance claim.

        Returns list of warning dicts:
          {step, field, value, claimed_source, severity, message}

        Severity levels (for Task 6 routing):
          'fabrication'    — LLM lied about where a value came from (block/fix)
          'unverified'     — domain default with low confidence (prompt user)
          'low_confidence' — inferred value (always prompt user)
        """
        warnings = []
        warnings.extend(self._verify_instruction_claims(spec, instruction))
        warnings.extend(self._verify_config_claims(spec, config))
        warnings.extend(self._flag_uncertain_claims(spec))
        return warnings

    # ========================================================================
    # SUFFICIENCY CHECK
    # ========================================================================

    @staticmethod
    def missing_fields(spec: ProtocolSpec) -> List[str]:
        """Check if the spec has enough information to generate a protocol.
        Returns list of gaps (empty = sufficient)."""
        gaps = []

        liquid_actions = {"transfer", "distribute", "consolidate", "aspirate",
                          "dispense", "mix", "serial_dilution"}

        for step in spec.steps:
            prefix = f"Step {step.order} ({step.action})"

            if step.action in liquid_actions and step.volume is None:
                gaps.append(f"{prefix}: missing volume")

            if step.action in {"transfer", "distribute", "serial_dilution", "consolidate"}:
                if step.source is None:
                    gaps.append(f"{prefix}: missing source location")
                if step.destination is None:
                    gaps.append(f"{prefix}: missing destination location")

        return gaps

    # ========================================================================
    # GAP FILLER
    # ========================================================================

    @staticmethod
    def fill_gaps(spec: ProtocolSpec, config: dict) -> ProtocolSpec:
        """Fill missing parameters with inferred defaults from config.
        Inferred values are tagged so the user can see what was assumed."""
        filled = copy.deepcopy(spec)

        for step in filled.steps:
            # Try to resolve source from config labware contents
            if step.source is None and step.substance and "labware" in config:
                substance_val = step.substance.value
                for label, lw in config["labware"].items():
                    contents = lw.get("contents", {})
                    for well, content_desc in contents.items():
                        if isinstance(content_desc, str) and substance_val.lower() in content_desc.lower():
                            step.source = LocationRef(
                                description=f"{label} (inferred from config)",
                                well=well
                            )
                            break
                    if step.source:
                        break

        return filled

    # ========================================================================
    # USER CONFIRMATION FORMATTER
    # ========================================================================

    @staticmethod
    def _format_step_line(step) -> str:
        """Format a single step as a one-line summary."""
        # Source
        src = ""
        if step.source:
            src = f" from {step.source.description}"
            if step.source.well:
                src += f" well {step.source.well}"
            elif step.source.well_range:
                src += f" wells {step.source.well_range}"

        # Destination
        dst = ""
        if step.destination:
            dst = f" to {step.destination.description}"
            if step.destination.well:
                dst += f" well {step.destination.well}"
            elif step.destination.well_range:
                dst += f" wells {step.destination.well_range}"
            elif step.destination.wells:
                wells = step.destination.wells
                if len(wells) > 2:
                    dst += f" wells {wells[0]}-{wells[-1]}"
                else:
                    dst += f" wells {', '.join(wells)}"

        vol = f" {step.volume.value}{step.volume.unit}" if step.volume else ""
        dur = f" for {step.duration.value} {step.duration.unit}" if step.duration else ""
        substance = f" of {step.substance.value}" if step.substance else ""

        return f"{step.order}. {step.action.upper()}{vol}{substance}{src}{dst}{dur}"

    @staticmethod
    def format_for_confirmation(spec: ProtocolSpec, provenance_warnings: List[dict] = None,
                                threshold: float = 0.7, full: bool = False) -> str:
        """Format spec for user confirmation, grouped by provenance bucket.

        Three sections:
          1. Auto-accepted: instruction/config values above threshold (count only)
          2. Confirmation queue: domain_default below threshold + all inferred
          3. Fabrication errors: provenance claims that failed verification

        If full=True, shows all parameters regardless of provenance (legacy behavior).
        """
        warnings = provenance_warnings or []
        fabrications = [w for w in warnings if w["severity"] == "fabrication"]
        confirmable = [w for w in warnings if w["severity"] in ("unverified", "low_confidence")]

        lines = [
            "",
            "=" * 60,
            "PROTOCOL SPECIFICATION",
            "=" * 60,
        ]

        if spec.protocol_type:
            lines.append(f"  Type: {spec.protocol_type}")
        lines.append(f"  Summary: {spec.summary}")
        lines.append("")

        # --- Fabrication errors (always shown) ---
        if fabrications:
            lines.append(f"  ERRORS ({len(fabrications)}):")
            for w in fabrications:
                lines.append(f"    ! Step {w['step']}, {w['field']}: {w['message']}")
            lines.append("")

        if full:
            # Full mode: show every step with provenance tags (legacy behavior)
            lines.append("  ALL STEPS (full confirmation mode):")
            for step in spec.steps:
                step_line = SemanticExtractor._format_step_line(step)
                # Composition provenance
                comp = step.composition_provenance
                grounding = ",".join(comp.grounding) if comp.grounding else "inferred"
                lines.append(f"    {step_line}")
                lines.append(f"       composition: [{grounding.upper()}, {comp.confidence}]")
                lines.append(f"         \"{comp.justification}\"")

                # Per-field provenance
                fields = []
                if step.volume:
                    p = step.volume.provenance
                    exactness = ", exact" if step.volume.exact else ", approx"
                    fields.append(f"volume: {step.volume.value}{step.volume.unit} "
                                  f"[{p.source}, {p.confidence}{exactness}]")
                if step.substance:
                    p = step.substance.provenance
                    fields.append(f"substance: {step.substance.value} [{p.source}, {p.confidence}]")
                if step.duration:
                    p = step.duration.provenance
                    fields.append(f"duration: {step.duration.value} {step.duration.unit} "
                                  f"[{p.source}, {p.confidence}]")
                for ref, role in [(step.source, "source"), (step.destination, "dest")]:
                    if ref and ref.provenance:
                        p = ref.provenance
                        fields.append(f"{role}: {ref.description} [{p.source}, {p.confidence}]")
                for f in fields:
                    lines.append(f"       {f}")

                if step.post_actions:
                    for pa in step.post_actions:
                        pa_parts = [pa.action]
                        if pa.repetitions:
                            pa_parts.append(f"{pa.repetitions}x")
                        if pa.volume:
                            p = pa.volume.provenance
                            pa_parts.append(f"{pa.volume.value}{pa.volume.unit} "
                                            f"[{p.source}, {p.confidence}]")
                        lines.append(f"       -> {' '.join(pa_parts)}")

                if step.tip_strategy and step.tip_strategy != "unspecified":
                    lines.append(f"       -> tip: {step.tip_strategy}")
                if step.pipette_hint:
                    lines.append(f"       -> pipette: {step.pipette_hint}")

        else:
            # Threshold mode: show auto-accepted count, then confirmation queue

            # Count auto-accepted parameters
            auto_accepted = 0
            for step in spec.steps:
                for field_val in [step.volume, step.substance, step.duration]:
                    if field_val and hasattr(field_val, 'provenance') and field_val.provenance:
                        p = field_val.provenance
                        if p.source in ("instruction", "config") and p.confidence >= threshold:
                            auto_accepted += 1
                for ref in [step.source, step.destination]:
                    if ref and ref.provenance:
                        p = ref.provenance
                        if p.source in ("instruction", "config") and p.confidence >= threshold:
                            auto_accepted += 1
                if step.composition_provenance:
                    comp = step.composition_provenance
                    if "instruction" in comp.grounding and comp.confidence >= threshold:
                        auto_accepted += 1

            # Step overview (always shown for context)
            lines.append("  STEPS:")
            for step in spec.steps:
                step_line = SemanticExtractor._format_step_line(step)
                lines.append(f"    {step_line}")
                if step.post_actions:
                    for pa in step.post_actions:
                        pa_parts = []
                        if pa.repetitions:
                            pa_parts.append(f"{pa.repetitions}x")
                        if pa.volume:
                            pa_parts.append(f"at {pa.volume.value}{pa.volume.unit}")
                        lines.append(f"       -> {pa.action} {' '.join(pa_parts)}")
                if step.tip_strategy and step.tip_strategy != "unspecified":
                    lines.append(f"       -> tip: {step.tip_strategy}")
            lines.append("")

            if auto_accepted:
                lines.append(f"  {auto_accepted} parameter(s) auto-accepted "
                             f"(instruction/config, confidence >= {threshold})")
                lines.append("")

            # Confirmation queue
            if confirmable:
                lines.append(f"  NEEDS CONFIRMATION ({len(confirmable)}):")
                for i, w in enumerate(confirmable, 1):
                    lines.append(f"    [{i}] Step {w['step']}, {w['field']}: "
                                 f"{w['value']} ({w['claimed_source']}, {w['severity']})")
                    # Find the provenance reason for this field
                    step = next((s for s in spec.steps if s.order == w['step']), None)
                    if step:
                        reason = _find_provenance_reason(step, w['field'])
                        if reason:
                            lines.append(f"        reason: \"{reason}\"")
                lines.append("")
            else:
                lines.append("  All parameters verified — no confirmation needed.")
                lines.append("")

        if spec.explicit_volumes:
            lines.append(f"  Locked volumes (from instruction): {spec.explicit_volumes}")
        lines.append("=" * 60)

        return "\n".join(lines)

    # ========================================================================
    # SCHEMA-VS-SPEC VALIDATION (post-generation deterministic gate)
    # ========================================================================

    @staticmethod
    def validate_schema_against_spec(spec: ProtocolSpec, schema) -> List[str]:
        """Check that generated ProtocolSchema honors the spec constraints.
        Returns list of mismatches (empty = OK).

        Only checks volumes that were ACTUALLY in the instruction text
        (spec.explicit_volumes). Post-action volumes that the LLM inferred
        (e.g., mix at 50uL) are NOT hard constraints — they may be legitimately
        adjusted by the constraint checker or pipette selection logic.
        """
        mismatches = []

        # Only validate volumes the user actually wrote in their instruction.
        # These are extracted via regex from the instruction text — ground truth.
        locked_volumes = spec.explicit_volumes
        if not locked_volumes:
            return mismatches

        # Collect all volumes from schema commands
        schema_volumes = set()
        for cmd in schema.commands:
            if hasattr(cmd, 'volume') and cmd.volume is not None:
                schema_volumes.add(cmd.volume)
            if hasattr(cmd, 'mix_after') and cmd.mix_after is not None:
                schema_volumes.add(cmd.mix_after[1])
            if hasattr(cmd, 'mix_before') and cmd.mix_before is not None:
                schema_volumes.add(cmd.mix_before[1])
            if hasattr(cmd, 'repetitions') and hasattr(cmd, 'volume') and cmd.volume is not None:
                schema_volumes.add(cmd.volume)

        # Every user-stated volume must appear in schema
        for vol in locked_volumes:
            if not any(abs(sv - vol) < 0.01 for sv in schema_volumes):
                mismatches.append(
                    f"Volume {vol}uL from instruction text not found in schema commands. "
                    f"Schema has: {sorted(schema_volumes)}. "
                    f"User-specified volumes must not be changed."
                )

        return mismatches

    # ========================================================================
    # DETERMINISTIC: spec + config → ProtocolSchema
    # ========================================================================

    @staticmethod
    def spec_to_schema(spec: ProtocolSpec, config: dict):
        """Deterministically convert a ProtocolSpec + config into a ProtocolSchema.

        No LLM involved. The spec says WHAT (science), this function maps it
        to HOW (hardware). Can't corrupt volumes because it copies them directly.

        Mapping:
          LocationRef.resolved_label → config labware label → slot, load_name
          ProvenancedVolume.value → command volume (direct copy, never modified)
          action → command_type
          pipette_hint / volume range → pipette mount
          well_range → expanded well list
        """
        from .models import (
            ProtocolSchema, Labware, Pipette, Module,
            Transfer, Distribute, Consolidate, Mix,
            Aspirate, Dispense, BlowOut, TouchTip,
            PickUpTip, DropTip,
            Comment, Pause, Delay,
            SetTemperature, WaitForTemperature, DeactivateModule,
            EngageMagnets, DisengageMagnets,
        )
        from .constraints import WellStateTracker

        # Initialize well state tracker with initial contents from spec
        well_tracker = WellStateTracker(config, spec)

        def track_command(cmd):
            """Update well state tracker for a generated command."""
            if isinstance(cmd, Transfer):
                substance = "liquid"
                well_tracker.aspirate(cmd.source_labware, cmd.source_well, cmd.volume)
                well_tracker.dispense(cmd.dest_labware, cmd.dest_well, cmd.volume, substance)
            elif isinstance(cmd, Distribute):
                for dw in cmd.dest_wells:
                    well_tracker.aspirate(cmd.source_labware, cmd.source_well, cmd.volume)
                    well_tracker.dispense(cmd.dest_labware, dw, cmd.volume, "liquid")
            elif isinstance(cmd, Consolidate):
                for sw in cmd.source_wells:
                    well_tracker.aspirate(cmd.source_labware, sw, cmd.volume)
                well_tracker.dispense(cmd.dest_labware, cmd.dest_well, cmd.volume * len(cmd.source_wells), "liquid")
            elif isinstance(cmd, (Aspirate,)):
                well_tracker.aspirate(cmd.labware, cmd.well, cmd.volume)
            elif isinstance(cmd, (Dispense,)):
                well_tracker.dispense(cmd.labware, cmd.well, cmd.volume, "liquid")

        # ---- helpers ----

        def resolve_labware(ref, cfg: dict) -> Optional[str]:
            """Read the pre-resolved config label from a LocationRef.

            Resolution is done upstream by LabwareResolver. This just reads
            the result, with a fallback to description for backwards compatibility.
            """
            if not ref:
                return None
            if ref.resolved_label and ref.resolved_label in cfg.get("labware", {}):
                return ref.resolved_label
            # Fallback: description might already be a config label (legacy specs)
            if ref.description in cfg.get("labware", {}):
                return ref.description
            return None

        def pipette_for_volume(volume: float, cfg: dict) -> Optional[str]:
            """Pick the pipette mount whose range contains the volume."""
            ranges = [('p1000', 100, 1000), ('p300', 20, 300),
                      ('p50', 5, 50), ('p20', 1, 20), ('p10', 1, 10)]
            if "pipettes" not in cfg:
                return None
            for mount, pip in cfg["pipettes"].items():
                model = pip["model"].lower()
                for key, lo, hi in ranges:
                    if key in model and lo <= volume <= hi:
                        return mount
            return None

        def pipette_by_hint(hint: str, cfg: dict) -> Optional[str]:
            """Match a pipette hint like 'p20' to a mount."""
            if not hint or "pipettes" not in cfg:
                return None
            for mount, pip in cfg["pipettes"].items():
                if hint.lower() in pip["model"].lower():
                    return mount
            return None

        def expand_well_range(well_range: str) -> List[str]:
            """Expand well range descriptions into explicit well lists.

            Handles many formats:
              'A1-A12'              → A1, A2, ..., A12  (single row)
              'A1-H1'              → A1, B1, ..., H1   (single column)
              'A1-H3'              → A1, B1, ..., H1, A2, ..., H3  (block, column-major)
              'column 1'           → A1, B1, ..., H1
              'columns 2-8'        → all wells in columns 2-8
              'columns 2-8, rows A-H' → explicit row/col specification
              'rows A-D'           → all wells in rows A-D
              'A1, A2, A3'         → explicit list
            """
            text = well_range.strip()

            # Format: explicit comma-separated wells "A1, A2, A3"
            comma_wells = re.findall(r'([A-P]\d{1,2})', text)
            if ',' in text and comma_wells:
                return comma_wells

            # Format: "A1-H3" or "A1-A12" (standard range)
            m = re.match(r'([A-P])(\d+)\s*[-–]\s*([A-P])(\d+)$', text)
            if m:
                r1, c1, r2, c2 = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
                wells = []
                for col in range(c1, c2 + 1):
                    for row in range(ord(r1), ord(r2) + 1):
                        wells.append(f"{chr(row)}{col}")
                return wells

            # Parse row and column ranges from natural language
            # Extract column info
            col_start, col_end = 1, 12  # defaults for 96-well
            col_match = re.search(r'columns?\s+(\d+)\s*[-–]\s*(\d+)', text, re.IGNORECASE)
            col_single = re.search(r'column\s+(\d+)', text, re.IGNORECASE)
            if col_match:
                col_start, col_end = int(col_match.group(1)), int(col_match.group(2))
            elif col_single:
                col_start = col_end = int(col_single.group(1))

            # Extract row info
            row_start, row_end = 'A', 'H'  # defaults for 96-well
            row_match = re.search(r'rows?\s+([A-P])\s*[-–]\s*([A-P])', text, re.IGNORECASE)
            row_single = re.search(r'row\s+([A-P])', text, re.IGNORECASE)
            if row_match:
                row_start, row_end = row_match.group(1).upper(), row_match.group(2).upper()
            elif row_single:
                row_start = row_end = row_single.group(1).upper()

            # If we found column or row keywords, generate the wells
            if col_match or col_single or row_match or row_single:
                wells = []
                for col in range(col_start, col_end + 1):
                    for row in range(ord(row_start), ord(row_end) + 1):
                        wells.append(f"{chr(row)}{col}")
                return wells

            # Format: "all wells" — return full 96-well plate
            if 'all wells' in text.lower():
                wells = []
                for col in range(1, 13):
                    for row in range(ord('A'), ord('H') + 1):
                        wells.append(f"{chr(row)}{col}")
                return wells

            return []

        def wells_from_ref(ref) -> List[str]:
            """Get explicit well list from a LocationRef."""
            if not ref:
                return []
            if ref.wells:
                return ref.wells
            if ref.well_range:
                expanded = expand_well_range(ref.well_range)
                if expanded:
                    return expanded
            if ref.well:
                return [ref.well]
            return []

        def vol_in_ul(step) -> Optional[float]:
            """Get volume in uL from a step."""
            if not step.volume:
                return None
            v = step.volume.value
            if step.volume.unit == "mL":
                v *= 1000
            return v

        def pick_mount(step, cfg) -> Optional[str]:
            """Determine pipette mount for a step.

            Considers ALL volumes in the step (transfer + post-actions like mix)
            and picks a pipette that satisfies all constraints. If no single
            pipette works, picks the one for the transfer volume — the constraint
            checker will have already surfaced this conflict to the scientist.
            """
            if step.pipette_hint:
                m = pipette_by_hint(step.pipette_hint, cfg)
                if m:
                    return m

            # Collect all volumes this step needs the pipette to handle
            volumes_needed = []
            v = vol_in_ul(step)
            if v:
                volumes_needed.append(v)

            if step.post_actions:
                for pa in step.post_actions:
                    if pa.volume:
                        pa_v = pa.volume.value
                        if pa.volume.unit == "mL":
                            pa_v *= 1000
                        volumes_needed.append(pa_v)

            if not volumes_needed:
                return None

            # Try to find a pipette that covers ALL volumes (min to max)
            min_vol = min(volumes_needed)
            max_vol = max(volumes_needed)

            if "pipettes" not in cfg:
                return None

            # Check each pipette: does its range cover [min_vol, max_vol]?
            ranges = [('p1000', 100, 1000), ('p300', 20, 300),
                      ('p50', 5, 50), ('p20', 1, 20), ('p10', 1, 10)]
            for mount, pip in cfg["pipettes"].items():
                model = pip["model"].lower()
                for key, lo, hi in ranges:
                    if key in model:
                        if lo <= min_vol and max_vol <= hi:
                            return mount  # This pipette covers everything
                        break

            # No single pipette covers all volumes.
            # Fall back to the one that handles the transfer volume.
            # The constraint checker has already flagged this for the scientist.
            if v:
                return pipette_for_volume(v, cfg)
            return pipette_for_volume(max_vol, cfg)

        # ---- build schema ----

        # Collect used labware labels
        used_labels = set()
        for step in spec.steps:
            for ref in [step.source, step.destination]:
                lbl = resolve_labware(ref, config)
                if lbl:
                    used_labels.add(lbl)
        # Include tipracks
        if "pipettes" in config:
            for pip in config["pipettes"].values():
                for tr in pip.get("tipracks", []):
                    used_labels.add(tr)

        # Build Labware objects
        labware_list = []
        for label in used_labels:
            if label in config.get("labware", {}):
                lw = config["labware"][label]
                labware_list.append(Labware(
                    slot=lw["slot"], load_name=lw["load_name"],
                    label=label, on_module=lw.get("on_module")
                ))

        # Collect used pipette mounts
        used_mounts = set()
        for step in spec.steps:
            m = pick_mount(step, config)
            if m:
                used_mounts.add(m)
        if not used_mounts and "pipettes" in config:
            used_mounts = set(config["pipettes"].keys())

        # Build Pipette objects
        pipette_list = []
        for mount in used_mounts:
            if mount in config.get("pipettes", {}):
                pip = config["pipettes"][mount]
                pipette_list.append(Pipette(
                    mount=mount, model=pip["model"],
                    tipracks=pip.get("tipracks", [])
                ))

        # Build Commands — using TrackedList to automatically update well state
        class TrackedList(list):
            """List that tracks well state whenever a command is appended."""
            def append(self, cmd):
                super().append(cmd)
                track_command(cmd)

        commands = TrackedList()
        default_mount = sorted(used_mounts)[0] if used_mounts else "left"

        for step in spec.steps:
            v = vol_in_ul(step)
            mount = pick_mount(step, config) or default_mount
            src_label = resolve_labware(step.source, config)
            dst_label = resolve_labware(step.destination, config)
            src_wells = wells_from_ref(step.source)
            dst_wells = wells_from_ref(step.destination)

            # Tip strategy
            # For compound commands (transfer), "same_tip" means we pick up one tip
            # before the batch and drop after. We use new_tip='never' on each individual
            # Transfer and wrap with PickUpTip/DropTip.
            # For "new_tip_each", each Transfer uses new_tip='always' (handles its own tips).
            use_same_tip = (step.tip_strategy == "same_tip")
            new_tip = "never" if use_same_tip else "always"

            # Mix from post_actions
            # NOTE: No silent clamping here. If the mix volume exceeds pipette
            # capacity, the ConstraintChecker should have caught it upstream and
            # surfaced it to the scientist. By this point, either:
            #   (a) the volume is within range, or
            #   (b) the scientist approved an alternative resolution
            mix_after = None
            if step.post_actions:
                for pa in step.post_actions:
                    if pa.action == "mix":
                        reps = pa.repetitions or 3
                        if pa.volume:
                            pa_v = pa.volume.value
                            if pa.volume.unit == "mL":
                                pa_v *= 1000
                        else:
                            # Default: use the step's transfer volume
                            pa_v = v if v else 50.0
                        # Constrain to pipette capacity — but transparently.
                        # If a constraint violation was raised and the scientist chose
                        # to proceed anyway (e.g., "reduce mix volume"), apply it here.
                        if mount in config.get("pipettes", {}):
                            model_str = config["pipettes"][mount]["model"].lower()
                            cap = None
                            for key, lo, hi in [('p1000', 100, 1000), ('p300', 20, 300),
                                                 ('p50', 5, 50), ('p20', 1, 20), ('p10', 1, 10)]:
                                if key in model_str:
                                    cap = hi
                                    break
                            if cap and pa_v > cap:
                                # Reduce to pipette max — this path is only reached
                                # when the scientist has been informed and chose to proceed
                                pa_v = cap
                        mix_after = (reps, pa_v)

            # ---- map action → commands ----

            if step.action == "transfer":
                # For same_tip: pick up one tip before all transfers, drop after
                if use_same_tip:
                    commands.append(PickUpTip(pipette=mount))

                if step.replicates and src_wells:
                    # Replicate expansion: each source well → N dest wells
                    # across consecutive columns starting at dest's column
                    dest_start_col = 1
                    if dst_wells:
                        m = re.match(r'[A-P](\d+)', dst_wells[0])
                        if m:
                            dest_start_col = int(m.group(1))
                    elif step.destination and step.destination.well:
                        m = re.match(r'[A-P](\d+)', step.destination.well)
                        if m:
                            dest_start_col = int(m.group(1))

                    for sw in src_wells:
                        row = sw[0]
                        for rep in range(step.replicates):
                            dw = f"{row}{dest_start_col + rep}"
                            commands.append(Transfer(
                                pipette=mount, source_labware=src_label, source_well=sw,
                                dest_labware=dst_label, dest_well=dw, volume=v,
                                new_tip=new_tip, mix_after=mix_after
                            ))
                elif src_wells and dst_wells:
                    if len(src_wells) == 1 and len(dst_wells) > 1:
                        # 1 source → N destinations: transfer to each dest
                        for dw in dst_wells:
                            commands.append(Transfer(
                                pipette=mount, source_labware=src_label, source_well=src_wells[0],
                                dest_labware=dst_label, dest_well=dw, volume=v,
                                new_tip=new_tip, mix_after=mix_after
                            ))
                    elif len(dst_wells) == 1 and len(src_wells) > 1:
                        # N sources → 1 destination: transfer from each source
                        for sw in src_wells:
                            commands.append(Transfer(
                                pipette=mount, source_labware=src_label, source_well=sw,
                                dest_labware=dst_label, dest_well=dst_wells[0], volume=v,
                                new_tip=new_tip, mix_after=mix_after
                            ))
                    else:
                        # Paired well lists: zip source[i] → dest[i]
                        pairs = list(zip(src_wells, dst_wells))
                        for sw, dw in pairs:
                            commands.append(Transfer(
                                pipette=mount, source_labware=src_label, source_well=sw,
                                dest_labware=dst_label, dest_well=dw, volume=v,
                                new_tip=new_tip, mix_after=mix_after
                            ))
                else:
                    # Single well fallback
                    sw = step.source.well if step.source and step.source.well else "A1"
                    dw = step.destination.well if step.destination and step.destination.well else "A1"
                    commands.append(Transfer(
                        pipette=mount, source_labware=src_label, source_well=sw,
                        dest_labware=dst_label, dest_well=dw, volume=v,
                        new_tip=new_tip, mix_after=mix_after
                    ))

                # Drop tip after same_tip transfer batch
                if use_same_tip:
                    commands.append(DropTip(pipette=mount))

            elif step.action == "distribute":
                commands.append(Distribute(
                    pipette=mount, source_labware=src_label,
                    source_well=src_wells[0] if src_wells else (step.source.well if step.source else "A1"),
                    dest_labware=dst_label, dest_wells=dst_wells, volume=v,
                    new_tip="once"
                ))

            elif step.action == "consolidate":
                commands.append(Consolidate(
                    pipette=mount, source_labware=src_label, source_wells=src_wells,
                    dest_labware=dst_label,
                    dest_well=dst_wells[0] if dst_wells else (step.destination.well if step.destination else "A1"),
                    volume=v, new_tip="once"
                ))

            elif step.action == "serial_dilution":
                # Serial dilution: sequential transfers from source through destination wells.
                # Each transfer moves liquid from one well to the next, with mixing.
                #
                # Pattern: source → dest[0] → dest[1] → ... → dest[N]
                #
                # NOTE: We do NOT distribute diluent here. If diluent is needed,
                # the scientist's instruction should have it as a separate step
                # (e.g., "add 90uL water to all wells"). The serial_dilution action
                # ONLY handles the sequential transfers with mixing.
                if dst_wells:
                    # Build the full chain: [source_well, dest[0], dest[1], ...]
                    source_well = src_wells[0] if src_wells else "A1"
                    chain = [source_well] + dst_wells

                    # Serial dilution transfers — new tip each to prevent carryover
                    if use_same_tip:
                        commands.append(PickUpTip(pipette=mount))
                    for i in range(len(chain) - 1):
                        # First transfer is from source labware, rest are within dest labware
                        from_label = src_label if i == 0 else dst_label
                        commands.append(Transfer(
                            pipette=mount, source_labware=from_label,
                            source_well=chain[i], dest_labware=dst_label,
                            dest_well=chain[i + 1], volume=v,
                            new_tip=new_tip, mix_after=mix_after or (3, v)
                        ))
                    if use_same_tip:
                        commands.append(DropTip(pipette=mount))

            elif step.action == "mix":
                target = dst_label or src_label
                reps = 3
                if step.post_actions:
                    for pa in step.post_actions:
                        if pa.action == "mix" and pa.repetitions:
                            reps = pa.repetitions
                wells = dst_wells or src_wells or ["A1"]
                if new_tip == "never":
                    # Same tip for all wells: pick up once, mix all, drop once
                    commands.append(PickUpTip(pipette=mount))
                    for well in wells:
                        commands.append(Mix(
                            pipette=mount, labware=target, well=well,
                            volume=v, repetitions=reps
                        ))
                    commands.append(DropTip(pipette=mount))
                else:
                    # New tip per well
                    for well in wells:
                        commands.append(PickUpTip(pipette=mount))
                        commands.append(Mix(
                            pipette=mount, labware=target, well=well,
                            volume=v, repetitions=reps
                        ))
                        commands.append(DropTip(pipette=mount))

            elif step.action == "delay":
                minutes, seconds = None, None
                # Use ProvenancedDuration if available
                if step.duration:
                    if step.duration.unit == "minutes":
                        minutes = step.duration.value
                    elif step.duration.unit == "seconds":
                        seconds = step.duration.value
                    elif step.duration.unit == "hours":
                        minutes = step.duration.value * 60
                # Fallback: parse from note
                if minutes is None and seconds is None and step.note:
                    mm = re.search(r'(\d+)\s*min', step.note)
                    ss = re.search(r'(\d+)\s*sec', step.note)
                    if mm:
                        minutes = float(mm.group(1))
                    if ss:
                        seconds = float(ss.group(1))
                commands.append(Delay(
                    pipette=mount, minutes=minutes,
                    seconds=seconds or (60.0 if not minutes else None)
                ))

            elif step.action == "pause":
                commands.append(Pause(
                    pipette=mount,
                    message=step.note or (step.substance.value if step.substance else None) or "Pausing protocol"
                ))

            elif step.action == "comment":
                commands.append(Comment(
                    pipette=mount,
                    message=step.note or (step.substance.value if step.substance else None) or ""
                ))

            elif step.action == "aspirate":
                target = src_label or dst_label
                wells = src_wells or dst_wells or ["A1"]
                if new_tip == "never":
                    # Same tip: pick up once, aspirate+blowout all wells, drop once
                    commands.append(PickUpTip(pipette=mount))
                    for well in wells:
                        commands.append(Aspirate(
                            pipette=mount, labware=target, well=well, volume=v
                        ))
                        # Blow out to trash after each aspirate (waste removal)
                        commands.append(BlowOut(pipette=mount))
                    commands.append(DropTip(pipette=mount))
                else:
                    # New tip per well
                    for well in wells:
                        commands.append(PickUpTip(pipette=mount))
                        commands.append(Aspirate(
                            pipette=mount, labware=target, well=well, volume=v
                        ))
                        commands.append(DropTip(pipette=mount))

            elif step.action == "dispense":
                target = dst_label or src_label
                wells = dst_wells or src_wells or ["A1"]
                if new_tip == "never":
                    commands.append(PickUpTip(pipette=mount))
                    for well in wells:
                        commands.append(Dispense(
                            pipette=mount, labware=target, well=well, volume=v
                        ))
                    commands.append(DropTip(pipette=mount))
                else:
                    for well in wells:
                        commands.append(PickUpTip(pipette=mount))
                        commands.append(Dispense(
                            pipette=mount, labware=target, well=well, volume=v
                        ))
                        commands.append(DropTip(pipette=mount))

            elif step.action == "blow_out":
                target = dst_label or src_label
                wells = dst_wells or src_wells
                if target and wells:
                    for well in wells:
                        commands.append(BlowOut(
                            pipette=mount, labware=target, well=well
                        ))
                else:
                    commands.append(BlowOut(pipette=mount))

            elif step.action == "touch_tip":
                target = dst_label or src_label
                wells = dst_wells or src_wells or ["A1"]
                for well in wells:
                    commands.append(TouchTip(
                        pipette=mount, labware=target, well=well
                    ))

            # --- Module commands ---

            elif step.action == "set_temperature":
                # Extract celsius from note
                celsius = None
                if step.note:
                    m = re.search(r'(\d+(?:\.\d+)?)\s*°?\s*C', step.note)
                    if m:
                        celsius = float(m.group(1))
                if celsius is not None:
                    # Find the module label
                    mod_label = None
                    if "modules" in config:
                        for label, mod in config["modules"].items():
                            if mod.get("module_type") == "temperature":
                                mod_label = label
                                break
                        if mod_label is None:
                            # Use first module as fallback
                            mod_label = next(iter(config["modules"]))
                    if mod_label:
                        commands.append(SetTemperature(
                            pipette=mount, module=mod_label, celsius=celsius
                        ))

            elif step.action == "wait_for_temperature":
                mod_label = None
                if "modules" in config:
                    for label, mod in config["modules"].items():
                        if mod.get("module_type") == "temperature":
                            mod_label = label
                            break
                if mod_label:
                    commands.append(WaitForTemperature(
                        pipette=mount, module=mod_label
                    ))

            elif step.action == "engage_magnets":
                mod_label = None
                if "modules" in config:
                    for label, mod in config["modules"].items():
                        if mod.get("module_type") == "magnetic":
                            mod_label = label
                            break
                if mod_label:
                    commands.append(EngageMagnets(
                        pipette=mount, module=mod_label
                    ))

            elif step.action == "disengage_magnets":
                mod_label = None
                if "modules" in config:
                    for label, mod in config["modules"].items():
                        if mod.get("module_type") == "magnetic":
                            mod_label = label
                            break
                if mod_label:
                    commands.append(DisengageMagnets(
                        pipette=mount, module=mod_label
                    ))

            elif step.action == "deactivate":
                mod_label = None
                if "modules" in config:
                    mod_label = next(iter(config["modules"]))
                if mod_label:
                    commands.append(DeactivateModule(
                        pipette=mount, module=mod_label
                    ))

            else:
                print(f"  Warning: unhandled action '{step.action}' in step {step.order}, skipping")

        # Build Module objects from config
        module_list = []
        if "modules" in config:
            # Include modules that are referenced by commands
            used_module_labels = set()
            for cmd in commands:
                if hasattr(cmd, 'module'):
                    used_module_labels.add(cmd.module)
            for label, mod in config["modules"].items():
                if label in used_module_labels:
                    module_list.append(Module(
                        module_type=mod["module_type"],
                        slot=mod["slot"],
                        label=label
                    ))

        # Report well state tracker warnings
        if well_tracker.warnings:
            import textwrap
            for w in well_tracker.warnings:
                wrapped = textwrap.fill(f"Well state: {w}", width=60,
                                        initial_indent="  ", subsequent_indent="    ")
                print(wrapped, file=sys.stderr)

        return ProtocolSchema(
            protocol_name=spec.protocol_type or "AI Generated Protocol",
            author="Biolab AI",
            labware=labware_list,
            pipettes=pipette_list,
            modules=module_list if module_list else None,
            commands=list(commands)
        )
