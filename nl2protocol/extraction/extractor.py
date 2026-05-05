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
from typing import Annotated, List, Optional, Literal, Dict, Tuple

from anthropic import Anthropic
from pydantic import BaseModel, Field, ValidationError, model_validator


# Import spec models from their canonical location
from nl2protocol.models.spec import (
    WellName,
    Provenance,
    CompositionProvenance,
    ProvenancedVolume,
    ProvenancedDuration,
    ProvenancedTemperature,
    ProvenancedString,
    LocationRef,
    PostAction,
    ActionType,
    ExtractedStep,
    WellContents,
    LabwarePrefill,
    ProtocolSpec,
    CompleteProtocolSpec,
)

from nl2protocol.extraction.prompts import REASONING_SYSTEM_PROMPT, REASONING_USER_PROMPT
from nl2protocol.extraction.resolver import LabwareResolver
from nl2protocol.extraction.schema_builder import (
    spec_to_schema as _spec_to_schema,
    validate_schema_against_spec as _validate_schema_against_spec,
    _format_step_line,
)




def _find_provenance_reason(step, field_name: str) -> Optional[str]:
    """Look up the provenance reason string for a given field on a step."""
    field_map = {
        "volume": step.volume,
        "temperature": step.temperature,
        "substance": step.substance,
        "duration": step.duration,
        "composition": None,
    }
    # Direct field match
    if field_name in field_map:
        val = field_map[field_name]
        if val and hasattr(val, 'provenance') and val.provenance:
            return _provenance_text(val.provenance)
        if field_name == "composition":
            return step.composition_provenance.step_cited_text
        return None

    # Location refs: "source location", "source labware", "source wells", etc.
    for prefix, ref in [("source", step.source), ("destination", step.destination), ("dest", step.destination)]:
        if field_name.startswith(prefix) and ref and ref.provenance:
            return _provenance_text(ref.provenance)

    # Post-action fields: "mix volume", "blow_out volume", etc.
    if step.post_actions:
        for pa in step.post_actions:
            if field_name.startswith(pa.action) and pa.volume and pa.volume.provenance:
                return _provenance_text(pa.volume.provenance)

    return None


def _provenance_text(prov) -> str:
    """Return the human-readable explanation for a Provenance, picking the
    field populated by the schema: cited_text for instruction-sourced,
    positive_reasoning for domain_default/inferred-sourced. See ADR-0005, ADR-0009."""
    return prov.cited_text or prov.positive_reasoning or ""


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
        from nl2protocol.config import enrich_config_with_wells
        enriched_config = enrich_config_with_wells(config)

        schema = ProtocolSpec.model_json_schema()

        system_prompt = REASONING_SYSTEM_PROMPT.format(
            schema=json.dumps(schema, indent=2)
        )
        user_prompt = REASONING_USER_PROMPT.format(
            instruction=instruction,
        )

        try:
            from nl2protocol.spinner import Spinner
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
            from nl2protocol.errors import format_api_error
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

    # ========================================================================
    # PROVENANCE VERIFICATION (Task 5)
    # ========================================================================

    @staticmethod
    def _expand_well_range(start: str, end: str) -> set:
        """Expand a well range like A1-A12 or A1-H1 to a full set."""
        start_row, start_col = start[0], int(start[1:])
        end_row, end_col = end[0], int(end[1:])

        wells = set()
        if start_row == end_row:
            # Same row, column range: A1-A12
            lo, hi = min(start_col, end_col), max(start_col, end_col)
            for c in range(lo, hi + 1):
                wells.add(f"{start_row}{c}")
        elif start_col == end_col:
            # Same column, row range: A1-H1
            lo, hi = min(ord(start_row), ord(end_row)), max(ord(start_row), ord(end_row))
            for r in range(lo, hi + 1):
                wells.add(f"{chr(r)}{start_col}")
        else:
            # Block: rectangle from (min_row, min_col) to (max_row, max_col)
            r_lo, r_hi = min(ord(start_row), ord(end_row)), max(ord(start_row), ord(end_row))
            c_lo, c_hi = min(start_col, end_col), max(start_col, end_col)
            for r in range(r_lo, r_hi + 1):
                for c in range(c_lo, c_hi + 1):
                    wells.add(f"{chr(r)}{c}")
        return wells

    @staticmethod
    def _extract_wells_from_text(instruction: str) -> set:
        """Extract all well positions mentioned in instruction text.

        Handles:
          - Literal wells: A1, B6, H12
          - Ranges with dash: A1-A12, A1-H1
          - Ranges with 'to'/'through': A1 to H1, A1 through A12
          - Column references: column 1, columns 2-8
          - Row references: row A, rows A-H
        """
        wells = set()

        # 1. Explicit ranges: A1-H1, A1 to A12, A1 through H1
        range_pattern = re.compile(
            r'\b([A-H](?:1[0-2]|[1-9]))\s*(?:-|to|through)\s*([A-H](?:1[0-2]|[1-9]))\b',
            re.IGNORECASE
        )
        for m in range_pattern.finditer(instruction):
            wells |= SemanticExtractor._expand_well_range(m.group(1).upper(), m.group(2).upper())

        # 2. Column references: "column 1", "columns 2-8", "columns 2 through 8"
        col_single = re.compile(r'\bcolumns?\s+(1[0-2]|[1-9])\b', re.IGNORECASE)
        for m in col_single.finditer(instruction):
            col = int(m.group(1))
            for row in "ABCDEFGH":
                wells.add(f"{row}{col}")

        col_range = re.compile(
            r'\bcolumns?\s+(1[0-2]|[1-9])\s*(?:-|to|through)\s*(1[0-2]|[1-9])\b',
            re.IGNORECASE
        )
        for m in col_range.finditer(instruction):
            lo, hi = int(m.group(1)), int(m.group(2))
            for col in range(min(lo, hi), max(lo, hi) + 1):
                for row in "ABCDEFGH":
                    wells.add(f"{row}{col}")

        # 3. Row references: "row A", "rows A-H"
        row_single = re.compile(r'\brows?\s+([A-H])\b', re.IGNORECASE)
        for m in row_single.finditer(instruction):
            row = m.group(1).upper()
            for col in range(1, 13):
                wells.add(f"{row}{col}")

        row_range = re.compile(
            r'\brows?\s+([A-H])\s*(?:-|to|through)\s*([A-H])\b',
            re.IGNORECASE
        )
        for m in row_range.finditer(instruction):
            lo, hi = ord(m.group(1).upper()), ord(m.group(2).upper())
            for r in range(min(lo, hi), max(lo, hi) + 1):
                for col in range(1, 13):
                    wells.add(f"{chr(r)}{col}")

        # 4. Literal wells (also catches range endpoints already added above)
        literal = re.compile(r'\b([A-H](?:1[0-2]|[1-9]))\b')
        wells |= set(literal.findall(instruction))

        return wells

    @staticmethod
    def _extract_durations_from_text(instruction: str) -> List[tuple]:
        """Extract all durations mentioned in instruction text.
        Returns list of (value, unit) tuples."""
        pattern = re.compile(
            r'(\d+(?:\.\d+)?)\s*[-]?\s*'
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

    @staticmethod
    def _extract_non_volume_numbers(instruction: str, volume_set: set) -> set:
        """Extract all numbers from instruction text that aren't volumes.

        Finds every standalone number, removes those already identified as
        volumes (uL/mL suffix). The remaining numbers are candidates for
        temperatures, counts, concentrations, etc.
        """
        all_numbers = set()
        for m in re.finditer(r'\b(\d+(?:\.\d+)?)\b', instruction):
            all_numbers.add(float(m.group(1)))
        return all_numbers - volume_set

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

    def _verify_claimed_instruction_provenance(self, spec: ProtocolSpec, instruction: str) -> List[dict]:
        """Verify all source='instruction' claims against the instruction text.

        If the LLM claims a value came from the instruction but the value
        doesn't appear in the text, that's a fabricated provenance claim.
        Severity: 'fabrication'.
        """
        warnings = []
        text_volumes = self._extract_volumes_from_text(instruction)
        text_wells = self._extract_wells_from_text(instruction)
        text_durations = self._extract_durations_from_text(instruction)
        non_volume_numbers = self._extract_non_volume_numbers(instruction, set(text_volumes))
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
                sub = step.substance.value.lower()
                # Check exact match, singular/plural variants, and individual words
                found = (sub in instruction_lower
                         or sub.rstrip('s') in instruction_lower
                         or (sub + 's') in instruction_lower)
                if not found:
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

            # --- Temperature ---
            if step.temperature and step.temperature.provenance.source == "instruction":
                if step.temperature.value not in non_volume_numbers:
                    warnings.append(self._warn(
                        step.order, "temperature",
                        f"{step.temperature.value}°C",
                        "instruction", "fabrication",
                        f"Step {step.order}: claims {step.temperature.value}°C "
                        f"from instruction but not found in text",
                    ))

            # --- LocationRef: labware + wells ---
            for ref, role in [(step.source, "source"), (step.destination, "destination")]:
                if (ref and ref.provenance and ref.provenance.source == "instruction"):
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
            # LocationRef with source='config': resolved_label must be a valid config key.
            # Only check after resolution (Stage 3.5) has filled resolved_label.
            for ref, role in [(step.source, "source"), (step.destination, "destination")]:
                if (ref and ref.provenance and ref.provenance.source == "config"
                        and ref.resolved_label and ref.resolved_label not in labware_labels):
                    warnings.append(self._warn(
                        step.order, f"{role} labware", ref.resolved_label,
                        "config", "fabrication",
                        f"Step {step.order} {role}: claims labware '{ref.resolved_label}' "
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
                ("temperature", step.temperature),
                ("substance", step.substance),
                ("duration", step.duration),
            ]
            for ref, role in [(step.source, "source"), (step.destination, "destination")]:
                if ref and ref.provenance:
                    fields.append((f"{role} location", ref))

            for field_name, field_val in fields:
                prov = getattr(field_val, 'provenance', None) if field_val else None
                if not prov:
                    continue

                val_str = str(field_val.value) if hasattr(field_val, 'value') else str(field_val)
                if prov.source == "inferred":
                    warnings.append(self._warn(
                        step.order, field_name, val_str,
                        "inferred", "low_confidence",
                        f"Step {step.order} {field_name}: inferred value "
                        f"(confidence {prov.confidence}) — needs confirmation",
                    ))
                elif prov.source == "domain_default" and prov.confidence < 0.8:
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
        warnings.extend(self._verify_claimed_instruction_provenance(spec, instruction))
        warnings.extend(self._verify_config_claims(spec, config))
        warnings.extend(self._flag_uncertain_claims(spec))
        return warnings

    # ========================================================================
    # SUFFICIENCY CHECK
    # ========================================================================

    @staticmethod
    def missing_fields(spec: ProtocolSpec) -> List[str]:
        """Check if the spec has enough information to generate a protocol.

        Attempts promotion to CompleteProtocolSpec. If validation fails,
        returns the error messages as a list. Empty list = sufficient.
        """
        try:
            CompleteProtocolSpec.model_validate(spec.model_dump())
            return []
        except ValidationError as e:
            gaps = []
            for err in e.errors():
                msg = err["msg"].removeprefix("Value error, ")
                # The validator joins multiple issues with "; " — split them
                prefix_end = msg.find(": ")
                if prefix_end != -1:
                    body = msg[prefix_end + 2:]
                    gaps.extend(issue.strip() for issue in body.split("; "))
                else:
                    gaps.append(msg)
            return gaps

    # ========================================================================
    # GAP FILLER
    # ========================================================================

    @staticmethod
    def fill_lookup_and_carryover_gaps(spec: ProtocolSpec, config: dict) -> Tuple[ProtocolSpec, List[str]]:
        """Fill exactly two kinds of deterministic gaps the LLM tends to leave.

        Per ADR-0006, this function is INTENTIONALLY narrow. It is NOT a
        general-purpose gap filler. It handles only the two patterns where
        the missing value is *forced by context already in the system* —
        not heuristically guessed. Adding a third case requires a new ADR
        with its own justification.

        The two patterns:

        1. **Lookup gaps** (substance → source location).
           When a step has a `substance` but no `source`, search the
           config's labware contents for a well that lists this substance.
           This is a config lookup keyed on substance name. Filled with
           `Provenance(source="inferred", positive_reasoning="<config path>",
           why_not_in_instruction="<what the instruction omitted>",
           confidence=0.9)`.

        2. **Carryover gaps** (wait_for_temperature → prior set_temperature).
           A `wait_for_temperature` action with no `temperature` value is
           semantically forced — "wait until the module reaches its target"
           can only refer to the most recent `set_temperature` in the same
           protocol. Filled with `Provenance(source="inferred",
           positive_reasoning="Inherited from prior set_temperature (X°C)...",
           why_not_in_instruction="instruction did not re-state the target",
           confidence=0.95)`.

        Both kinds get inferred provenance attached so the report's ▴
        marker + tooltip surface what was filled and why.

        Pre:    `spec` is a ProtocolSpec post-extraction; some steps may
                have null `source` (with substance set) or null
                `temperature` (on wait_for_temperature actions).
        Post:   Returns (filled_spec, fills) where filled_spec is a deep
                copy of spec with applicable gaps populated. fills is a
                list of human-readable strings describing each fill.
                Steps the function CANNOT fill (no config match, no prior
                set_temperature) are left untouched — the strict
                CompleteProtocolSpec validator will catch them later.
        Side effects: None on the input spec (deep-copied).
        Raises: Never.
        """
        from ..models.spec import Provenance, ProvenancedTemperature

        filled = copy.deepcopy(spec)
        fills = []

        # ---- (1) Lookup gaps: substance → source location via config ----
        for step in filled.steps:
            if step.source is None and step.substance and "labware" in config:
                substance_val = step.substance.value
                for label, lw in config["labware"].items():
                    contents = lw.get("contents", {})
                    for well, content_desc in contents.items():
                        if isinstance(content_desc, str) and substance_val.lower() in content_desc.lower():
                            step.source = LocationRef(
                                description=f"{label} (inferred from config)",
                                well=well,
                                provenance=Provenance(
                                    source="inferred",
                                    positive_reasoning=(
                                        f"Substance '{substance_val}' is listed in "
                                        f"config labware '{label}' at well {well}."
                                    ),
                                    why_not_in_instruction=(
                                        f"The instruction names a substance "
                                        f"('{substance_val}') to transfer but does not "
                                        f"state which labware/well it comes from."
                                    ),
                                    confidence=0.9,
                                ),
                            )
                            fills.append(f"Step {step.order}: '{substance_val}' source → {label} well {well}")
                            break
                    if step.source:
                        break

        # ---- (2) Carryover gaps: wait_for_temperature inherits from set_temperature ----
        last_set_temp_celsius = None
        for step in filled.steps:
            if step.action == "set_temperature" and step.temperature is not None:
                last_set_temp_celsius = step.temperature.value
            elif step.action == "wait_for_temperature" and step.temperature is None:
                if last_set_temp_celsius is None:
                    # No prior set_temperature to inherit from. Leave the
                    # gap; the strict CompleteProtocolSpec validator will
                    # surface a clear error to the user.
                    continue
                step.temperature = ProvenancedTemperature(
                    value=last_set_temp_celsius,
                    provenance=Provenance(
                        source="inferred",
                        positive_reasoning=(
                            f"wait_for_temperature inherits from the most "
                            f"recent set_temperature ({last_set_temp_celsius}°C); "
                            f"the action's semantics force this — 'wait until "
                            f"the module reaches its target' can only refer to "
                            f"the most recent set_temperature."
                        ),
                        why_not_in_instruction=(
                            "The instruction said to wait for the temperature "
                            "to stabilize but did not re-state the target "
                            "temperature for this wait."
                        ),
                        confidence=0.95,
                    ),
                )
                fills.append(
                    f"Step {step.order}: wait_for_temperature target → "
                    f"{last_set_temp_celsius}°C (inherited from prior set_temperature)"
                )

        return filled, fills

    @staticmethod
    def validate_schema_against_spec(spec: ProtocolSpec, schema) -> List[str]:
        """Delegate to extraction.schema_builder."""
        return _validate_schema_against_spec(spec, schema)

    @staticmethod
    def spec_to_schema(spec: 'CompleteProtocolSpec', config: dict):
        """Delegate to extraction.schema_builder."""
        return _spec_to_schema(spec, config)

