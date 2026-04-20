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
from typing import List, Optional, Literal, Dict

from anthropic import Anthropic
from pydantic import BaseModel, Field, model_validator


# ============================================================================
# INTERMEDIATE REPRESENTATION
# ============================================================================

class VolumeSpec(BaseModel):
    """A volume associated with a protocol step."""
    value: float = Field(..., gt=0)
    unit: Literal["uL", "mL"] = "uL"
    approximate: bool = Field(False, description="True if hedged: 'about 100uL', '~50uL'")
    inferred: bool = Field(False, description="True if inferred from domain knowledge, not stated by user")


class LocationRef(BaseModel):
    """A reference to a labware location, as the user described it."""
    description: str = Field(..., description="How the user referred to it: 'reservoir', 'PCR plate', 'tube rack'")
    well: Optional[str] = Field(None, description="Single well: 'A1'")
    wells: Optional[List[str]] = Field(None, description="Explicit well list")
    well_range: Optional[str] = Field(None, description="Range as stated: 'A1-A12', 'column 1'")


class DurationSpec(BaseModel):
    """A time duration for delays, incubations, pauses."""
    value: float = Field(..., gt=0)
    unit: Literal["seconds", "minutes", "hours"]


class PostAction(BaseModel):
    """Post-transfer action like mixing."""
    action: Literal["mix", "blow_out", "touch_tip"]
    repetitions: Optional[int] = None
    volume: Optional[VolumeSpec] = None


class ExtractedStep(BaseModel):
    """One logical step in the protocol."""
    order: int = Field(..., ge=1)
    action: str = Field(..., description="transfer, distribute, consolidate, mix, serial_dilution, delay, pause, set_temperature, aspirate, dispense, engage_magnets, disengage_magnets, comment, etc.")
    substance: Optional[str] = Field(None, description="What is being moved: 'master mix', 'DNA template', 'diluent'")
    volume: Optional[VolumeSpec] = None
    duration: Optional[DurationSpec] = Field(None, description="For delay/pause/incubation steps: how long to wait")
    source: Optional[LocationRef] = None
    destination: Optional[LocationRef] = None
    post_actions: Optional[List[PostAction]] = None
    replicates: Optional[int] = Field(None, ge=2, description="Number of replicates per source well (e.g., 3 for triplicate). Each source well maps to N destination wells across consecutive columns.")
    tip_strategy: Optional[Literal["new_tip_each", "same_tip", "unspecified"]] = None
    pipette_hint: Optional[str] = Field(None, description="If user specified a pipette: 'p20', 'p300'")
    note: Optional[str] = Field(None, description="Additional context from instruction")


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

    @model_validator(mode='after')
    def validate_step_ordering(self) -> 'ProtocolSpec':
        orders = [s.order for s in self.steps]
        if sorted(orders) != list(range(1, len(orders) + 1)):
            raise ValueError(f"Step orders must be consecutive 1..N, got {orders}")
        return self


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

THEN produce structured JSON (inside <spec> tags) matching this schema:
{schema}

RULES:
- Preserve exact volumes the user stated (10.5uL stays 10.5, never round or adjust)
- If a volume is approximate ("about 100uL", "~50uL"), mark approximate: true
- If you infer a volume from domain knowledge (user didn't state it), mark inferred: true
- If the user mentioned a pipette ("use the p20"), record it as pipette_hint
- If the user mentioned tip strategy ("new tip for each"), record as tip_strategy
- For time durations (delays, pauses, incubations), use the "duration" field with unit "seconds", "minutes", or "hours" — NEVER put time values in the "volume" field
- For well ranges, prefer the format "A1-H12" or provide explicit well lists like ["A1", "B1", "C1"]. Avoid natural language well descriptions like "columns 2-8, rows A-H" — instead write "A2-H8"
- Every liquid-handling step (transfer, distribute, mix, etc.) MUST have a volume
- DO NOT choose Opentrons API labware names — use the user's descriptions
- DO NOT choose pipette mounts — only record hints if the user mentioned a pipette
- Leave the "reasoning" field empty in your JSON — it will be filled from your <reasoning> block
- Leave "explicit_volumes" empty — it will be populated automatically

COMPRESSION — KEEP STEPS COMPACT:
- NEVER create separate steps for repetitive operations that differ only in wells.
  Instead, use ONE step with well lists on source and/or destination.
- For replicate patterns (triplicate, duplicate), use the "replicates" field:
  Example: 8 standards each plated in triplicate across columns 1-3:
    {{
      "action": "transfer",
      "source": {{"description": "dilution strip", "wells": ["A1","B1","C1","D1","E1","F1","G1","H1"]}},
      "destination": {{"description": "qPCR plate", "well": "A1"}},
      "replicates": 3
    }}
  This means: A1→[A1,A2,A3], B1→[B1,B2,B3], ..., H1→[H1,H2,H3].
  Each source well maps to N consecutive columns starting at the destination well's column.
- For paired well lists (source[0]→dest[0], source[1]→dest[1], ...):
    {{
      "action": "transfer",
      "source": {{"description": "plate A", "wells": ["A1","A2","A3"]}},
      "destination": {{"description": "plate B", "wells": ["B1","B2","B3"]}}
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
            print(f"  Reasoning failed: {e}")
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
    # HALLUCINATION GUARD
    # ========================================================================

    def validate_against_text(self, spec: ProtocolSpec, instruction: str) -> List[str]:
        """Check that non-approximate, non-inferred volumes actually appear in instruction.
        Returns list of warnings for potentially hallucinated values."""
        warnings = []
        text_volumes = self._extract_volumes_from_text(instruction)

        for step in spec.steps:
            if step.volume and not step.volume.approximate and not step.volume.inferred:
                if step.volume.value not in text_volumes:
                    warnings.append(
                        f"Step {step.order}: volume {step.volume.value}{step.volume.unit} "
                        f"not found in instruction text (found: {text_volumes})"
                    )
            if step.post_actions:
                for pa in step.post_actions:
                    if pa.volume and not pa.volume.approximate and not pa.volume.inferred:
                        if pa.volume.value not in text_volumes:
                            warnings.append(
                                f"Step {step.order} {pa.action}: volume "
                                f"{pa.volume.value}{pa.volume.unit} not found in instruction text"
                            )
        return warnings

    # ========================================================================
    # SUFFICIENCY CHECK
    # ========================================================================

    @staticmethod
    def check_sufficiency(spec: ProtocolSpec) -> List[str]:
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
                for label, lw in config["labware"].items():
                    contents = lw.get("contents", {})
                    for well, content_desc in contents.items():
                        if isinstance(content_desc, str) and step.substance.lower() in content_desc.lower():
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
    def format_for_confirmation(spec: ProtocolSpec) -> str:
        """Format spec as human-readable summary for user confirmation."""
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
        lines.append("  STEPS:")

        for step in spec.steps:
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

            # Volume with provenance tag
            vol = ""
            if step.volume:
                if step.volume.inferred:
                    tag = " (inferred)"
                elif step.volume.approximate:
                    tag = " (approximate)"
                else:
                    tag = ""
                vol = f" {step.volume.value}{step.volume.unit}{tag}"

            substance = f" of {step.substance}" if step.substance else ""
            line = f"    {step.order}. {step.action.upper()}{vol}{substance}{src}{dst}"
            lines.append(line)

            # Post-actions
            if step.post_actions:
                for pa in step.post_actions:
                    pa_vol = f" at {pa.volume.value}{pa.volume.unit}" if pa.volume else ""
                    pa_reps = f" {pa.repetitions}x" if pa.repetitions else ""
                    lines.append(f"       -> {pa.action}{pa_reps}{pa_vol}")

            if step.tip_strategy and step.tip_strategy != "unspecified":
                lines.append(f"       -> tip strategy: {step.tip_strategy}")

            if step.pipette_hint:
                lines.append(f"       -> pipette hint: {step.pipette_hint}")

        lines.append("")
        if spec.explicit_volumes:
            lines.append(f"  Volumes from instruction (locked): {spec.explicit_volumes}")
        lines.append("=" * 60)

        return "\n".join(lines)

    # ========================================================================
    # SCHEMA-VS-SPEC VALIDATION (post-generation deterministic gate)
    # ========================================================================

    @staticmethod
    def validate_schema_against_spec(spec: ProtocolSpec, schema) -> List[str]:
        """Check that generated ProtocolSchema honors the spec constraints.
        Returns list of mismatches (empty = OK).

        This is the deterministic gate: volumes the user specified must appear
        in the generated schema. If the LLM changed them during generation,
        this catches it.
        """
        mismatches = []

        # Collect all hard-constraint volumes from spec
        # (non-approximate, non-inferred = user explicitly stated these)
        spec_volumes = []
        for step in spec.steps:
            if step.volume and not step.volume.approximate and not step.volume.inferred:
                vol = step.volume.value
                if step.volume.unit == "mL":
                    vol *= 1000
                spec_volumes.append(vol)
            if step.post_actions:
                for pa in step.post_actions:
                    if pa.volume and not pa.volume.approximate and not pa.volume.inferred:
                        vol = pa.volume.value
                        if pa.volume.unit == "mL":
                            vol *= 1000
                        spec_volumes.append(vol)

        if not spec_volumes:
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

        # Every spec volume must appear in schema
        for vol in spec_volumes:
            if not any(abs(sv - vol) < 0.01 for sv in schema_volumes):
                mismatches.append(
                    f"Volume {vol}uL from spec not found in schema commands. "
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
          LocationRef.description → config labware label → slot, load_name
          VolumeSpec.value → command volume (direct copy, never modified)
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

        # ---- helpers ----

        def resolve_labware(ref, cfg: dict) -> Optional[str]:
            """Map a LocationRef description to a config labware label.

            Tries multiple matching strategies:
              1. Exact match (desc == label)
              2. Substring match (desc in label or label in desc)
              3. Word overlap (any word in desc matches any word in label)
              4. Load_name keyword match
              5. Normalized word match (strip separators, compare word sets)
            """
            if not ref or "labware" not in cfg:
                return None
            desc = ref.description.lower().replace("(inferred from config)", "").strip()
            desc_words = set(re.split(r'[\s_\-]+', desc))

            # 1. Exact match
            for label in cfg["labware"]:
                if label.lower() == desc:
                    return label

            # 2. Score-based matching — score each label, pick highest
            #    This avoids the problem where "destination plate" matches
            #    "source_plate" and "dest_plate" equally on word overlap,
            #    by adding substring bonus ("dest" is in "destination")
            def score_label(label: str) -> int:
                s = 0
                label_words = set(re.split(r'[\s_\-]+', label.lower()))
                # Word overlap
                s += len(desc_words & label_words) * 10
                # Substring bonus: label word is substring of a desc word or vice versa
                for lw in label_words:
                    for dw in desc_words:
                        if len(lw) > 2 and len(dw) > 2 and lw != dw:
                            if lw in dw or dw in lw:
                                s += 5
                # Load_name keyword match
                load = cfg["labware"][label].get("load_name", "").lower()
                for dw in desc_words:
                    if len(dw) > 2 and dw in load:
                        s += 3
                return s

            scored = [(label, score_label(label)) for label in cfg["labware"]]
            scored.sort(key=lambda x: -x[1])
            if scored and scored[0][1] > 0:
                # Require a minimum score to avoid false matches
                # (e.g., "sample plate" matching "qpcr_plate" just on "plate")
                if scored[0][1] >= 10:
                    return scored[0][0]
                # Low-confidence match — warn
                print(f"  Warning: weak labware match '{desc}' → '{scored[0][0]}' (score={scored[0][1]}), skipping")

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
            """Determine pipette mount for a step."""
            if step.pipette_hint:
                m = pipette_by_hint(step.pipette_hint, cfg)
                if m:
                    return m
            v = vol_in_ul(step)
            if v:
                return pipette_for_volume(v, cfg)
            return None

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

        # Build Commands
        commands = []
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
                        # Clamp mix volume to pipette capacity
                        if mount in config.get("pipettes", {}):
                            model = config["pipettes"][mount]["model"].lower()
                            cap = None
                            for key, lo, hi in [('p1000', 100, 1000), ('p300', 20, 300),
                                                 ('p50', 5, 50), ('p20', 1, 20), ('p10', 1, 10)]:
                                if key in model:
                                    cap = hi
                                    break
                            if cap and pa_v > cap:
                                pa_v = v if v and v <= cap else cap
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
                # Decompose: distribute diluent + sequential transfers with mix
                if dst_wells and len(dst_wells) > 1:
                    commands.append(Distribute(
                        pipette=mount, source_labware=src_label,
                        source_well=src_wells[0] if src_wells else "A1",
                        dest_labware=dst_label, dest_wells=dst_wells[1:],
                        volume=v, new_tip="once"
                    ))
                    # Serial dilution transfers — pick up tip before batch if same_tip
                    if use_same_tip:
                        commands.append(PickUpTip(pipette=mount))
                    for i in range(len(dst_wells) - 1):
                        commands.append(Transfer(
                            pipette=mount, source_labware=dst_label,
                            source_well=dst_wells[i], dest_labware=dst_label,
                            dest_well=dst_wells[i + 1], volume=v,
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
                # Use DurationSpec if available
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
                    message=step.note or step.substance or "Pausing protocol"
                ))

            elif step.action == "comment":
                commands.append(Comment(
                    pipette=mount,
                    message=step.note or step.substance or ""
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

        return ProtocolSchema(
            protocol_name=spec.protocol_type or "AI Generated Protocol",
            author="Biolab AI",
            labware=labware_list,
            pipettes=pipette_list,
            modules=module_list if module_list else None,
            commands=commands
        )
