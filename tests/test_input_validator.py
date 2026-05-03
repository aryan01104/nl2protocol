"""
Contract tests for nl2protocol.validation.input_validator.

Scope: only the deterministic length-check phase of `InputValidator.classify`.
The LLM-call phase is integration territory and is not exercised here.

Setup: tests stub `ANTHROPIC_API_KEY` so InputValidator() can construct
without raising APIKeyError, then mock `self.client` so any accidental
LLM-call path raises a clear failure.
"""

import os
import pytest
from unittest.mock import MagicMock

from nl2protocol.validation.input_validator import (
    InputValidator, InputValidationResult,
)


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def validator(monkeypatch):
    """Construct an InputValidator with a stubbed API key + mocked client.

    The mocked client raises if any LLM call is attempted, so tests of the
    deterministic length-check path can assert "no LLM call was made".
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-stub-key")
    v = InputValidator()
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = AssertionError(
        "Test reached the LLM path; this test should only exercise the "
        "deterministic length-check path."
    )
    v.client = mock_client
    return v


# ============================================================================
# InputValidator.classify — Phase 1 (deterministic length checks)
# ============================================================================

class TestClassifyTooShort:
    """Phase 1: len(user_input.strip()) < 3 → INVALID 'Input too short'."""

    @pytest.mark.parametrize("short_input", ["", " ", "  ", "a", "ab", "  a  "])
    def test_short_input_returns_invalid_too_short(self, validator, short_input):
        result = validator.classify(short_input)
        assert result.classification == "INVALID"
        assert result.reason == "Input too short"
        assert result.suggestion is not None  # contract: suggestion provided

    def test_short_input_does_not_call_llm(self, validator):
        # The mocked client raises AssertionError on any call. If the LLM
        # path is reached, this test fails with that AssertionError.
        validator.classify("hi")
        validator.client.messages.create.assert_not_called()

    # Boundary: exactly 3 non-whitespace chars passes length checks → LLM is called
    def test_three_char_input_passes_length_check_and_attempts_llm(self, validator):
        # The mock client raises, classify wraps it in RuntimeError; we
        # don't care about the raise — only that the LLM was invoked.
        with pytest.raises(RuntimeError):
            validator.classify("abc")
        validator.client.messages.create.assert_called_once()


class TestClassifyTooLong:
    """Phase 1: len(user_input) > 10000 → INVALID 'Input too long'."""

    def test_long_input_returns_invalid_too_long(self, validator):
        result = validator.classify("x" * 10001)
        assert result.classification == "INVALID"
        assert result.reason == "Input too long"
        assert result.suggestion is not None

    def test_long_input_does_not_call_llm(self, validator):
        validator.classify("y" * 50000)
        validator.client.messages.create.assert_not_called()

    # Boundary: exactly 10000 chars passes the length check → LLM is called
    def test_ten_thousand_char_input_passes_length_check_and_attempts_llm(self, validator):
        with pytest.raises(RuntimeError):
            validator.classify("z" * 10000)
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
