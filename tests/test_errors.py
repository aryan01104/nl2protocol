"""
Contract tests for nl2protocol.errors formatter functions.

Scope: the two pure formatters — `format_error_for_cli` and `format_api_error`.
Exception classes' __init__ message construction is exercised incidentally.
"""

import pytest
import anthropic

from nl2protocol.errors import (
    format_error_for_cli,
    format_api_error,
    NL2ProtocolError,
    ConfigurationError,
    APIKeyError,
    ValidationError,
    InputValidationError,
    EquipmentError,
    LabwareNotFoundError,
)


# ============================================================================
# Helpers — fake anthropic exceptions that pass isinstance dispatch but
# skip the real __init__ (which requires an httpx.Response object).
# ============================================================================

class _FakeAuthError(anthropic.AuthenticationError):
    def __init__(self, message=""):
        Exception.__init__(self, message)


class _FakeRateLimitError(anthropic.RateLimitError):
    def __init__(self, message=""):
        Exception.__init__(self, message)


class _FakeStatusError(anthropic.APIStatusError):
    def __init__(self, message="", status_code=400):
        Exception.__init__(self, message)
        self.status_code = status_code


class _FakeTimeoutError(anthropic.APITimeoutError):
    def __init__(self, message=""):
        Exception.__init__(self, message)


class _FakeConnectionError(anthropic.APIConnectionError):
    def __init__(self, message=""):
        Exception.__init__(self, message)


# ============================================================================
# format_error_for_cli — contract tests
# ============================================================================

class TestFormatErrorForCli:
    """Contract tests for format_error_for_cli."""

    # Post: NL2ProtocolError → "Error: " prefix
    def test_nl2protocol_error_gets_error_prefix(self):
        err = NL2ProtocolError("something broke")
        assert format_error_for_cli(err).startswith("Error: ")
        assert "something broke" in format_error_for_cli(err)

    # Post: NL2ProtocolError subclass also gets "Error: " prefix
    def test_subclass_gets_error_prefix(self):
        err = ConfigurationError("bad config")
        assert format_error_for_cli(err).startswith("Error: ")

    # Post: deeply-nested subclass also dispatches to NL2ProtocolError branch
    def test_deep_subclass_gets_error_prefix(self):
        err = LabwareNotFoundError("plate_xyz")
        # LabwareNotFoundError -> EquipmentError -> NL2ProtocolError
        assert format_error_for_cli(err).startswith("Error: ")

    # Post: built-in exception → "Unexpected error: " prefix with class name
    def test_builtin_exception_gets_unexpected_prefix_with_class_name(self):
        err = ValueError("invalid arg")
        result = format_error_for_cli(err)
        assert result.startswith("Unexpected error: ")
        assert "ValueError" in result
        assert "invalid arg" in result

    # Post: exact-class name appears, not a parent class name
    def test_exact_class_name_appears(self):
        err = TypeError("bad type")
        result = format_error_for_cli(err)
        assert "TypeError" in result
        assert "Exception" not in result  # parent class shouldn't appear


# ============================================================================
# format_api_error — contract tests
# ============================================================================

class TestFormatApiError:
    """Contract tests for format_api_error dispatch table."""

    # Post: AuthenticationError → auth message
    def test_authentication_error(self):
        msg = format_api_error(_FakeAuthError())
        assert "authentication" in msg.lower()
        assert "ANTHROPIC_API_KEY" in msg

    # Post: RateLimitError → rate-limit message
    def test_rate_limit_error(self):
        msg = format_api_error(_FakeRateLimitError())
        assert "rate limit" in msg.lower()
        assert "wait" in msg.lower()

    # Post: APIStatusError with "credit" in message → credit-balance branch
    def test_api_status_error_credit(self):
        msg = format_api_error(_FakeStatusError("Insufficient credit balance"))
        assert "credit" in msg.lower()
        assert "console.anthropic.com" in msg

    # Post: APIStatusError with "balance" in message → credit-balance branch
    def test_api_status_error_balance(self):
        msg = format_api_error(_FakeStatusError("Account balance too low"))
        assert "credit balance" in msg.lower()

    # Post: APIStatusError with "overloaded" → overloaded branch
    def test_api_status_error_overloaded(self):
        msg = format_api_error(_FakeStatusError("API is overloaded right now"))
        assert "overloaded" in msg.lower()

    # Post: APIStatusError with generic message → status-code branch
    def test_api_status_error_generic(self):
        msg = format_api_error(_FakeStatusError("server error", status_code=503))
        assert "503" in msg
        assert "transient" in msg.lower() or "retry" in msg.lower()

    # Post: APITimeoutError → timeout message
    def test_timeout_error(self):
        msg = format_api_error(_FakeTimeoutError())
        assert "timed out" in msg.lower() or "timeout" in msg.lower()

    # Post: APIConnectionError → connection message
    def test_connection_error(self):
        msg = format_api_error(_FakeConnectionError())
        assert "could not connect" in msg.lower() or "connection" in msg.lower()

    # Post: unknown exception type → "Unexpected error: <ClassName>"
    def test_unknown_exception_falls_through_to_else(self):
        msg = format_api_error(ValueError("random error"))
        assert msg == "Unexpected error: ValueError"

    # Post: dispatch order — credit beats overloaded when both substrings present
    def test_credit_beats_overloaded_when_both_present(self):
        msg = format_api_error(_FakeStatusError("credit balance and overloaded"))
        assert "credit" in msg.lower()
        assert "overloaded" not in msg.lower()


# ============================================================================
# Exception classes — message-construction smoke tests
# ============================================================================

class TestExceptionMessages:
    """Verify __init__ methods build messages with the documented hints."""

    def test_api_key_error_message_includes_setup_instructions(self):
        err = APIKeyError("MY_KEY")
        s = str(err)
        assert "MY_KEY" in s
        assert "console.anthropic.com" in s
        assert ".env" in s

    def test_input_validation_error_includes_classification_and_examples(self):
        err = InputValidationError(
            classification="INVALID",
            reason="off-topic",
            suggestion="rephrase as a protocol",
        )
        s = str(err)
        assert "INVALID" in s
        assert "off-topic" in s
        assert "rephrase as a protocol" in s
        assert "Transfer 100uL" in s  # examples block

    def test_labware_not_found_includes_available_labware(self):
        err = LabwareNotFoundError("missing_plate", available=["plate_a", "plate_b"])
        s = str(err)
        assert "missing_plate" in s
        assert "plate_a" in s
        assert "plate_b" in s
