"""Tests for nl2protocol/citing.py — shared citation predicates.

Covers `cite_covers_well`, the range-aware "is this well grounded by
this cite" predicate consumed by the verifier in extractor.py.
"""

from nl2protocol.citing import cite_covers_well
from nl2protocol.extraction.extractor import SemanticExtractor


# ============================================================================
# Alphanumeric ranges: 'B1-B4', 'A1-D1', 'A1-D4', '(B1-B4)', 'D4-A1'
# ============================================================================


class TestCiteCoversWell_AlphanumericRanges:
    """Wells matched against a hyphenated alphanumeric range in the cite."""

    def test_single_row_interior(self):
        # 'B1-B4' covers B2, B3 (interior).
        assert cite_covers_well("(B1-B4)", "B2") is True
        assert cite_covers_well("(B1-B4)", "B3") is True

    def test_single_row_endpoints(self):
        # Endpoints are inclusive on both sides.
        assert cite_covers_well("(B1-B4)", "B1") is True
        assert cite_covers_well("(B1-B4)", "B4") is True

    def test_single_row_outside_column(self):
        # Column out of [1, 4].
        assert cite_covers_well("(B1-B4)", "B5") is False

    def test_single_row_wrong_row(self):
        # Column in range but row mismatched.
        assert cite_covers_well("(B1-B4)", "A2") is False
        assert cite_covers_well("(B1-B4)", "C2") is False

    def test_single_column_range(self):
        # 'A1-D1' covers A1, B1, C1, D1.
        assert cite_covers_well("A1-D1", "B1") is True
        assert cite_covers_well("A1-D1", "C1") is True

    def test_single_column_range_outside_row(self):
        assert cite_covers_well("A1-D1", "E1") is False

    def test_single_column_range_wrong_column(self):
        # Row in range but column mismatched.
        assert cite_covers_well("A1-D1", "B2") is False

    def test_rectangle(self):
        # 'A1-D4' covers the 4x4 grid.
        assert cite_covers_well("A1-D4", "A1") is True
        assert cite_covers_well("A1-D4", "D4") is True
        assert cite_covers_well("A1-D4", "B3") is True

    def test_rectangle_outside(self):
        assert cite_covers_well("A1-D4", "E5") is False
        assert cite_covers_well("A1-D4", "A5") is False
        assert cite_covers_well("A1-D4", "E1") is False

    def test_reversed_endpoints(self):
        # 'D4-A1' designates the same rectangle as 'A1-D4'.
        assert cite_covers_well("D4-A1", "B3") is True
        assert cite_covers_well("D4-A1", "E5") is False

    def test_en_dash(self):
        # en-dash variant (U+2013) as the delimiter.
        assert cite_covers_well("B1\u2013B4", "B2") is True

    def test_em_dash(self):
        # em-dash variant (U+2014) as the delimiter.
        assert cite_covers_well("B1\u2014B4", "B2") is True

    def test_case_insensitive_well(self):
        # Lowercase well name should still match.
        assert cite_covers_well("(B1-B4)", "b2") is True

    def test_case_insensitive_cite(self):
        # Lowercase cite should also work.
        assert cite_covers_well("(b1-b4)", "B2") is True

    def test_double_digit_columns(self):
        # 96-well plates have columns up to 12.
        assert cite_covers_well("A1-A12", "A10") is True
        assert cite_covers_well("A1-A12", "A12") is True
        assert cite_covers_well("A1-A12", "A13") is False


# ============================================================================
# Natural language: 'column N', 'columns N-M'
# ============================================================================


class TestCiteCoversWell_ColumnExpressions:
    """Wells matched against column-based natural-language ranges."""

    def test_single_column(self):
        # 'column 1' covers any well in column 1.
        assert cite_covers_well("column 1", "A1") is True
        assert cite_covers_well("column 1", "H1") is True

    def test_single_column_wrong_column(self):
        assert cite_covers_well("column 1", "A2") is False

    def test_column_range_plural(self):
        # 'columns 2-8' covers columns 2 through 8.
        assert cite_covers_well("columns 2-8", "A2") is True
        assert cite_covers_well("columns 2-8", "H8") is True

    def test_column_range_outside(self):
        assert cite_covers_well("columns 2-8", "A1") is False
        assert cite_covers_well("columns 2-8", "A9") is False

    def test_column_range_singular(self):
        # Same shape without the trailing 's'.
        assert cite_covers_well("column 2-8", "B5") is True

    def test_column_double_digit(self):
        # Column numbers > 9 should parse correctly.
        assert cite_covers_well("column 12", "H12") is True
        assert cite_covers_well("columns 10-12", "A11") is True


# ============================================================================
# Natural language: 'row X', 'rows X-Y'
# ============================================================================


class TestCiteCoversWell_RowExpressions:
    """Wells matched against row-based natural-language ranges."""

    def test_single_row(self):
        assert cite_covers_well("row B", "B5") is True
        assert cite_covers_well("row B", "B12") is True

    def test_single_row_wrong_row(self):
        assert cite_covers_well("row B", "A5") is False

    def test_row_range_plural(self):
        assert cite_covers_well("rows A-D", "A1") is True
        assert cite_covers_well("rows A-D", "D12") is True
        assert cite_covers_well("rows A-D", "B6") is True

    def test_row_range_outside(self):
        assert cite_covers_well("rows A-D", "E1") is False

    def test_row_range_singular(self):
        # 'row A-D' (no plural) also accepted.
        assert cite_covers_well("row A-D", "B5") is True


# ============================================================================
# Edge cases: invalid input, empty cite, non-range cite
# ============================================================================


class TestCiteCoversWell_EdgeCases:
    """Boundary inputs and cites with no range expression."""

    def test_invalid_well_name(self):
        # 'Z9' doesn't match [A-P]\d+.
        assert cite_covers_well("(B1-B4)", "Z9") is False

    def test_non_well_string(self):
        assert cite_covers_well("(B1-B4)", "not-a-well") is False

    def test_empty_well(self):
        assert cite_covers_well("(B1-B4)", "") is False

    def test_empty_cite(self):
        assert cite_covers_well("", "B2") is False

    def test_cite_with_no_range_expression(self):
        # Cite is just a literal well or unrelated text.
        assert cite_covers_well("cells B2", "B2") is False
        assert cite_covers_well("Add 2uL", "B2") is False

    def test_cite_with_literal_well_only(self):
        # A literal cite like 'B2' isn't a range — caller should handle
        # this via the substring path, not via this predicate.
        assert cite_covers_well("B2", "B2") is False


# ============================================================================
# Integration: _value_in_quote wires the range relaxation through
# ============================================================================


class TestValueInQuote_RangeRelaxation:
    """Confirms the verifier's `_value_in_quote` consults `cite_covers_well`
    after the literal-substring path. Regression-guards the existing
    behavior on non-well values."""

    def test_well_in_range_cite_passes(self):
        # The bacterial-transformation case: 'B2' against '(B1-B4)'
        # used to fire a fabrication warning; now it passes.
        assert SemanticExtractor._value_in_quote("B2", "(B1-B4)") is True
        assert SemanticExtractor._value_in_quote("B3", "(B1-B4)") is True

    def test_well_outside_range_cite_fails(self):
        # The check still has teeth: a well outside the cited range
        # is still flagged.
        assert SemanticExtractor._value_in_quote("B5", "(B1-B4)") is False
        assert SemanticExtractor._value_in_quote("E5", "A1-D4") is False

    def test_literal_substring_unchanged(self):
        # Existing substring path: 'B2' literally inside 'cells B2'.
        assert SemanticExtractor._value_in_quote("B2", "cells B2") is True

    def test_numeric_unchanged(self):
        # Numeric path untouched by the range relaxation.
        assert SemanticExtractor._value_in_quote(100, "100uL") is True
        assert SemanticExtractor._value_in_quote(100.0, "100uL") is True
        assert SemanticExtractor._value_in_quote(50, "100uL") is False

    def test_string_non_well_unchanged(self):
        # Non-well strings still use substring semantics only.
        assert SemanticExtractor._value_in_quote("plasmid DNA",
                                                  "plasmid DNA samples") is True
        assert SemanticExtractor._value_in_quote("buffer", "plasmid DNA") is False

    def test_column_cite_covers_well(self):
        # Natural-language column expression in the cite.
        assert SemanticExtractor._value_in_quote("A1", "column 1") is True
        assert SemanticExtractor._value_in_quote("H1", "column 1") is True
        assert SemanticExtractor._value_in_quote("A2", "column 1") is False
