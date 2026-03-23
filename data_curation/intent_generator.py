"""
intent_generator.py

Generate natural language intent descriptions from Opentrons protocol scripts.
Uses Claude to summarize what a protocol does.

Generates 3 style variations per script to expand the dataset:
1. Technical/formal - precise lab terminology
2. Casual/conversational - how someone might verbally describe it
3. Concise/minimal - shortest form that captures intent

Usage:
    from intent_generator import generate_intents
    intents = generate_intents(script_source)  # Returns list of 3 variations
"""

import os
import json
from typing import Optional, Dict, Any, List, List
from anthropic import Anthropic
from dotenv import load_dotenv


MULTI_STYLE_PROMPT = """You are analyzing an Opentrons robot protocol script. Generate 3 different natural language descriptions of what this protocol does - the kind of instructions a scientist might give to request this protocol.

Write 3 variations in different styles:

1. TECHNICAL: Use precise lab terminology, be specific about volumes, wells, and techniques. Example: "Execute a 1:2 serial dilution series across wells A1-A12, transferring 100µL per step with mixing"

2. CASUAL: How someone might verbally describe it in conversation. Example: "Do a serial dilution going across the first row, mixing each time"

3. CONCISE: Shortest form that still captures the essential task. Example: "Serial dilution row A, 1:2 ratio"

Protocol script:
```python
{script}
```

Respond with JSON only:
{{"technical": "...", "casual": "...", "concise": "..."}}"""


SINGLE_STYLE_PROMPT = """You are analyzing an Opentrons robot protocol script. Your task is to write a concise natural language description of what this protocol does - the kind of instruction a scientist would give to request this protocol.

Focus on:
- The main goal/purpose (e.g., serial dilution, sample transfer, reagent distribution)
- Key parameters (volumes, well locations, number of samples)
- Any special steps (mixing, incubation, pauses)

Write it as a direct instruction, like how a scientist would describe the task they want to accomplish. Keep it to 1-3 sentences.

Examples of good intents:
- "Transfer 100uL from wells A1-A8 of the source plate to the corresponding wells of the destination plate"
- "Perform a 2x serial dilution across row A, starting with 200uL of stock solution"
- "Distribute 50uL of reagent from the reservoir to all wells in columns 1-6 of the assay plate"

Protocol script:
```python
{script}
```

Write ONLY the natural language intent, nothing else:"""


class IntentGenerator:
    """Generate NL intents from protocol scripts using Claude."""

    def __init__(self, model_name: str = "claude-sonnet-4-20250514"):
        load_dotenv()
        self.model_name = model_name
        self._client = None

    @property
    def client(self) -> Anthropic:
        if self._client is None:
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY not set")
            self._client = Anthropic(api_key=api_key)
        return self._client

    def generate(self, script: str) -> Optional[str]:
        """
        Generate a single natural language intent from a protocol script.

        Args:
            script: The Python protocol source code

        Returns:
            Natural language intent string, or None on failure
        """
        try:
            response = self.client.messages.create(
                model=self.model_name,
                max_tokens=256,
                messages=[{
                    "role": "user",
                    "content": SINGLE_STYLE_PROMPT.format(script=script)
                }]
            )
            return response.content[0].text.strip()
        except Exception as e:
            print(f"Error generating intent: {e}")
            return None

    def generate_multi_style(self, script: str) -> Optional[Dict[str, str]]:
        """
        Generate 3 style variations of intent from a protocol script.

        Args:
            script: The Python protocol source code

        Returns:
            Dict with keys: technical, casual, concise
            None on failure
        """
        try:
            response = self.client.messages.create(
                model=self.model_name,
                max_tokens=512,
                messages=[{
                    "role": "user",
                    "content": MULTI_STYLE_PROMPT.format(script=script)
                }]
            )

            text = response.content[0].text.strip()

            # Parse JSON response
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]

            return json.loads(text.strip())

        except Exception as e:
            print(f"Error generating multi-style intents: {e}")
            return None

    def generate_all(self, script: str) -> List[str]:
        """
        Generate all intent variations as a flat list.

        Args:
            script: The Python protocol source code

        Returns:
            List of 3 intent strings [technical, casual, concise]
            Empty list on failure
        """
        result = self.generate_multi_style(script)
        if result:
            return [
                result.get("technical", ""),
                result.get("casual", ""),
                result.get("concise", "")
            ]
        return []

    def generate_from_schema(self, schema: Dict[str, Any]) -> Optional[str]:
        """
        Generate intent from an extracted schema (alternative to full script).

        Args:
            schema: Dict with labware, pipettes, commands

        Returns:
            Natural language intent string
        """
        # Build a simplified description for the LLM
        summary_parts = []

        if schema.get("protocol_name"):
            summary_parts.append(f"Protocol: {schema['protocol_name']}")

        if schema.get("labware"):
            lw_list = [f"{lw.get('label', lw.get('load_name'))} in slot {lw['slot']}"
                      for lw in schema["labware"]]
            summary_parts.append(f"Labware: {', '.join(lw_list)}")

        if schema.get("pipettes"):
            pip_list = [f"{p['model']} on {p['mount']}" for p in schema["pipettes"]]
            summary_parts.append(f"Pipettes: {', '.join(pip_list)}")

        if schema.get("commands"):
            cmd_summary = self._summarize_commands(schema["commands"])
            summary_parts.append(f"Commands: {cmd_summary}")

        prompt = f"""Based on this protocol summary, write a natural language description of what a scientist would request to create this protocol. Write it as a direct instruction in 1-3 sentences.

{chr(10).join(summary_parts)}

Write ONLY the natural language intent:"""

        try:
            response = self.client.messages.create(
                model=self.model_name,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text.strip()
        except Exception as e:
            print(f"Error generating intent from schema: {e}")
            return None

    def _summarize_commands(self, commands: list) -> str:
        """Create a brief summary of commands."""
        cmd_counts = {}
        for cmd in commands:
            cmd_type = cmd.get("command_type", "unknown")
            cmd_counts[cmd_type] = cmd_counts.get(cmd_type, 0) + 1

        parts = [f"{count}x {cmd_type}" for cmd_type, count in cmd_counts.items()]
        return ", ".join(parts)


def generate_intent(script: str) -> Optional[str]:
    """Convenience function to generate single intent from a script."""
    generator = IntentGenerator()
    return generator.generate(script)


def generate_intents(script: str) -> List[str]:
    """Convenience function to generate all 3 style variations."""
    generator = IntentGenerator()
    return generator.generate_all(script)


def generate_intent_from_schema(schema: Dict[str, Any]) -> Optional[str]:
    """Convenience function to generate intent from a schema."""
    generator = IntentGenerator()
    return generator.generate_from_schema(schema)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python intent_generator.py <protocol.py>")
        sys.exit(1)

    with open(sys.argv[1], 'r') as f:
        script = f.read()

    print("Generating 3 style variations...\n")

    generator = IntentGenerator()
    result = generator.generate_multi_style(script)

    if result:
        print("TECHNICAL:")
        print(f"  {result.get('technical', 'N/A')}\n")
        print("CASUAL:")
        print(f"  {result.get('casual', 'N/A')}\n")
        print("CONCISE:")
        print(f"  {result.get('concise', 'N/A')}")
    else:
        print("Failed to generate intents")
        sys.exit(1)
