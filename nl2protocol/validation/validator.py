"""
validator.py — LLM-based protocol validation.

Cross-checks the generated protocol against the original instruction by
giving the LLM the per-step breakdown, well state warnings, and config
context. Catches semantic mismatches the Opentrons simulator can't detect
(wrong substances, wrong wells, missing steps).
"""

import json
import os
from dataclasses import dataclass

from anthropic import Anthropic
from dotenv import load_dotenv


@dataclass
class ValidationResult:
    """Result from LLM validation of generated protocol."""
    matches: bool
    confidence: float
    issues: list
    summary: str

    def __str__(self) -> str:
        status = "MATCH" if self.matches else "MISMATCH"
        result = f"[{status}] Confidence: {self.confidence:.0%}"
        if self.issues:
            result += f"\nIssues: {', '.join(self.issues)}"
        result += f"\nSummary: {self.summary}"
        return result


VALIDATION_PROMPT = """You are validating an Opentrons protocol by cross-checking the original instruction against what was actually generated.

ORIGINAL INSTRUCTION:
{intent}

HARDWARE CONFIG:
{config_summary}

INITIAL WELL STATE (what's in wells before the protocol starts):
{initial_state}

PER-STEP BREAKDOWN (each instruction step and the commands it produced):
{step_breakdown}

WELL STATE WARNINGS (issues detected during command generation):
{well_warnings}

IMPORTANT CONTEXT:
- Labware with "on_module" shares the module's slot — this is NOT a slot conflict.
- One instruction step expanding to many commands is normal (e.g. "add to A1-H3" = 24 transfers).
- The user's informal labware names (e.g. "tube rack") may map to different Opentrons load names (e.g. a deep well plate). Focus on whether the actions are correct, not naming.
- Well state warnings about assumed volumes (well capacity) mean the instruction didn't specify a volume — these are informational, not errors.

Cross-check:
1. Does each instruction step have corresponding commands with correct action, volume, and wells?
2. Are source/destination labware assignments correct for each step?
3. Are there missing steps, extra steps, or wrong volumes?
4. Do well state warnings indicate real problems (e.g. aspirating from truly empty wells)?

Respond with JSON only:
{{"matches": true/false, "confidence": 0.0-1.0, "issues": ["specific issue 1", ...], "summary": "one sentence"}}
"""


def validate_protocol(
    intent: str,
    spec,
    schema,
    config: dict,
    well_state_warnings: list = None,
    step_summaries: list = None,
) -> ValidationResult:
    """
    Ask Claude to validate the generated protocol against the original instruction.

    Cross-checks per-step commands, well state warnings, and hardware config
    to catch mismatches the simulator can't detect (wrong substances, wrong wells).
    """
    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return ValidationResult(
            matches=True,
            confidence=1.0,
            issues=[],
            summary="Validation skipped (no API key)"
        )

    client = Anthropic(api_key=api_key)

    # Config summary: labware (with on_module), pipettes, modules
    config_lines = []
    for label, lw in config.get("labware", {}).items():
        line = f"  {label}: {lw.get('load_name', '?')} in slot {lw.get('slot', '?')}"
        if lw.get("on_module"):
            line += f" (on module '{lw['on_module']}' — shares module slot)"
        config_lines.append(line)
    for mount, pip in config.get("pipettes", {}).items():
        config_lines.append(f"  {mount} mount: {pip.get('model', '?')}")
    for label, mod in config.get("modules", {}).items():
        config_lines.append(f"  module '{label}': {mod.get('module_type', '?')} in slot {mod.get('slot', '?')}")
    config_summary = "\n".join(config_lines) or "  (none)"

    # Initial well state from spec
    initial_lines = []
    for ic in spec.initial_contents:
        vol_str = f"{ic.volume_ul}uL" if ic.volume_ul else "volume not stated"
        initial_lines.append(f"  {ic.labware} well {ic.well}: {ic.substance} ({vol_str})")
    for pf in spec.prefilled_labware:
        initial_lines.append(f"  {pf.labware}: all wells prefilled with {pf.substance} ({pf.volume_ul}uL)")
    initial_state = "\n".join(initial_lines) or "  (none declared)"

    # Per-step breakdown
    step_lines = []
    for s in (step_summaries or []):
        line = f"  Step {s['step']}: {s['description']}"
        line += f" → {s['commands_generated']} command(s)"
        step_lines.append(line)
    step_breakdown = "\n".join(step_lines) or "  (no steps)"

    # Well state warnings
    if well_state_warnings:
        well_warnings = "\n".join(f"  - {w}" for w in well_state_warnings)
    else:
        well_warnings = "  (none)"

    prompt = VALIDATION_PROMPT.format(
        intent=intent,
        config_summary=config_summary,
        initial_state=initial_state,
        step_breakdown=step_breakdown,
        well_warnings=well_warnings,
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )

        result_text = response.content[0].text.strip()

        # Parse JSON response
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0]
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0]

        result = json.loads(result_text.strip())

        return ValidationResult(
            matches=result.get("matches", True),
            confidence=result.get("confidence", 1.0),
            issues=result.get("issues", []),
            summary=result.get("summary", "")
        )

    except Exception as e:
        return ValidationResult(
            matches=True,
            confidence=0.5,
            issues=["Validation failed due to error"],
            summary="Could not validate protocol"
        )
