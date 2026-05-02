"""
config.py — Lab config loading, validation, and enrichment.

ConfigLoader reads a lab_config.json, validates it, normalizes labels,
and initializes the Anthropic client for the pipeline.
"""

import json
import os
from typing import Dict, Any

from anthropic import Anthropic
from dotenv import find_dotenv, load_dotenv

from .errors import APIKeyError, ConfigFileError
from .models.labware import get_well_info


def normalize_config(config: Dict) -> Dict:
    """
    Normalize config so that label = dict_key for all labware.
    This ensures consistent referencing throughout the system.
    """
    normalized = json.loads(json.dumps(config))  # Deep copy

    if "labware" in normalized:
        for key, labware in normalized["labware"].items():
            labware["label"] = key

    return normalized


def enrich_config_with_wells(config: Dict) -> Dict:
    """Add well information to each labware in the config."""
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


class ConfigLoader:
    """
    Config loader and Claude client initializer for the protocol pipeline.

    Takes: config file path, model name
    Provides: validated config, Anthropic client, model name
    """

    def __init__(self, config_path: str = "lab_config.json", model_name: str = "claude-sonnet-4-20250514"):
        load_dotenv(find_dotenv(usecwd=True))
        self.model_name = model_name
        self.config_path = config_path
        self.config = None
        self._setup_claude()

    def load_config(self) -> Dict:
        from .validation.validate_config import validate_config_file

        if not os.path.exists(self.config_path):
            raise ConfigFileError(self.config_path, "File not found.")

        result = validate_config_file(self.config_path)
        if not result.valid:
            raise ConfigFileError(self.config_path, str(result))

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

