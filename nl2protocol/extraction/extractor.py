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
import shutil
import sys
from typing import Annotated, List, Optional, Literal, Dict

from anthropic import Anthropic
from pydantic import BaseModel, Field, ValidationError, model_validator

from nl2protocol.citing import cite_covers_well
from nl2protocol.constants import DEFAULT_MODEL


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

    def __init__(self, client: Anthropic, model_name: str = DEFAULT_MODEL,
                 reporter=None):
        self.client = client
        self.model_name = model_name
        # Optional reporter (nl2protocol.reporting.Reporter). When the pipeline
        # runs in live/web mode this is the WebSocketReporter, so the streamed
        # reasoning reaches the browser as reasoning_delta events. In the CLI
        # it's the silent ConsoleReporter (the Spinner drives the terminal),
        # so emitting is a harmless no-op there.
        self.reporter = reporter

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
            from nl2protocol.for_cli.spinner import Spinner
            # Stream the call so the user sees the model reasoning live (the model
            # emits <reasoning> before <spec>) and so we can safely raise max_tokens
            # — streaming is the supported path above ~16K (it sidesteps the SDK's
            # HTTP-timeout guard). 32000 is well within Sonnet 4.6's 64K ceiling.
            full_response = ""
            spec_seen = False
            last_emit_len = 0   # accumulated length at the last reasoning_delta emit
            with Spinner("Reasoning through protocol...") as spinner:
                with self.client.messages.stream(
                    model=self.model_name,
                    max_tokens=32000,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}]
                ) as stream:
                    for delta in stream.text_stream:
                        full_response += delta
                        if not spec_seen and "<spec>" in full_response:
                            # JSON isn't useful to show — switch to an assembling state.
                            spec_seen = True
                            spinner.update("Assembling spec…")
                            self._emit_reasoning("Assembling spec…")
                        elif not spec_seen:
                            compact = self._compact_reasoning(full_response)
                            spinner.update(compact)
                            # Throttle browser events by accumulated length so we
                            # don't flood the WebSocket queue (CLI uses the spinner
                            # above, which is fine to update every token).
                            if len(full_response) - last_emit_len >= 60:
                                last_emit_len = len(full_response)
                                self._emit_reasoning(compact)
                    final = stream.get_final_message()

            full_response = full_response.strip()

            # Streaming lets us raise max_tokens, but a truncation can still happen
            # on very large protocols. Make it an explicit, legible error instead of
            # an opaque downstream JSON parse failure.
            if final.stop_reason == "max_tokens":
                raise ValueError(
                    "extraction truncated at max_tokens — "
                    "raise max_tokens or simplify the instruction"
                )

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

    def _emit_reasoning(self, text: str) -> None:
        """Push a one-line reasoning update to the reporter, if one is wired.

        Lands in the browser's live indicator sub-line via a `reasoning_delta`
        event (same surface as `pipeline_progress`). No-op when no reporter is
        set (CLI mode) or if emission fails — never blocks extraction.
        """
        if self.reporter is None:
            return
        try:
            from nl2protocol.reporting import StageEvent
            self.reporter.emit(StageEvent(
                kind="reasoning_delta",
                data={"text": text},
                stage_name="stage_2_extraction",
            ))
        except Exception:
            pass

    @staticmethod
    def _compact_reasoning(accumulated: str) -> str:
        """A one-line live view of the reasoning so far: the latest non-empty
        line, stripped of the <reasoning> open tag, truncated to terminal width.
        """
        text = accumulated.replace("<reasoning>", "")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        latest = lines[-1] if lines else "Reasoning through protocol..."
        # Leave a margin for the spinner prefix ("  X ") and a trailing ellipsis.
        width = max(20, shutil.get_terminal_size((80, 24)).columns - 6)
        if len(latest) > width:
            latest = latest[: width - 1] + "…"
        return latest

    @staticmethod
    def _save_debug_output(full_response: Optional[str], spec_json: Optional[str], error: Exception):
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

    @staticmethod
    def _parse_response(response: str) -> tuple[str, str]:
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
