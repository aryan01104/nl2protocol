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

import json
import re
import sys
from typing import Annotated, List, Optional, Literal, Dict

from anthropic import Anthropic
from pydantic import BaseModel, Field, ValidationError, model_validator

from nl2protocol.citing import cite_covers_well


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
    # LocationRef carries two provenances (description + wells); prefer the
    # wells one when the field name targets wells, else the description one.
    for prefix, ref in [("source", step.source), ("destination", step.destination), ("dest", step.destination)]:
        if not (field_name.startswith(prefix) and ref):
            continue
        prov = ref.wells_provenance if "well" in field_name else ref.description_provenance
        if prov:
            return _provenance_text(prov)

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


# Review states that terminate the fabrication-detection lifecycle for a
# Provenance. Once a Provenance reaches one of these states, the cited_text
# substring checks in `_verify_claimed_instruction_provenance` are skipped —
# the user (or reviewer) has already passed judgment on this value and a
# re-check would only re-raise a gap they already resolved, causing the
# orchestrator loop to bounce. See ADR (history-respecting verifier).
TERMINAL_REVIEW_STATUSES = frozenset({
    "user_confirmed",
    "user_edited",
    "user_accepted_suggestion",
    "user_overrode_fabrication",
    "reviewed_agree",
})


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

    def extract(self, instruction: str) -> Optional[ProtocolSpec]:
        """Reason through the instruction and produce a ProtocolSpec.

        Returns ProtocolSpec on success, None on failure (fail-fast).
        """
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
    def validate_schema_against_spec(spec: ProtocolSpec, schema) -> List[str]:
        """Delegate to extraction.schema_builder."""
        return _validate_schema_against_spec(spec, schema)

    @staticmethod
    def spec_to_schema(spec: 'CompleteProtocolSpec', config: dict):
        """Delegate to extraction.schema_builder."""
        return _spec_to_schema(spec, config)

