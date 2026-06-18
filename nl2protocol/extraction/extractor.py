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

MAX_SPEC_REPAIR_ATTEMPTS = 3
_REPAIRABLE_CONTAINERS = ("steps", "initial_contents", "prefilled_labware")



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
            spec = self._validate_with_repair(data, instruction)

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

    def _validate_with_repair(self, data: dict, instruction: str) -> ProtocolSpec:
        """Validate the extracted spec, repairing model self-consistency slips in place.

        Pre:  `data` is the JSON the model emitted for ProtocolSpec (already
              parsed — malformed JSON fails earlier); `instruction` is the
              original natural-language protocol.

        Post: Returns a validated ProtocolSpec. Every rule enforced at this
              stage governs the model's OWN output consistency (source/grounding
              tags, citation presence, provenance fields, step ordering) — none
              can be caused by the instruction, so every ValidationError is a
              repairable extraction slip. Each failed list element (a step /
              initial_content / prefilled_labware entry) is re-asked from the
              model with the exact violated rules, patched in place, and
              re-validated, up to MAX_SPEC_REPAIR_ATTEMPTS rounds. Step ordering
              is renumbered deterministically (a value-free relabel). Value gaps
              (missing volume / location / well) are not enforced here — they
              belong to CompleteProtocolSpec, after gap resolution.

        Raises: ValidationError if repair does not converge within
                MAX_SPEC_REPAIR_ATTEMPTS.
        """
        for _ in range(MAX_SPEC_REPAIR_ATTEMPTS):
            try:
                return ProtocolSpec.model_validate(data)
            except ValidationError as e:
                self._repair_round(data, e.errors(), instruction)
        return ProtocolSpec.model_validate(data)

    def _repair_round(self, data: dict, errors: list, instruction: str) -> None:
        """Apply one repair pass over `data` in place for the given errors."""
        steps = data.get("steps")
        if isinstance(steps, list):
            for i, step in enumerate(steps, start=1):
                if isinstance(step, dict):
                    step["order"] = i

        by_element: Dict[tuple, List[str]] = {}
        for err in errors:
            loc = err.get("loc", ())
            if (len(loc) >= 2 and loc[0] in _REPAIRABLE_CONTAINERS
                    and isinstance(loc[1], int)):
                by_element.setdefault((loc[0], loc[1]), []).append(err.get("msg", ""))

        for (container, index), messages in by_element.items():
            self._reask_element(data, container, index, messages, instruction)

    def _reask_element(self, data: dict, container: str, index: int,
                       messages: List[str], instruction: str) -> None:
        """Re-ask the model to correct one offending list entry, patched in place.

        Hands the model the exact violated rules plus the original instruction
        and asks it to return only the corrected entry. Raises if the reply is
        not a JSON object, so the caller falls back to the normal
        extraction-failure path rather than patching in garbage.
        """
        entry = data[container][index]
        violations = "\n".join(f"- {m}" for m in messages)
        prompt = (
            f"A lab-protocol extraction produced this `{container}` entry, but it "
            "violates internal consistency rules of the spec format. These are "
            "extraction-format mistakes, not problems with the instruction — the "
            "instruction does not dictate these fields. Correct ONLY this entry "
            "so it satisfies the rules, keeping every value the instruction "
            "supports and changing nothing the rules do not force.\n\n"
            f"Original instruction:\n{instruction}\n\n"
            f"Rule violations to fix:\n{violations}\n\n"
            f"Current entry:\n{json.dumps(entry, indent=2)}\n\n"
            "Return ONLY the corrected entry as a single JSON object — no tags, "
            "no prose, no code fence."
        )
        resp = self.client.messages.create(
            model=self.model_name,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        if "```" in text:
            fence = "```json" if "```json" in text else "```"
            text = text.split(fence, 1)[1].split("```", 1)[0].strip()
        data[container][index] = json.loads(text)
        self._emit_reasoning(f"Repairing {container}[{index}]…")

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
