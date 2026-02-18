import json
import os
from typing import Optional, Dict, Any
import google.generativeai as genai
from models import ProtocolSchema
from dotenv import load_dotenv


def get_well_info(load_name: str) -> Dict[str, Any]:
    """
    Derive well layout from Opentrons load_name.
    Returns rows, columns, and the full list of valid well names.
    """
    load_name = load_name.lower()

    # Determine grid size from load_name patterns
    if "_384_" in load_name:
        rows, cols = 16, 24  # A-P, 1-24
    elif "_96_" in load_name:
        rows, cols = 8, 12   # A-H, 1-12
    elif "_48_" in load_name:
        rows, cols = 6, 8    # A-F, 1-8
    elif "_24_" in load_name:
        rows, cols = 4, 6    # A-D, 1-6
    elif "_12_reservoir" in load_name or "_12_well_reservoir" in load_name:
        rows, cols = 1, 12   # A only, 1-12
    elif "_12_" in load_name:
        rows, cols = 3, 4    # A-C, 1-4
    elif "_6_" in load_name:
        rows, cols = 2, 3    # A-B, 1-3
    elif "_1_reservoir" in load_name or "_1_well" in load_name:
        rows, cols = 1, 1    # A1 only
    else:
        # Default fallback for unknown labware
        rows, cols = 8, 12

    # Generate well names
    row_letters = [chr(ord('A') + i) for i in range(rows)]
    well_names = [f"{r}{c}" for r in row_letters for c in range(1, cols + 1)]

    return {
        "rows": rows,
        "cols": cols,
        "row_range": f"A-{row_letters[-1]}" if rows > 1 else "A",
        "col_range": f"1-{cols}",
        "well_count": len(well_names),
        "valid_wells": well_names
    }


def enrich_config_with_wells(config: Dict) -> Dict:
    """Add well information to each labware in the config."""
    enriched = config.copy()
    if "labware" in enriched:
        for name, labware in enriched["labware"].items():
            if "load_name" in labware:
                well_info = get_well_info(labware["load_name"])
                labware["well_info"] = {
                    "valid_rows": well_info["row_range"],
                    "valid_columns": well_info["col_range"],
                    "total_wells": well_info["well_count"],
                    "example_wells": well_info["valid_wells"][:5] + ["..."] + well_info["valid_wells"][-2:] if well_info["well_count"] > 7 else well_info["valid_wells"]
                }
    return enriched


def get_protocol_json_schema() -> str:
    """Get the JSON Schema for ProtocolSchema to include in prompts."""
    schema = ProtocolSchema.model_json_schema()
    return json.dumps(schema, indent=2)


# =============================================================================
# FEW-SHOT EXAMPLES
# =============================================================================

FEW_SHOT_EXAMPLES = """
=== EXAMPLE 1: SERIAL DILUTION ===

USER GOAL: "Perform a 2x serial dilution across row A (wells A1-A8). Stock solution is in the reservoir at A1. Pre-fill destination wells with 100uL diluent first."

LAB CONFIG:
- reservoir in slot 1 (label: "Reservoir") - contains diluent in A1, stock in A2
- plate in slot 2 (label: "Dilution Plate") - 96-well, destination
- tiprack in slot 3 (label: "Tips")
- left pipette: p300_single_gen2

OUTPUT:
{
  "protocol_name": "Serial Dilution 2x Row A",
  "author": "Biolab AI",
  "labware": [
    {"slot": "1", "load_name": "nest_12_reservoir_15ml", "label": "Reservoir"},
    {"slot": "2", "load_name": "corning_96_wellplate_360ul_flat", "label": "Dilution Plate"},
    {"slot": "3", "load_name": "opentrons_96_tiprack_300ul", "label": "Tips"}
  ],
  "pipettes": [
    {"mount": "left", "model": "p300_single_gen2", "tipracks": ["Tips"]}
  ],
  "commands": [
    {"command_type": "distribute", "pipette": "left", "source_labware": "Reservoir", "source_well": "A1", "dest_labware": "Dilution Plate", "dest_wells": ["A2", "A3", "A4", "A5", "A6", "A7", "A8"], "volume": 100, "new_tip": "once"},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Reservoir", "source_well": "A2", "dest_labware": "Dilution Plate", "dest_well": "A1", "volume": 200, "new_tip": "always", "mix_after": [3, 100]},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Dilution Plate", "source_well": "A1", "dest_labware": "Dilution Plate", "dest_well": "A2", "volume": 100, "new_tip": "always", "mix_after": [3, 100]},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Dilution Plate", "source_well": "A2", "dest_labware": "Dilution Plate", "dest_well": "A3", "volume": 100, "new_tip": "always", "mix_after": [3, 100]},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Dilution Plate", "source_well": "A3", "dest_labware": "Dilution Plate", "dest_well": "A4", "volume": 100, "new_tip": "always", "mix_after": [3, 100]},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Dilution Plate", "source_well": "A4", "dest_labware": "Dilution Plate", "dest_well": "A5", "volume": 100, "new_tip": "always", "mix_after": [3, 100]},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Dilution Plate", "source_well": "A5", "dest_labware": "Dilution Plate", "dest_well": "A6", "volume": 100, "new_tip": "always", "mix_after": [3, 100]},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Dilution Plate", "source_well": "A6", "dest_labware": "Dilution Plate", "dest_well": "A7", "volume": 100, "new_tip": "always", "mix_after": [3, 100]},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Dilution Plate", "source_well": "A7", "dest_labware": "Dilution Plate", "dest_well": "A8", "volume": 100, "new_tip": "always", "mix_after": [3, 100]}
  ]
}

=== EXAMPLE 2: PLATE REPLICATION ===

USER GOAL: "Copy 50uL from each well in column 1 of the source plate to the same wells in the destination plate (A1-H1)."

LAB CONFIG:
- source plate in slot 1 (label: "Source Plate") - 96-well with samples
- dest plate in slot 2 (label: "Dest Plate") - 96-well, empty
- tiprack in slot 3 (label: "Tips")
- left pipette: p300_single_gen2

OUTPUT:
{
  "protocol_name": "Plate Replication Column 1",
  "author": "Biolab AI",
  "labware": [
    {"slot": "1", "load_name": "corning_96_wellplate_360ul_flat", "label": "Source Plate"},
    {"slot": "2", "load_name": "corning_96_wellplate_360ul_flat", "label": "Dest Plate"},
    {"slot": "3", "load_name": "opentrons_96_tiprack_300ul", "label": "Tips"}
  ],
  "pipettes": [
    {"mount": "left", "model": "p300_single_gen2", "tipracks": ["Tips"]}
  ],
  "commands": [
    {"command_type": "transfer", "pipette": "left", "source_labware": "Source Plate", "source_well": "A1", "dest_labware": "Dest Plate", "dest_well": "A1", "volume": 50, "new_tip": "always"},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Source Plate", "source_well": "B1", "dest_labware": "Dest Plate", "dest_well": "B1", "volume": 50, "new_tip": "always"},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Source Plate", "source_well": "C1", "dest_labware": "Dest Plate", "dest_well": "C1", "volume": 50, "new_tip": "always"},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Source Plate", "source_well": "D1", "dest_labware": "Dest Plate", "dest_well": "D1", "volume": 50, "new_tip": "always"},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Source Plate", "source_well": "E1", "dest_labware": "Dest Plate", "dest_well": "E1", "volume": 50, "new_tip": "always"},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Source Plate", "source_well": "F1", "dest_labware": "Dest Plate", "dest_well": "F1", "volume": 50, "new_tip": "always"},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Source Plate", "source_well": "G1", "dest_labware": "Dest Plate", "dest_well": "G1", "volume": 50, "new_tip": "always"},
    {"command_type": "transfer", "pipette": "left", "source_labware": "Source Plate", "source_well": "H1", "dest_labware": "Dest Plate", "dest_well": "H1", "volume": 50, "new_tip": "always"}
  ]
}

=== EXAMPLE 3: REAGENT DISTRIBUTION ===

USER GOAL: "Add 100uL of buffer from reservoir A1 to all wells in row A of the plate (A1-A12)."

LAB CONFIG:
- reservoir in slot 1 (label: "Reagent Reservoir") - buffer in A1
- plate in slot 2 (label: "Assay Plate") - 96-well destination
- tiprack in slot 3 (label: "Tips")
- left pipette: p300_single_gen2

OUTPUT:
{
  "protocol_name": "Buffer Distribution Row A",
  "author": "Biolab AI",
  "labware": [
    {"slot": "1", "load_name": "nest_12_reservoir_15ml", "label": "Reagent Reservoir"},
    {"slot": "2", "load_name": "corning_96_wellplate_360ul_flat", "label": "Assay Plate"},
    {"slot": "3", "load_name": "opentrons_96_tiprack_300ul", "label": "Tips"}
  ],
  "pipettes": [
    {"mount": "left", "model": "p300_single_gen2", "tipracks": ["Tips"]}
  ],
  "commands": [
    {"command_type": "distribute", "pipette": "left", "source_labware": "Reagent Reservoir", "source_well": "A1", "dest_labware": "Assay Plate", "dest_wells": ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10", "A11", "A12"], "volume": 100, "new_tip": "once"}
  ]
}

=== END OF EXAMPLES ===
"""


class ProtocolParser:
    """
    The 'Reasoning' engine.
    It translates Natural Language + CSV Data + Config into a rigid ProtocolSchema using Gemini.
    """

    def __init__(self, config_path: str = "lab_config.json", model_name: str = "gemini-2.0-flash"):
        load_dotenv()
        self.model_name = model_name
        self.config_path = config_path
        self.config = self._load_config()
        self._setup_gemini()

    def _load_config(self) -> Dict:
        try:
            with open(self.config_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"Warning: Config file {self.config_path} not found.")
            return {}

    def _setup_gemini(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable not set.")
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(self.model_name)

    def parse_intent(self, prompt: str, csv_path: Optional[str] = None, error_log: Optional[str] = None) -> Optional[ProtocolSchema]:

        # 1. Prepare Inputs
        csv_content = ""
        if csv_path:
            try:
                with open(csv_path, 'r') as f:
                    csv_content = f.read()
            except Exception as e:
                print(f"Error reading CSV: {e}")

        # 2. Get JSON Schema
        json_schema = get_protocol_json_schema()

        # 3. Construct Prompt
        system_instruction = f"""
You are an expert Opentrons robot engineer. Your job is to translate a scientist's natural language goal into a strictly typed JSON protocol.

RULES:
1. You must output VALID JSON matching the ProtocolSchema structure defined below.
2. Use the provided 'Lab Config' to identify labware slots and pipette mounts. Do not invent new labware.
3. Use the appropriate command types for the task.
4. If an 'Error Log' is provided, fix the previous protocol to resolve the error.
5. CRITICAL - WELL NAMES: Each labware has specific valid wells listed in its 'well_info'.
   - Use ONLY wells from the valid range (e.g., A1-H12 for 96-well plates)
   - Well format is always: Row letter + Column number (e.g., A1, B3, H12)
   - Row letters go A, B, C... (top to bottom)
   - Column numbers go 1, 2, 3... (left to right)
6. Use "new_tip": "always" when transferring between different samples to avoid cross-contamination.
7. Use "mix_after" for serial dilutions to ensure proper mixing.

JSON SCHEMA (your output MUST conform to this):
{json_schema}

COMMAND TYPES QUICK REFERENCE:

Atomic Commands:
- aspirate: {{"command_type": "aspirate", "pipette": "left", "labware": "...", "well": "A1", "volume": 100}}
- dispense: {{"command_type": "dispense", "pipette": "left", "labware": "...", "well": "A1", "volume": 100}}
- mix: {{"command_type": "mix", "pipette": "left", "labware": "...", "well": "A1", "volume": 50, "repetitions": 3}}
- blow_out: {{"command_type": "blow_out", "pipette": "left"}}
- touch_tip: {{"command_type": "touch_tip", "pipette": "left", "labware": "...", "well": "A1"}}
- air_gap: {{"command_type": "air_gap", "pipette": "left", "volume": 20}}
- pick_up_tip: {{"command_type": "pick_up_tip", "pipette": "left"}}
- drop_tip: {{"command_type": "drop_tip", "pipette": "left"}}
- return_tip: {{"command_type": "return_tip", "pipette": "left"}}

Complex Commands:
- transfer: {{"command_type": "transfer", "pipette": "left", "source_labware": "...", "source_well": "A1", "dest_labware": "...", "dest_well": "B1", "volume": 100, "new_tip": "always", "mix_before": [3, 50], "mix_after": [3, 50]}}
- distribute: {{"command_type": "distribute", "pipette": "left", "source_labware": "...", "source_well": "A1", "dest_labware": "...", "dest_wells": ["A1","A2","A3"], "volume": 50, "new_tip": "once"}}
- consolidate: {{"command_type": "consolidate", "pipette": "left", "source_labware": "...", "source_wells": ["A1","A2","A3"], "dest_labware": "...", "dest_well": "A1", "volume": 50, "new_tip": "once"}}

NOTE on mix_before/mix_after: Format is [repetitions, volume_in_ul]. Example: [3, 50] means mix 3 times with 50µL.

{FEW_SHOT_EXAMPLES}

Now generate a protocol for the user's goal below. Output ONLY valid JSON, no explanations.
"""

        # Enrich config with well information for each labware
        enriched_config = enrich_config_with_wells(self.config)

        user_message = f"""
GOAL: {prompt}

LAB CONFIG (Physical Setup with Valid Wells):
{json.dumps(enriched_config, indent=2)}

IMPORTANT - HOW TO READ THE CONFIG:
- 'well_info': Shows valid wells for each labware. Use ONLY wells from the valid range.
- 'contents': Shows what liquid/sample is in each well (e.g., "A1": "PBS buffer", "A2": "Stock solution").
- 'label': The labware name to use in commands.

RESOLVING REFERENCES IN THE GOAL:
The user's goal may refer to labware or liquids by:
1. Labware label (e.g., "Reservoir", "Source Plate")
2. Contents name (e.g., "stock solution", "PBS", "diluent")
3. Common lab names (e.g., "buffer", "samples", "tips")

Look at the config to identify which labware and well contains the referenced item.
Example: If goal says "Transfer stock solution" and config shows Reservoir A2 contains "Stock solution (100µM)",
         then source_labware="Reservoir" and source_well="A2".

CSV DATA (Experiment Parameters):
{csv_content if csv_content else "None"}

ERROR LOG (From previous run, if any):
{error_log if error_log else "None"}
"""

        # 4. Call Gemini
        try:
            response = self.model.generate_content(
                f"{system_instruction}\n\n{user_message}",
                generation_config={"response_mime_type": "application/json"}
            )

            # 5. Parse & Validate
            json_str = response.text
            # Clean up potential markdown code blocks if the model adds them despite JSON mode
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]

            protocol_data = json.loads(json_str)
            # Validate with config context for cross-checking
            return ProtocolSchema.model_validate(protocol_data, context={'config': self.config})

        except Exception as e:
            print(f"LLM Reasoning Failed: {e}")
            return None
