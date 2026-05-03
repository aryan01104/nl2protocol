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
    """Return a deep copy of `config` with every labware entry's `label` set to its key.

    Pre:    `config` is a JSON-serializable dict (round-trips through
            `json.dumps`/`json.loads`). The optional "labware" key, if
            present, maps str → dict.

    Post:   Returns a NEW dict (deep copy). If the copy has a "labware" key,
            every value `copy["labware"][k]` gets a "label" field set to
            exactly `k`, overwriting any previously-set "label". All other
            top-level keys are preserved verbatim.

    Side effects: None. Does not mutate the input `config` (deep copy via
                  json round-trip).

    Raises: TypeError if `config` contains values that are not JSON-
            serializable (raised from `json.dumps`).
    """
    normalized = json.loads(json.dumps(config))  # Deep copy

    if "labware" in normalized:
        for key, labware in normalized["labware"].items():
            labware["label"] = key

    return normalized


def enrich_config_with_wells(config: Dict) -> Dict:
    """Return a normalized deep-copy of `config` with `well_info` added per labware.

    Pre:    `config` is a JSON-serializable dict. Each labware entry that
            has a `load_name` field carries a string accepted by
            `get_well_info` (Opentrons load-name or heuristic-recognized
            string).

    Post:   Returns `normalize_config(config)` with an additional `well_info`
            sub-dict added to every labware entry that has a `load_name`.
            The `well_info` sub-dict has exactly four keys:
              * `valid_rows`     (str): e.g. "A-H" or "A".
              * `valid_columns`  (str): e.g. "1-12".
              * `total_wells`    (int): well count.
              * `example_wells`  (List[str]): first-5 wells + "..." +
                last-2 wells if well_count > 7; full list otherwise.
            Labware entries without a `load_name` are left without a
            `well_info` field.

    Side effects: None on `config` (deep copy). Inherits the side effects
                  of `get_well_info` (lazy Opentrons import; logging on
                  fallback).

    Raises: TypeError if `config` is not JSON-serializable.
            AttributeError if any `load_name` is not a string.
    """
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
        """Construct a ConfigLoader with a config path and a Claude model name.

        Pre:    `config_path` is a string (file path; not required to exist
                yet — existence is checked in `load_config`). `model_name`
                is an Anthropic model identifier string.

        Post:   Sets `self.config_path`, `self.model_name`. Initializes
                `self.config` to None (set later by `load_config`).
                Initializes `self.client` to a constructed `Anthropic`
                instance using `ANTHROPIC_API_KEY` from environment.

        Side effects: Loads environment variables from a `.env` file via
                      `load_dotenv(find_dotenv(usecwd=True))` — searches
                      from the current working directory upward.

        Raises: APIKeyError if `ANTHROPIC_API_KEY` is not set in the
                environment after `.env` is loaded.
        """
        load_dotenv(find_dotenv(usecwd=True))
        self.model_name = model_name
        self.config_path = config_path
        self.config = None
        self._setup_claude()

    def load_config(self) -> Dict:
        """Read, validate, parse, and normalize the config file at `self.config_path`.

        Pre:    `self.config_path` was set in `__init__`.

        Post:   On success: `self.config` is set to a normalized dict (with
                `label = key` for every labware entry, per `normalize_config`).
                Returns `self.config`.
                On failure: `self.config` is left at its previous value
                (None on first call), and a ConfigFileError is raised.

        Side effects: Reads `self.config_path` from disk. Mutates
                      `self.config`. Imports `validate_config_file` lazily.

        Raises: ConfigFileError if any of:
                  * the file does not exist,
                  * `validate_config_file` reports the file invalid,
                  * the file contains invalid JSON.
                The error's reason field carries the specific failure cause.
        """
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

