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
    Get well layout for a labware load_name.

    Primary: uses Opentrons' own labware definitions (authoritative source).
    Fallback: heuristic pattern matching on the load_name string.

    Returns: dict with rows, cols, row_range, col_range, well_count, valid_wells.
    """
    # Try the Opentrons labware library first — this is the real data
    try:
        from opentrons.protocols.labware import get_labware_definition
        defn = get_labware_definition(load_name)
        wells = sorted(defn["wells"].keys(), key=lambda w: (int(w[1:]), w[0]))

        # Derive grid dimensions from actual wells
        row_set = sorted(set(w[0] for w in wells))
        col_set = sorted(set(int(w[1:]) for w in wells))
        rows = len(row_set)
        cols = len(col_set)

        return {
            "rows": rows,
            "cols": cols,
            "row_range": f"{row_set[0]}-{row_set[-1]}" if rows > 1 else row_set[0],
            "col_range": f"{col_set[0]}-{col_set[-1]}",
            "well_count": len(wells),
            "valid_wells": wells
        }
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            f"Could not load Opentrons definition for '{load_name}' — using heuristic well layout"
        )

    # Fallback: heuristic pattern matching for unknown/custom labware
    ln = load_name.lower()
    if "_384_" in ln:
        rows, cols = 16, 24
    elif "_96_" in ln:
        rows, cols = 8, 12
    elif "_48_" in ln:
        rows, cols = 6, 8
    elif "_24_" in ln:
        rows, cols = 4, 6
    elif "_12_reservoir" in ln or "_12_well_reservoir" in ln:
        rows, cols = 1, 12
    elif "_12_" in ln:
        rows, cols = 3, 4
    elif "_6_" in ln:
        rows, cols = 2, 3
    elif "_1_reservoir" in ln or "_1_well" in ln:
        rows, cols = 1, 1
    else:
        rows, cols = 8, 12

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
        self.config = None
        self._setup_claude()

    def load_config(self) -> Dict:
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

        self.config = normalize_config(raw_config)
        return self.config

    def _setup_claude(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise APIKeyError("ANTHROPIC_API_KEY")
        self.client = Anthropic(api_key=api_key)
