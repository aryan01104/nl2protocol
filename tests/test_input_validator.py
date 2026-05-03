"""
Contract tests for nl2protocol.validation.input_validator.

`check_input_length` is the pure deterministic pre-check — tested directly
without any client setup. `InputValidator.classify` delegates to it; a small
set of integration tests verify that delegation works (mocked client).
"""

import os
import pytest
from unittest.mock import MagicMock

from nl2protocol.validation.input_validator import (
    InputValidator, InputValidationResult, check_input_length,
)


# ============================================================================
# check_input_length — pure function, no fixtures needed
# ============================================================================

class TestCheckInputLengthTooShort:
    """Post: len(strip(input)) < 3 → INVALID 'Input too short'."""

    @pytest.mark.parametrize("short_input", ["", " ", "  ", "a", "ab", "  a  "])
    def test_short_input_returns_invalid_too_short(self, short_input):
        result = check_input_length(short_input)
        assert result is not None
        assert result.classification == "INVALID"
        assert result.reason == "Input too short"
        assert result.suggestion is not None

    # Boundary: exactly 3 non-whitespace chars returns None (proceed to LLM)
    def test_three_char_input_passes_length_check(self):
        assert check_input_length("abc") is None


class TestCheckInputLengthTooLong:
    """Post: len(input) > 10000 → INVALID 'Input too long'."""

    def test_long_input_returns_invalid_too_long(self):
        result = check_input_length("x" * 10001)
        assert result is not None
        assert result.classification == "INVALID"
        assert result.reason == "Input too long"
        assert result.suggestion is not None

    # Boundary: exactly 10000 chars returns None (proceed to LLM)
    def test_ten_thousand_char_input_passes_length_check(self):
        assert check_input_length("z" * 10000) is None


# ============================================================================
# InputValidator.classify — verifies delegation to check_input_length
# ============================================================================

@pytest.fixture
def validator(monkeypatch):
    """Construct an InputValidator with a stubbed API key + mocked client.

    The mocked client raises on any call, so tests can assert that the
    length-pre-check short-circuited (LLM was NOT invoked).
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-stub-key")
    v = InputValidator()
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = AssertionError("LLM should not be called")
    v.client = mock_client
    return v


class TestClassifyDelegation:
    """classify() should delegate to check_input_length and short-circuit on early return."""

    def test_short_input_does_not_call_llm(self, validator):
        result = validator.classify("hi")
        assert result.reason == "Input too short"
        validator.client.messages.create.assert_not_called()

    def test_long_input_does_not_call_llm(self, validator):
        result = validator.classify("y" * 50000)
        assert result.reason == "Input too long"
        validator.client.messages.create.assert_not_called()

    # Inputs that pass the length pre-check proceed to the LLM call.
    def test_inputs_passing_length_check_attempt_llm_call(self, validator):
        with pytest.raises(RuntimeError):  # mock raises, classify wraps as RuntimeError
            validator.classify("Transfer 100uL from A1 to B1")
        validator.client.messages.create.assert_called_once()


# ============================================================================
# InputValidationResult — dataclass contract
# ============================================================================

class TestInputValidationResult:
    """Contract tests for the InputValidationResult dataclass."""

    # Post: is_valid_protocol is True iff classification == "PROTOCOL"
    @pytest.mark.parametrize("classification,expected", [
        ("PROTOCOL", True),
        ("QUESTION", False),
        ("AMBIGUOUS", False),
        ("INVALID", False),
    ])
    def test_is_valid_protocol(self, classification, expected):
        result = InputValidationResult(classification=classification, reason="r")
        assert result.is_valid_protocol is expected

    # Post: __str__ includes classification and reason
    def test_str_includes_classification_and_reason(self):
        result = InputValidationResult(classification="PROTOCOL", reason="looks good")
        s = str(result)
        assert "PROTOCOL" in s
        assert "looks good" in s

    # Post: __str__ includes suggestion when provided
    def test_str_includes_suggestion_when_present(self):
        result = InputValidationResult(
            classification="INVALID", reason="bad", suggestion="try X"
        )
        assert "try X" in str(result)

    # Post: __str__ omits suggestion line when None
    def test_str_omits_suggestion_when_none(self):
        result = InputValidationResult(classification="PROTOCOL", reason="ok")
        assert "Suggestion" not in str(result)
