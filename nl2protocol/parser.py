import json
import os
from typing import Optional, Dict, Any
from anthropic import Anthropic
from .models import ProtocolSchema
from .errors import APIKeyError, ConfigFileError
from dotenv import load_dotenv

"""
parser.py

file that contains functions for STEP 1 of the pipeline
the main artifact of this file is 
    - ProtocolParser class
    - its parse_intent method: (prompt, config, error, data) -> protocol schema objects

 -> if we create a PP object and then run the parse_intent method we do the following:
 - check if the config is valid
 - check if the nl is valid ?
 - check if the data is valid ?

 - enrich the config with well info, normalize the names

 - feed this into Claude with examples and clear instructions
 - get an output which is mapped to a protocol schema object
"""


########## 1. config enrichment: normalize labbware labels and keys, add well info ##########


def get_well_info(load_name: str) -> Dict[str, Any]:
    """
    Derive well layout from Opentrons load_name.

    Takes: labware's load_name
    Returns: dict of rows, columns, and the full list of valid well names.
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


def normalize_config(config: Dict) -> Dict:
    """
    Normalize config so that label = dict_key for all labware.
    This ensures consistent referencing throughout the system.
    """
    normalized = json.loads(json.dumps(config))  # Deep copy

    if "labware" in normalized:
        for key, labware in normalized["labware"].items():
            # Set label to dict key (overwrite any existing label)
            labware["label"] = key

    return normalized


def enrich_config_with_wells(config: Dict) -> Dict:
    """Add well information to each labware in the config."""
    # First normalize the config
    enriched = normalize_config(config)

    if "labware" in enriched:
        for _, labware in enriched["labware"].items():
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




class ProtocolParser:
    """
    Step 1 of application : inputs -> schema

    takes: Natural language instruction, CSV Data, Config
    returns: protocol schema, as defined in models.py
    """

    def __init__(self, config_path: str = "lab_config.json", model_name: str = "claude-sonnet-4-20250514", use_rag: bool = True):
        load_dotenv()
        self.model_name = model_name
        self.config_path = config_path
        self.use_rag = use_rag
        self.last_failed_output = None  # Store failed JSON for spot-fix retries
        self.error_history = []  # Track all errors across retry attempts
        self.config = self._load_config()
        self._setup_claude()
        self._setup_example_store()

    def reset_retry_state(self):
        """Reset retry state for a new protocol generation run."""
        self.last_failed_output = None
        self.error_history = []

    def add_error(self, error_msg: str, failed_output: str = None):
        """Record an error from a failed attempt."""
        self.error_history.append(error_msg)
        if failed_output:
            self.last_failed_output = failed_output

    def _load_config(self) -> Dict:
        from .validate_config import validate_config_file

        # Check if config file exists
        if not os.path.exists(self.config_path):
            raise ConfigFileError(self.config_path, "File not found.")

        # Validate the config file
        result = validate_config_file(self.config_path)
        if not result.valid:
            raise ConfigFileError(self.config_path, str(result))

        # Load and normalize
        try:
            with open(self.config_path, 'r') as f:
                raw_config = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigFileError(self.config_path, f"Invalid JSON: {e}")

        return normalize_config(raw_config)

    def _setup_claude(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise APIKeyError("ANTHROPIC_API_KEY")
        self.client = Anthropic(api_key=api_key)

    def _setup_example_store(self):
        """Initialize RAG example store if enabled."""
        self.example_store = None
        if self.use_rag:
            try:
                from .example_store import ExampleStore
                self.example_store = ExampleStore()
            except ImportError:
                print("Warning: RAG dependencies not installed, skipping example retrieval")
            except Exception as e:
                print(f"Warning: Could not initialize example store: {e}")

    def _save_failed_output(self, raw_output: str, error_msg: str):
        """
        Save failed LLM output for debugging.
        Creates a file in the current directory with the raw output.
        """
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_file = f"llm_failed_output_{timestamp}.txt"

        try:
            with open(debug_file, 'w') as f:
                f.write(f"ERROR: {error_msg}\n")
                f.write("=" * 60 + "\n")
                f.write("RAW LLM OUTPUT:\n")
                f.write("=" * 60 + "\n")
                f.write(raw_output)
            print(f"Debug: Raw LLM output saved to {debug_file}")
        except Exception:
            # If we can't save, at least print a snippet
            snippet = raw_output[:500] + "..." if len(raw_output) > 500 else raw_output
            print(f"Debug: Raw LLM output snippet:\n{snippet}")

    def parse_intent(self, prompt: str, csv_path: Optional[str] = None, error_log: Optional[str] = None, failed_json: Optional[str] = None, spec=None) -> Optional[ProtocolSchema]:

        # 1. Prepare Inputs
        print("  [1/5] Preparing inputs...")
        csv_content = ""
        if csv_path:
            try:
                with open(csv_path, 'r') as f:
                    csv_content = f.read()
                print(f"    - Loaded CSV data from {csv_path}")
            except Exception as e:
                print(f"    - Warning: Could not read CSV: {e}")

        print(f"    - Using config: {self.config_path}")

        # 2. Get JSON Schema
        json_schema = get_protocol_json_schema()
        print("    - Protocol schema template loaded")

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

            Flow Control Commands:
            - pause: {{"command_type": "pause", "pipette": "left", "message": "Add reagent manually and resume"}}
            - delay: {{"command_type": "delay", "pipette": "left", "seconds": 30}} or {{"command_type": "delay", "pipette": "left", "minutes": 5}}
            - comment: {{"command_type": "comment", "pipette": "left", "message": "Starting incubation step"}}

            Complex Commands:
            - transfer: {{"command_type": "transfer", "pipette": "left", "source_labware": "...", "source_well": "A1", "dest_labware": "...", "dest_well": "B1", "volume": 100, "new_tip": "always", "mix_before": [3, 50], "mix_after": [3, 50]}}
            - distribute: {{"command_type": "distribute", "pipette": "left", "source_labware": "...", "source_well": "A1", "dest_labware": "...", "dest_wells": ["A1","A2","A3"], "volume": 50, "new_tip": "once"}}
            - consolidate: {{"command_type": "consolidate", "pipette": "left", "source_labware": "...", "source_wells": ["A1","A2","A3"], "dest_labware": "...", "dest_well": "A1", "volume": 50, "new_tip": "once"}}

            Module Commands (for magnetic module, temperature module, etc.):
            - engage_magnets: {{"command_type": "engage_magnets", "pipette": "left", "module": "mag_mod"}}
            - disengage_magnets: {{"command_type": "disengage_magnets", "pipette": "left", "module": "mag_mod"}}
            - set_temperature: {{"command_type": "set_temperature", "pipette": "left", "module": "temp_mod", "celsius": 37}}
            - wait_for_temperature: {{"command_type": "wait_for_temperature", "pipette": "left", "module": "temp_mod"}}
            - deactivate: {{"command_type": "deactivate", "pipette": "left", "module": "temp_mod"}}

            NOTE on mix_before/mix_after: Format is [repetitions, volume_in_ul]. Example: [3, 50] means mix 3 times with 50µL.
            NOTE on flow control: Use pause for manual intervention, delay for incubation times, comment for logging.
            NOTE on modules: If a labware has "on_module" in config, include it in your labware output. Reference modules by their label in module commands.

            PIPETTE VOLUME CONSTRAINTS (CRITICAL):
            - p20_single_gen2 / p20_multi_gen2: 1-20 µL only
            - p300_single_gen2 / p300_multi_gen2: 20-300 µL only
            - p1000_single_gen2: 100-1000 µL only
            - NEVER use a volume outside the pipette's range - this will cause validation failure
            - If a volume doesn't fit the available pipette, adjust the protocol (e.g., multiple transfers)

            EFFICIENCY - KEEP OUTPUT CONCISE:
            - Use "distribute" with dest_wells array instead of many individual "transfer" commands
            - Use "consolidate" with source_wells array instead of many individual "aspirate" commands
            - Example: Instead of 8 transfers to A1,A2,A3...A8, use ONE distribute with dest_wells: ["A1","A2",...,"A8"]
            - This reduces output size and is more efficient

            COMMON MISTAKES TO AVOID:
            - DO NOT reference labware labels that aren't in the config
            - DO NOT use volumes outside the pipette's valid range
            - DO NOT forget to include tipracks in pipette definitions
            - DO NOT use wells outside the labware's valid range (check well_info)
            - DO NOT truncate the JSON - ensure all arrays and objects are complete
            - DO NOT generate excessive individual commands when distribute/consolidate can be used

            PRE-OUTPUT CHECKLIST (verify before responding):
            [ ] All labware labels match dictionary keys from LAB CONFIG
            [ ] All volumes are within the assigned pipette's range
            [ ] All wells exist in the referenced labware (check well_info)
            [ ] JSON is complete with no truncation
            [ ] All tipracks referenced in pipettes are defined in labware

            Now generate a protocol for the user's goal below. Output ONLY valid JSON, no explanations.
        """

        # Enrich config with well information for each labware
        print("  [2/5] Enriching config with well information...")
        enriched_config = enrich_config_with_wells(self.config)

        # Retrieve similar protocol examples to improve code generation
        print("  [3/5] Retrieving similar examples from RAG...")
        rag_examples = ""
        if self.example_store:
            try:
                rag_examples = self.example_store.format_for_prompt(prompt, top_k=3)
                if rag_examples:
                    print("    - Found similar examples for context")
                else:
                    print("    - No similar examples found, proceeding without")
            except Exception:
                print("    - RAG retrieval skipped")
        else:
            print("    - RAG not available, proceeding without examples")

        user_message = f"""
            GOAL: {prompt}

            LAB CONFIG (Physical Setup with Valid Wells):
            {json.dumps(enriched_config, indent=2)}

            IMPORTANT - HOW TO READ THE CONFIG:
            - The dictionary key (e.g., "reservoir", "source_plate", "tips") is the canonical identifier for each labware.
            - 'label': Always equals the dictionary key. Use this value in all labware references and tipracks.
            - 'well_info': Shows valid wells for each labware. Use ONLY wells from the valid range.
            - 'contents': Shows what liquid/sample is in each well (e.g., "A1": "PBS buffer", "A2": "Stock solution").
            - 'on_module': If present, this labware is loaded ONTO the module (e.g., plate on magnetic module). Include this in your labware output.

            RESOLVING REFERENCES IN THE GOAL:
            The user's goal may refer to labware or liquids by:
            1. Dictionary key (e.g., "reservoir", "source_plate") - use this exact key in your output
            2. Contents name (e.g., "stock solution", "PBS", "diluent") - find which labware contains it
            3. Common lab names (e.g., "buffer", "samples", "tips") - map to the appropriate dict key

            Look at the config to identify which labware (by dict key) and well contains the referenced item.
            Example: If goal says "Transfer stock solution" and config shows reservoir["contents"]["A2"] = "Stock solution",
                    then source_labware="reservoir" and source_well="A2".

            CSV DATA (Experiment Parameters):
            {csv_content if csv_content else "None"}

            ERROR LOG (From previous run, if any):
            {error_log if error_log else "None"}

            {rag_examples}
        """

        # Add spec constraint block if reasoning step produced a spec
        if spec is not None:
            spec_section = f"""

            ============================================================
            PROTOCOL SPECIFICATION (from reasoning step — MUST follow)
            ============================================================

            {spec.model_dump_json(indent=2)}

            CRITICAL RULES:
            - Every volume in the spec MUST appear exactly in your output commands
            - Every step in the spec MUST map to one or more commands
            - DO NOT change any volume values from the spec
            - DO NOT add or remove steps beyond what the spec describes
            - The spec tells you WHAT to do; you decide HOW:
              * Which pipette mount to assign (based on volume fitting the pipette range)
              * Which Opentrons labware API name to use (map spec descriptions to config labels)
              * Tip rack assignments
              * Command-level details (new_tip, mix parameters, etc.)
            """
            user_message += spec_section

        # Add fix mode section if we have a failed JSON to correct
        if failed_json:
            # Format error history
            error_history_text = ""
            if self.error_history:
                error_history_text = "\n".join([f"  Attempt {i+1}: {err}" for i, err in enumerate(self.error_history)])

            fix_section = f"""

            ============================================================
            FIX MODE - CORRECT THE PREVIOUS OUTPUT
            ============================================================

            PREVIOUS OUTPUT (with errors):
            {failed_json}

            ERROR HISTORY (all previous attempts):
            {error_history_text if error_history_text else "  No previous errors recorded"}

            FIX INSTRUCTIONS:
            - The JSON above failed validation with the error shown in ERROR LOG
            - Make MINIMAL changes to fix ONLY the specific error(s)
            - Do NOT regenerate from scratch - keep all working parts unchanged
            - Pay attention to the error history to avoid repeating past mistakes
            - Common fixes:
              * Volume outside pipette range: NEVER change the user's specified volume.
                Instead, change the pipette assignment to one that supports the volume.
                The error message will tell you which pipettes can handle the volume.
              * Undefined labware: Use only labels from the LAB CONFIG
              * JSON truncation: Complete any cut-off structures properly

            Output the CORRECTED JSON only.
            """
            user_message += fix_section

        # 4. Call Claude
        print(f"  [4/5] Calling LLM ({self.model_name})...")
        try:
            response = self.client.messages.create(
                model=self.model_name,
                max_tokens=16384,
                system=system_instruction,
                messages=[{"role": "user", "content": user_message}]
            )
            print("    - LLM response received")

            # 5. Parse & Validate
            print("  [5/5] Parsing and validating response...")
            json_str = response.content[0].text

            # Clean up potential markdown code blocks if the model adds them despite JSON mode
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
                print("    - Cleaned markdown formatting from response")
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]
                print("    - Cleaned markdown formatting from response")

            print("    - Parsing JSON...")
            protocol_data = json.loads(json_str)
            print("    - JSON parsed successfully")

            print("    - Validating against ProtocolSchema...")
            # Validate with config context for cross-checking
            result = ProtocolSchema.model_validate(protocol_data, context={'config': self.config})
            print("    - Schema validation passed")
            return result

        except json.JSONDecodeError as e:
            error_msg = f"JSON parse error: {e}"
            print(f"    - Caught JSON issue: {error_msg}")
            # Store for retry and save debug file
            self.last_failed_output = json_str
            self._save_failed_output(json_str, error_msg)
            return None
        except Exception as e:
            error_msg = f"Validation error: {e}"
            print(f"    - Caught schema validation issue: {error_msg}")
            # Store for retry and save debug file (if we have output)
            if 'json_str' in locals():
                self.last_failed_output = json_str
                self._save_failed_output(json_str, error_msg)
            return None
