"""
Contract tests for nl2protocol.validation.validate_config.

Covers ConfigValidator.validate (the orchestrator), the two convenience
functions, and ValidationResult.__str__. Private _validate_* helpers are
tested transitively through `validate`.
"""

import json
import pytest

from nl2protocol.validation.validate_config import (
    ConfigValidator, ValidationError, ValidationResult,
    validate_config, validate_config_file,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def valid_config():
    """A minimally-valid config that should pass every check."""
    return {
        "labware": {
            "tiprack": {"load_name": "opentrons_96_tiprack_300ul", "slot": "1"},
            "source_plate": {"load_name": "corning_96_wellplate_360ul_flat", "slot": "2"},
        },
        "pipettes": {
            "left": {"model": "p300_single_gen2", "tipracks": ["tiprack"]},
        },
    }


# ============================================================================
# ConfigValidator.validate — orchestration contract
# ============================================================================

class TestValidate:
    """Contract tests for ConfigValidator.validate."""

    # Post: valid config → valid=True, no errors
    def test_valid_config_returns_valid_true(self, valid_config):
        result = ConfigValidator().validate(valid_config)
        assert result.valid is True
        assert result.errors == []

    # Post: missing 'labware' → schema error short-circuits secondary checks
    def test_missing_labware_short_circuits(self):
        config = {"pipettes": {"left": {"model": "p300_single_gen2"}}}
        result = ConfigValidator().validate(config)
        assert result.valid is False
        # Schema error present
        assert any(e.category == "schema" and "Missing required field 'labware'" in e.message
                   for e in result.errors)
        # Short-circuit: secondary checks did not produce labware/slot/etc.
        assert all(e.category == "schema" for e in result.errors)

    # Post: missing 'pipettes' → schema error short-circuits
    def test_missing_pipettes_short_circuits(self):
        config = {"labware": {}}
        result = ConfigValidator().validate(config)
        assert result.valid is False
        assert any("Missing required field 'pipettes'" in e.message for e in result.errors)

    # Post: when 'labware' or 'pipettes' is the wrong type (e.g. a string),
    # the function returns a structured ValidationResult(valid=False) with the
    # schema error — it does NOT crash. The short-circuit guard skips secondary
    # checks (which would otherwise hit `.items()` on the string and crash).
    def test_labware_wrong_type_returns_structured_error_no_crash(self):
        config = {"labware": "not_a_dict", "pipettes": {"left": {"model": "p300_single_gen2"}}}
        result = ConfigValidator().validate(config)  # must not raise
        assert result.valid is False
        assert any(e.category == "schema" and "must be an object" in e.message
                   for e in result.errors)

    def test_pipettes_wrong_type_returns_structured_error_no_crash(self):
        config = {"labware": {}, "pipettes": "not_a_dict"}
        result = ConfigValidator().validate(config)  # must not raise
        assert result.valid is False
        assert any(e.category == "schema" and "must be an object" in e.message
                   for e in result.errors)

    # Post: labware entry missing load_name → schema error
    def test_labware_missing_load_name(self):
        config = {
            "labware": {"plate": {"slot": "1"}},  # no load_name
            "pipettes": {"left": {"model": "p300_single_gen2"}},
        }
        result = ConfigValidator().validate(config)
        assert any(e.category == "schema" and "missing 'load_name'" in e.message
                   for e in result.errors)

    # Post: labware entry missing slot → schema error
    def test_labware_missing_slot(self):
        config = {
            "labware": {"plate": {"load_name": "corning_96_wellplate_360ul_flat"}},
            "pipettes": {"left": {"model": "p300_single_gen2"}},
        }
        result = ConfigValidator().validate(config)
        assert any(e.category == "schema" and "missing 'slot'" in e.message
                   for e in result.errors)

    # Post: pipette entry missing model → schema error
    def test_pipette_missing_model(self):
        config = {
            "labware": {"plate": {"load_name": "corning_96_wellplate_360ul_flat", "slot": "1"}},
            "pipettes": {"left": {}},  # no model
        }
        result = ConfigValidator().validate(config)
        assert any(e.category == "schema" and "missing 'model'" in e.message
                   for e in result.errors)

    # Post: slot conflict → category="slot" error
    def test_slot_conflict(self):
        config = {
            "labware": {
                "plate1": {"load_name": "corning_96_wellplate_360ul_flat", "slot": "1"},
                "plate2": {"load_name": "corning_96_wellplate_360ul_flat", "slot": "1"},
            },
            "pipettes": {"left": {"model": "p300_single_gen2"}},
        }
        result = ConfigValidator().validate(config)
        slot_errors = [e for e in result.errors if e.category == "slot"]
        assert len(slot_errors) == 1
        assert "plate1" in slot_errors[0].message
        assert "plate2" in slot_errors[0].message

    # Post: invalid mount → category="mount" error
    def test_invalid_mount(self):
        config = {
            "labware": {"plate": {"load_name": "corning_96_wellplate_360ul_flat", "slot": "1"}},
            "pipettes": {"middle": {"model": "p300_single_gen2"}},
        }
        result = ConfigValidator().validate(config)
        mount_errors = [e for e in result.errors if e.category == "mount"]
        assert len(mount_errors) == 1
        assert "middle" in mount_errors[0].message

    # Post: tiprack ref to non-existent labware → category="tiprack" error
    def test_tiprack_ref_missing(self, valid_config):
        valid_config["pipettes"]["left"]["tipracks"] = ["nonexistent_tiprack"]
        result = ConfigValidator().validate(valid_config)
        tiprack_errors = [e for e in result.errors if e.category == "tiprack"]
        assert len(tiprack_errors) == 1
        assert "nonexistent_tiprack" in tiprack_errors[0].message

    # Post: invalid load_name → category="labware" error (only when shared data available)
    def test_invalid_load_name(self):
        validator = ConfigValidator()
        if validator._shared_data_unavailable:
            pytest.skip("opentrons_shared_data unavailable; load_name validation is skipped")

        config = {
            "labware": {"plate": {"load_name": "totally_made_up_loadname", "slot": "1"}},
            "pipettes": {"left": {"model": "p300_single_gen2"}},
        }
        result = validator.validate(config)
        labware_errors = [e for e in result.errors if e.category == "labware"]
        assert any("totally_made_up_loadname" in e.message for e in labware_errors)

    # Post: invalid slot value → category="labware" error
    def test_invalid_slot_value(self):
        config = {
            "labware": {"plate": {"load_name": "corning_96_wellplate_360ul_flat", "slot": "99"}},
            "pipettes": {"left": {"model": "p300_single_gen2"}},
        }
        result = ConfigValidator().validate(config)
        slot_errors = [e for e in result.errors if e.category == "labware" and "Invalid slot" in e.message]
        assert len(slot_errors) == 1


# ============================================================================
# ValidationResult.__str__ — contract
# ============================================================================

class TestValidationResultStr:
    """Contract tests for ValidationResult.__str__."""

    # Post: valid=True returns single line
    def test_valid_returns_single_line(self):
        result = ValidationResult(valid=True)
        assert str(result) == "Config is valid"

    # Post: valid=False returns multi-line with header + indented errors
    def test_invalid_returns_multiline_with_header(self):
        result = ValidationResult(
            valid=False,
            errors=[
                ValidationError(category="schema", message="missing X"),
                ValidationError(category="slot", message="conflict on Y"),
            ],
        )
        s = str(result)
        assert s.startswith("Config invalid (2 errors):")
        assert "  - [schema] missing X" in s
        assert "  - [slot] conflict on Y" in s

    # Post: warnings are not included in __str__
    def test_warnings_not_in_str(self):
        result = ValidationResult(
            valid=True,
            warnings=[ValidationError(category="labware", message="some warning")],
        )
        assert "some warning" not in str(result)


# ============================================================================
# validate_config (convenience) — contract
# ============================================================================

class TestValidateConfigConvenience:
    """Contract tests for the validate_config convenience function."""

    def test_delegates_to_ConfigValidator_validate(self, valid_config):
        result = validate_config(valid_config)
        assert isinstance(result, ValidationResult)
        assert result.valid is True


# ============================================================================
# validate_config_file — contract
# ============================================================================

class TestValidateConfigFile:
    """Contract tests for validate_config_file."""

    # Post: file not found → ValidationResult(valid=False) with category="file"
    def test_file_not_found_returns_invalid_with_file_error(self, tmp_path):
        result = validate_config_file(str(tmp_path / "nope.json"))
        assert result.valid is False
        assert any(e.category == "file" and "not found" in e.message.lower()
                   for e in result.errors)

    # Post: invalid JSON → ValidationResult(valid=False) with category="file"
    def test_invalid_json_returns_invalid_with_file_error(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not valid json")
        result = validate_config_file(str(path))
        assert result.valid is False
        assert any(e.category == "file" and "Invalid JSON" in e.message
                   for e in result.errors)

    # Post: valid file → delegates to validate_config
    def test_valid_file_returns_valid(self, valid_config, tmp_path):
        path = tmp_path / "good.json"
        path.write_text(json.dumps(valid_config))
        result = validate_config_file(str(path))
        assert result.valid is True
