"""
Contract tests for nl2protocol.config.

Covers normalize_config, enrich_config_with_wells, ConfigLoader.__init__,
and ConfigLoader.load_config.
"""

import json
import pytest

from nl2protocol.config import (
    normalize_config, enrich_config_with_wells, ConfigLoader,
)
from nl2protocol.errors import APIKeyError, ConfigFileError


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def labware_config():
    return {
        "labware": {
            "source_plate": {"load_name": "corning_96_wellplate_360ul_flat", "slot": "1"},
            "tiprack": {"load_name": "opentrons_96_tiprack_300ul", "slot": "2"},
        },
        "pipettes": {"left": {"model": "p300_single_gen2", "tipracks": ["tiprack"]}},
    }


@pytest.fixture
def stub_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-stub-key")


# ============================================================================
# normalize_config — contract tests
# ============================================================================

class TestNormalizeConfig:
    """Contract tests for normalize_config."""

    # Post: returns a deep copy (modifying result doesn't affect input)
    def test_returns_deep_copy(self, labware_config):
        original_snapshot = json.dumps(labware_config, sort_keys=True)
        result = normalize_config(labware_config)
        result["labware"]["source_plate"]["slot"] = "11"
        assert json.dumps(labware_config, sort_keys=True) == original_snapshot

    # Post: sets label = key for every labware entry
    def test_sets_label_to_key_for_every_labware(self, labware_config):
        result = normalize_config(labware_config)
        assert result["labware"]["source_plate"]["label"] == "source_plate"
        assert result["labware"]["tiprack"]["label"] == "tiprack"

    # Post: overwrites existing label with the dict key
    def test_overwrites_existing_label(self):
        config = {"labware": {"actual_key": {"load_name": "lw", "label": "wrong_label"}}}
        result = normalize_config(config)
        assert result["labware"]["actual_key"]["label"] == "actual_key"

    # Post: no-op if "labware" key absent
    def test_no_labware_key_returns_deep_copy_unchanged(self):
        config = {"pipettes": {"left": {"model": "p300_single_gen2"}}}
        result = normalize_config(config)
        assert result == config
        assert result is not config  # but still a deep copy

    # Post: empty labware dict is OK
    def test_empty_labware_dict(self):
        result = normalize_config({"labware": {}})
        assert result == {"labware": {}}

    # Post: preserves other top-level keys
    def test_preserves_pipettes_and_other_top_level_keys(self, labware_config):
        result = normalize_config(labware_config)
        assert result["pipettes"] == labware_config["pipettes"]


# ============================================================================
# enrich_config_with_wells — contract tests
# ============================================================================

class TestEnrichConfigWithWells:
    """Contract tests for enrich_config_with_wells."""

    EXPECTED_WELL_INFO_KEYS = {"valid_rows", "valid_columns", "total_wells", "example_wells"}

    # Post: every labware with load_name gets well_info with 4 keys
    def test_adds_well_info_with_four_keys(self, labware_config):
        result = enrich_config_with_wells(labware_config)
        for label in ("source_plate", "tiprack"):
            assert "well_info" in result["labware"][label]
            assert set(result["labware"][label]["well_info"].keys()) == self.EXPECTED_WELL_INFO_KEYS

    # Post: example_wells is truncated when total_wells > 7 (5 + "..." + 2)
    def test_example_wells_truncated_when_more_than_7(self, labware_config):
        result = enrich_config_with_wells(labware_config)
        info = result["labware"]["source_plate"]["well_info"]
        assert info["total_wells"] == 96
        assert len(info["example_wells"]) == 8  # 5 + 1 ("...") + 2
        assert "..." in info["example_wells"]

    # Post: example_wells is the full list when total_wells <= 7
    def test_example_wells_full_when_at_most_7(self):
        config = {"labware": {"reservoir": {"load_name": "custom_1_reservoir", "slot": "3"}}}
        result = enrich_config_with_wells(config)
        info = result["labware"]["reservoir"]["well_info"]
        assert info["total_wells"] == 1
        assert "..." not in info["example_wells"]
        assert len(info["example_wells"]) == 1

    # Post: labware without load_name is left without well_info
    def test_no_load_name_no_well_info(self):
        config = {"labware": {"weird_thing": {"slot": "5"}}}  # no load_name
        result = enrich_config_with_wells(config)
        assert "well_info" not in result["labware"]["weird_thing"]

    # Post: still applies normalize_config (label = key)
    def test_also_applies_normalize_config(self, labware_config):
        result = enrich_config_with_wells(labware_config)
        assert result["labware"]["source_plate"]["label"] == "source_plate"

    # Post: does not mutate input
    def test_does_not_mutate_input(self, labware_config):
        snapshot = json.dumps(labware_config, sort_keys=True)
        enrich_config_with_wells(labware_config)
        assert json.dumps(labware_config, sort_keys=True) == snapshot


# ============================================================================
# ConfigLoader.__init__ — contract tests
# ============================================================================

class TestConfigLoaderInit:
    """Contract tests for ConfigLoader.__init__."""

    # Post: with valid env, fields are set correctly
    def test_constructs_with_default_model(self, stub_api_key):
        loader = ConfigLoader(config_path="some_path.json")
        assert loader.config_path == "some_path.json"
        assert loader.model_name == "claude-sonnet-4-20250514"  # default
        assert loader.config is None
        assert loader.client is not None

    # Post: model_name override
    def test_constructs_with_custom_model(self, stub_api_key):
        loader = ConfigLoader(model_name="claude-opus-4-20250514")
        assert loader.model_name == "claude-opus-4-20250514"

    # Raises: APIKeyError if ANTHROPIC_API_KEY missing
    def test_raises_api_key_error_when_env_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Also block .env discovery from picking up a real key by chdir-ing
        # to an empty temp dir (no .env present).
        monkeypatch.chdir(tmp_path)
        with pytest.raises(APIKeyError):
            ConfigLoader()


# ============================================================================
# ConfigLoader.load_config — contract tests
# ============================================================================

class TestConfigLoaderLoadConfig:
    """Contract tests for ConfigLoader.load_config."""

    # Raises: ConfigFileError if file does not exist
    def test_raises_when_file_does_not_exist(self, stub_api_key, tmp_path):
        loader = ConfigLoader(config_path=str(tmp_path / "nope.json"))
        with pytest.raises(ConfigFileError) as exc_info:
            loader.load_config()
        assert "not found" in str(exc_info.value).lower()

    # Raises: ConfigFileError on invalid JSON
    def test_raises_on_invalid_json(self, stub_api_key, tmp_path, monkeypatch):
        # Bypass validate_config_file by stubbing it to always return valid
        from nl2protocol.validation import validate_config as vc
        from dataclasses import dataclass

        @dataclass
        class _Stub:
            valid: bool = True
            errors: list = None
            def __str__(self): return ""

        monkeypatch.setattr(vc, "validate_config_file", lambda p: _Stub(valid=True, errors=[]))

        path = tmp_path / "bad.json"
        path.write_text("{not valid json")
        loader = ConfigLoader(config_path=str(path))

        with pytest.raises(ConfigFileError) as exc_info:
            loader.load_config()
        assert "invalid json" in str(exc_info.value).lower()

    # Post: returns normalized dict on success; sets self.config
    def test_returns_normalized_config_on_success(self, stub_api_key):
        loader = ConfigLoader(
            config_path="test_cases/examples/simple_transfer/config.json"
        )
        result = loader.load_config()
        assert isinstance(result, dict)
        assert result is loader.config  # self.config set
        # normalize_config applied: every labware has label = key
        if "labware" in result:
            for key, lw in result["labware"].items():
                assert lw["label"] == key

    # Raises: ConfigFileError when validation fails
    def test_raises_on_validation_failure(self, stub_api_key, tmp_path, monkeypatch):
        from nl2protocol.validation import validate_config as vc
        from dataclasses import dataclass

        @dataclass
        class _Stub:
            valid: bool = False
            errors: list = None
            def __str__(self): return "synthetic validation failure"

        monkeypatch.setattr(vc, "validate_config_file", lambda p: _Stub(valid=False, errors=[]))

        path = tmp_path / "valid_json_bad_schema.json"
        path.write_text('{"some": "json"}')
        loader = ConfigLoader(config_path=str(path))

        with pytest.raises(ConfigFileError) as exc_info:
            loader.load_config()
        assert "synthetic validation failure" in str(exc_info.value)
