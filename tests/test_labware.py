"""
Contract tests for nl2protocol.models.labware.get_well_info.

Each test method targets one clause of the docstring contract.
Tests use load_names like "custom_96_..." that Opentrons does NOT recognize
to deterministically exercise the heuristic-fallback path.
"""

import pytest

from nl2protocol.models.labware import get_well_info


# ============================================================================
# get_well_info — contract tests
# ============================================================================

class TestGetWellInfoSchema:
    """Post: returns dict with exactly six expected keys."""

    EXPECTED_KEYS = {"rows", "cols", "row_range", "col_range", "well_count", "valid_wells"}

    def test_returns_dict_with_six_keys(self):
        info = get_well_info("custom_96_my_plate")
        assert set(info.keys()) == self.EXPECTED_KEYS

    def test_well_count_matches_valid_wells_length(self):
        info = get_well_info("custom_96_my_plate")
        assert info["well_count"] == len(info["valid_wells"])

    def test_well_count_equals_rows_times_cols(self):
        info = get_well_info("custom_96_my_plate")
        assert info["well_count"] == info["rows"] * info["cols"]


class TestGetWellInfoOpentronsPath:
    """Post: Opentrons-known load_names match Opentrons' definition exactly."""

    def test_corning_96_wellplate_returns_8x12(self):
        info = get_well_info("corning_96_wellplate_360ul_flat")
        assert info["rows"] == 8
        assert info["cols"] == 12
        assert info["well_count"] == 96
        assert "A1" in info["valid_wells"]
        assert "H12" in info["valid_wells"]


class TestGetWellInfoHeuristicDispatch:
    """Post: heuristic dispatch table — substring patterns map to grid sizes.

    Uses load_names with "custom_" prefix so Opentrons doesn't recognize them
    and the heuristic path is exercised.
    """

    @pytest.mark.parametrize("load_name,expected_rows,expected_cols", [
        ("custom_384_my_plate",          16, 24),
        ("custom_96_my_plate",            8, 12),
        ("custom_48_my_plate",            6,  8),
        ("custom_24_my_tube_rack",        4,  6),
        ("custom_12_reservoir_my_label",  1, 12),
        ("custom_12_other_plate",         3,  4),
        ("custom_6_my_plate",             2,  3),
        ("custom_1_reservoir",            1,  1),
        ("custom_1_well_other",           1,  1),
    ])
    def test_heuristic_pattern_dispatch(self, load_name, expected_rows, expected_cols):
        info = get_well_info(load_name)
        assert info["rows"] == expected_rows
        assert info["cols"] == expected_cols
        assert info["well_count"] == expected_rows * expected_cols


class TestGetWellInfoWarts:
    """KNOWN WARTS — pin-tests for current silent-fallback behavior."""

    # Wart 1: unrecognized load_name → default 8x12 (silent lie)
    def test_unrecognized_loadname_defaults_to_96well(self):
        info = get_well_info("totally_unknown_garbage_string")
        assert info["rows"] == 8
        assert info["cols"] == 12
        assert info["well_count"] == 96

    # Wart 2: empty string → default 8x12 (silent lie)
    def test_empty_string_defaults_to_96well(self):
        info = get_well_info("")
        assert info["rows"] == 8
        assert info["cols"] == 12
        assert info["well_count"] == 96

    # Both warts: caller cannot distinguish "real plate" from "garbage in"
    def test_garbage_and_real_96well_indistinguishable(self):
        garbage = get_well_info("typo_in_loadname")
        real = get_well_info("custom_96_my_plate")
        assert garbage["rows"] == real["rows"]
        assert garbage["cols"] == real["cols"]
        assert garbage["well_count"] == real["well_count"]


class TestGetWellInfoRaises:
    """Raises: AttributeError if load_name is not a string."""

    def test_raises_attribute_error_on_none(self):
        with pytest.raises(AttributeError):
            get_well_info(None)
