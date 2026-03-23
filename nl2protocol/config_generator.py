"""
config_generator.py

Infer lab configuration from natural language protocol instructions.
Uses Claude to determine required labware, pipettes, and modules.
Uses RAG to retrieve similar configs as few-shot examples.
"""

import json
import os
from typing import Optional, Dict, Any, List
from anthropic import Anthropic
from dotenv import load_dotenv

from .errors import APIKeyError
from .example_store import ExampleStore


CONFIG_INFERENCE_PROMPT = """You are an Opentrons protocol expert. Given a natural language protocol description, infer the minimal lab configuration needed to execute it.
{similar_configs}

Determine:
1. What labware is needed (plates, tipracks, reservoirs, tube racks)
2. What pipettes are needed (consider volumes mentioned)
3. What modules are needed (temperature, magnetic, heater-shaker, thermocycler)

Rules:
- Assign unique slots (1-11) to each item. Thermocycler uses slots 7,8,10,11.
- Match tiprack to pipette volume range (p20 uses 20ul tips, p300 uses 300ul tips, p1000 uses 1000ul tips)
- Use common Opentrons labware load_names
- Only include what's explicitly needed or strongly implied

Common labware load_names:
- Tipracks: opentrons_96_tiprack_20ul, opentrons_96_tiprack_300ul, opentrons_96_tiprack_1000ul
- Plates: corning_96_wellplate_360ul_flat, nest_96_wellplate_100ul_pcr_full_skirt, corning_384_wellplate_112ul_flat
- Reservoirs: nest_12_reservoir_15ml, nest_1_reservoir_195ml
- Tube racks: opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap, opentrons_10_tuberack_falcon_4x50ml_6x15ml_conical
- Deep well: nest_96_wellplate_2ml_deep

Pipette models: p20_single_gen2, p300_single_gen2, p1000_single_gen2, p20_multi_gen2, p300_multi_gen2

Module types: temperature, magnetic, heater_shaker, thermocycler

Protocol description:
{instruction}

Respond with valid JSON only:
{{
    "labware": {{
        "label": {{"load_name": "...", "slot": "1"}},
        ...
    }},
    "pipettes": {{
        "left": {{"model": "...", "tipracks": ["label1"]}},
        "right": {{"model": "...", "tipracks": ["label2"]}}  // optional
    }},
    "modules": {{  // optional, omit if none needed
        "label": {{"module_type": "...", "slot": "..."}}
    }}
}}
"""


class ConfigGenerator:
    """Generate lab configuration from natural language instructions."""

    def __init__(self, model_name: str = "claude-sonnet-4-20250514"):
        load_dotenv()
        self.model_name = model_name
        self._setup_client()
        self._setup_example_store()

    def _setup_client(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise APIKeyError("ANTHROPIC_API_KEY")
        self.client = Anthropic(api_key=api_key)

    def _setup_example_store(self):
        """Initialize the example store for RAG retrieval."""
        try:
            self.example_store = ExampleStore()
        except Exception:
            # RAG is optional - continue without it
            self.example_store = None

    def _get_similar_configs(self, instruction: str, top_k: int = 3) -> str:
        """
        Retrieve similar configs from RAG store and format for prompt.

        Only returns examples that have a valid 'config' field.
        """
        if not self.example_store:
            return ""

        try:
            examples = self.example_store.find_similar(instruction, top_k=top_k + 3)
        except Exception:
            return ""

        if not examples:
            return ""

        # Filter to examples with valid config field
        examples_with_config = [
            ex for ex in examples
            if ex.get("config") and ex["config"].get("labware")
        ][:top_k]

        if not examples_with_config:
            return ""

        lines = ["\n=== SIMILAR CONFIGURATIONS (use as reference) ===\n"]
        for i, ex in enumerate(examples_with_config, 1):
            intent = ex.get("intent", "")[:100]
            if len(ex.get("intent", "")) > 100:
                intent += "..."
            lines.append(f"Example {i}: {intent}")
            lines.append(f"Config: {json.dumps(ex['config'], indent=2)}")
            lines.append("")

        lines.append("=== END EXAMPLES ===\n")
        return "\n".join(lines)

    def generate(self, instruction: str) -> Optional[Dict[str, Any]]:
        """
        Generate lab configuration from a protocol instruction.

        Uses RAG to retrieve similar configs as few-shot examples.

        Args:
            instruction: Natural language protocol description

        Returns:
            Config dict with labware, pipettes, and optionally modules
            None on failure
        """
        try:
            # Get similar configs for few-shot context
            similar_configs = self._get_similar_configs(instruction)

            response = self.client.messages.create(
                model=self.model_name,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": CONFIG_INFERENCE_PROMPT.format(
                        instruction=instruction,
                        similar_configs=similar_configs
                    )
                }]
            )

            result_text = response.content[0].text.strip()

            # Parse JSON response
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0]
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0]

            config = json.loads(result_text.strip())

            # Validate basic structure
            if "labware" not in config or "pipettes" not in config:
                print("Error: Generated config missing required fields")
                return None

            return config

        except json.JSONDecodeError as e:
            print(f"Error parsing generated config: {e}")
            return None
        except Exception as e:
            print(f"Error generating config: {e}")
            return None


def format_config_for_display(config: Dict[str, Any]) -> str:
    """Format config dict for user-friendly display."""
    lines = []
    lines.append("=" * 50)
    lines.append("INFERRED LAB CONFIGURATION")
    lines.append("=" * 50)

    lines.append("\nLABWARE:")
    for label, info in config.get("labware", {}).items():
        lines.append(f"  Slot {info['slot']}: {label}")
        lines.append(f"          ({info['load_name']})")

    lines.append("\nPIPETTES:")
    for mount, info in config.get("pipettes", {}).items():
        tipracks = ", ".join(info.get("tipracks", []))
        lines.append(f"  {mount.upper()}: {info['model']}")
        lines.append(f"          tipracks: [{tipracks}]")

    if config.get("modules"):
        lines.append("\nMODULES:")
        for label, info in config["modules"].items():
            lines.append(f"  Slot {info['slot']}: {label} ({info['module_type']})")

    lines.append("\n" + "=" * 50)

    return "\n".join(lines)


def generate_config(instruction: str) -> Optional[Dict[str, Any]]:
    """Convenience function to generate config from instruction."""
    generator = ConfigGenerator()
    return generator.generate(instruction)
