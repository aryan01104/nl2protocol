"""
input_validator.py

LLM-based input validation to classify user inputs before protocol generation.
Uses Claude Haiku for fast, cheap classification.

Classifications:
- PROTOCOL: Valid lab protocol instruction → proceed to generation
- QUESTION: Question about protocols/system → answer differently
- AMBIGUOUS: Could be protocol but needs clarification → ask user
- INVALID: Off-topic, nonsense, not lab-related → reject
"""

import json
import os
import sys
from dataclasses import dataclass
from typing import Literal, Optional
from anthropic import Anthropic
from dotenv import find_dotenv, load_dotenv
from nl2protocol.errors import APIKeyError


Classification = Literal["PROTOCOL", "QUESTION", "AMBIGUOUS", "INVALID"]


@dataclass
class InputValidationResult:
    """Result of input classification."""
    classification: Classification
    reason: str
    suggestion: Optional[str] = None

    @property
    def is_valid_protocol(self) -> bool:
        return self.classification == "PROTOCOL"

    def __str__(self) -> str:
        result = f"[{self.classification}] {self.reason}"
        if self.suggestion:
            result += f"\nSuggestion: {self.suggestion}"
        return result


def check_input_length(user_input: str) -> Optional[InputValidationResult]:
    """Deterministic length pre-check; returns INVALID result if out of bounds, else None.

    Pre:    `user_input` is a string.

    Post:   Returns InputValidationResult(classification="INVALID", reason=...,
            suggestion=...) iff one of:
              * `len(user_input.strip()) < 3` → reason "Input too short"
              * `len(user_input) > 10000`     → reason "Input too long"
            Otherwise returns None — caller should proceed with full
            classification (typically the LLM call).

    Side effects: None. Pure.

    Raises: Never.
    """
    if len(user_input.strip()) < 3:
        return InputValidationResult(
            classification="INVALID",
            reason="Input too short",
            suggestion="Please provide a complete protocol instruction, e.g., 'Transfer 100uL from well A1 to B1'",
        )
    if len(user_input) > 10000:
        return InputValidationResult(
            classification="INVALID",
            reason="Input too long",
            suggestion="Please provide a concise protocol instruction (under 10,000 characters)",
        )
    return None


CLASSIFY_PROMPT = """You are classifying user inputs for a lab protocol generation system.

Classify the input as one of:
- PROTOCOL: A valid lab protocol instruction (liquid handling, transfers, dilutions, mixing, pipetting, etc.)
- QUESTION: A question about protocols, the system, or how to do something
- AMBIGUOUS: Could be a protocol but is too vague or needs clarification
- INVALID: Off-topic, nonsense, greeting, or not related to lab work

Examples:
- "Transfer 100uL from A1 to B1" → PROTOCOL
- "Perform a serial dilution across row A" → PROTOCOL
- "Mix the samples in column 1" → PROTOCOL
- "How do I do a serial dilution?" → QUESTION
- "What pipettes are available?" → QUESTION
- "Hello" → INVALID
- "Tell me a joke" → INVALID
- "dilution" → AMBIGUOUS (too vague - what kind? where?)
- "move liquid" → AMBIGUOUS (missing details)

Respond with JSON only:
{
    "classification": "PROTOCOL" | "QUESTION" | "AMBIGUOUS" | "INVALID",
    "reason": "Brief explanation of why this classification",
    "suggestion": "If not PROTOCOL, suggest how user could rephrase (null if PROTOCOL)"
}

Input to classify:
"""


class InputValidator:
    """Validates user inputs before protocol generation using LLM classification."""

    def __init__(self, model_name: str = "claude-sonnet-4-20250514"):
        load_dotenv(find_dotenv(usecwd=True))
        self.model_name = model_name
        self._setup_client()

    def _setup_client(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise APIKeyError("ANTHROPIC_API_KEY")
        self.client = Anthropic(api_key=api_key)

    def classify(self, user_input: str) -> InputValidationResult:
        """Classify `user_input` as PROTOCOL / QUESTION / AMBIGUOUS / INVALID.

        Pre:    `user_input` is a string. `self.client` is a constructed
                Anthropic client (set in __init__ via `_setup_client`).

        Post:   Delegates the deterministic length pre-check to
                `check_input_length`. If that returns an InputValidationResult,
                returns it directly (no LLM call). Otherwise, calls the
                Anthropic API and returns an InputValidationResult parsed
                from the JSON response. The returned classification is one
                of {PROTOCOL, QUESTION, AMBIGUOUS, INVALID}; `reason` is a
                non-empty string; `suggestion` is a string or None.

        Side effects: None when the length pre-check rejects. Otherwise
                      makes an Anthropic API call (network I/O) and renders
                      a Spinner to stdout.

        Raises: RuntimeError ("Input validation failed: ...") if the API
                call fails or the response is unparseable.
        """
        early = check_input_length(user_input)
        if early is not None:
            return early

        try:
            from nl2protocol.spinner import Spinner
            with Spinner("Classifying input..."):
                response = self.client.messages.create(
                    model=self.model_name,
                    max_tokens=256,
                    messages=[{
                        "role": "user",
                        "content": f"{CLASSIFY_PROMPT}{user_input}"
                    }]
                )

            result_text = response.content[0].text

            # Parse JSON response
            # Handle potential markdown code blocks
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0]
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0]

            result = json.loads(result_text.strip())

            return InputValidationResult(
                classification=result.get("classification", "INVALID"),
                reason=result.get("reason", "Classification failed"),
                suggestion=result.get("suggestion")
            )

        except Exception as e:
            from nl2protocol.errors import format_api_error
            raise RuntimeError(f"Input validation failed: {format_api_error(e)}") from e


def validate_input(user_input: str) -> InputValidationResult:
    """
    Convenience function to validate input without creating validator instance.

    Args:
        user_input: The raw user input string

    Returns:
        InputValidationResult with classification details
    """
    validator = InputValidator()
    return validator.classify(user_input)
