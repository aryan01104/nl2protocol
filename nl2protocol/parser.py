import json
import os
from typing import Dict, Any
from anthropic import Anthropic
from .errors import APIKeyError, ConfigFileError
from dotenv import load_dotenv

"""
parser.py

Config loading, validation, enrichment, and Claude client setup.
Used by ProtocolAgent to initialize the pipeline.
"""


########## Config enrichment: normalize labware labels and keys, add well info ##########


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


class ProtocolParser:
    """
    Config loader and Claude client initializer for the protocol pipeline.

    Takes: config file path, model name
    Provides: validated config, Anthropic client, model name
    """

    def __init__(self, config_path: str = "lab_config.json", model_name: str = "claude-sonnet-4-20250514"):
        load_dotenv()
        self.model_name = model_name
        self.config_path = config_path
        self.config = self._load_config()
        self._setup_claude()

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
