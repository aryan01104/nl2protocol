"""
Contract tests for nl2protocol.confirmation.

Covers the three ConfirmationManager implementations: InteractiveCM (CLI
default), AutoConfirmCM (non-interactive), ScriptedCM (tests/evals).
"""

import pytest

from nl2protocol.confirmation import (
    AutoConfirmCM,
    InteractiveCM,
    ScriptedCM,
)


# ============================================================================
# AutoConfirmCM
# ============================================================================

class TestAutoConfirmCM:
    """Always returns the empty string — 'Enter = accept default' in pipeline parsing."""

    def test_returns_empty_string_for_any_question(self):
        cm = AutoConfirmCM()
        assert cm.prompt("anything") == ""
        assert cm.prompt("Pick 1-3: ") == ""
        assert cm.prompt("") == ""


# ============================================================================
# ScriptedCM
# ============================================================================

class TestScriptedCM:
    """Pops responses from a queue in order; question text is ignored."""

    def test_returns_answers_in_order(self):
        cm = ScriptedCM(["yes", "1", ""])
        assert cm.prompt("Q1?") == "yes"
        assert cm.prompt("Q2?") == "1"
        assert cm.prompt("Q3?") == ""

    def test_question_text_is_ignored(self):
        # Same answer is returned regardless of what the question is.
        cm = ScriptedCM(["fixed_answer"])
        assert cm.prompt("Pick 1-3:") == "fixed_answer"

    def test_remaining_property_decrements(self):
        cm = ScriptedCM(["a", "b", "c"])
        assert cm.remaining == 3
        cm.prompt("?")
        assert cm.remaining == 2
        cm.prompt("?")
        cm.prompt("?")
        assert cm.remaining == 0

    def test_exhaustion_raises_index_error_with_helpful_message(self):
        cm = ScriptedCM(["only_one"])
        cm.prompt("first call OK")
        with pytest.raises(IndexError, match="ScriptedCM exhausted"):
            cm.prompt("second call should fail")

    def test_empty_script_raises_immediately(self):
        cm = ScriptedCM([])
        with pytest.raises(IndexError):
            cm.prompt("Q?")

    def test_constructor_copies_input_list(self):
        # Mutating the original list should not affect the manager.
        answers = ["a", "b"]
        cm = ScriptedCM(answers)
        answers.clear()
        assert cm.prompt("?") == "a"
        assert cm.prompt("?") == "b"


# ============================================================================
# InteractiveCM
# ============================================================================

class TestInteractiveCM:
    """Wraps input() — verified via monkeypatched stdin."""

    def test_calls_input_and_strips_response(self, monkeypatch, capsys):
        # Stub input() to return a known string with surrounding whitespace.
        monkeypatch.setattr("builtins.input", lambda _prompt: "  yes  ")
        cm = InteractiveCM()
        result = cm.prompt("Continue?")
        assert result == "yes"  # stripped

    def test_emits_blank_line_before_prompt_for_breathing_room(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda _prompt: "x")
        cm = InteractiveCM()
        cm.prompt("Q?")
        captured = capsys.readouterr()
        # Blank-line print() before the input call → at least one newline in stdout.
        assert "\n" in captured.out

    def test_passes_question_through_to_input(self, monkeypatch):
        captured_question = []
        def fake_input(question):
            captured_question.append(question)
            return ""
        monkeypatch.setattr("builtins.input", fake_input)
        cm = InteractiveCM()
        cm.prompt("UNIQUE_QUESTION_TEXT")
        # Question should appear in what input() received (formatted by colors module).
        assert any("UNIQUE_QUESTION_TEXT" in q for q in captured_question)
