"""
confirmation.py — Pluggable user-confirmation interface for the pipeline.

The CLI runs `ProtocolAgent.run_pipeline` with an `InteractiveCM` (default)
that prompts the user via stdin. Tests and evals construct the agent with a
`ScriptedCM(answers)` or `AutoConfirmCM()` to drive the pipeline
non-interactively.

Why a thin single-method interface: the existing pipeline.py parsing logic
expects free-form string responses ('q', '', '1', '2', 'y', 'e', etc.) that
callers parse case-by-case. A richer interface (e.g. typed `confirm() -> bool`,
`pick() -> str | None`) would require rewriting every parsing site. This thin
wrapper preserves the call-site parsing and only abstracts where the answer
comes from.
"""

from typing import List, Protocol


class ConfirmationManager(Protocol):
    """Single-method interface: given a question, return the user's response.

    All existing parsing of the response (digits, 'q', 'e', empty for accept,
    etc.) lives in the caller. This interface only abstracts the *source* of
    the response — interactive stdin vs. canned answers vs. always-default.
    """

    def prompt(self, question: str) -> str:
        """Return the response to `question` as a stripped string."""
        ...


class InteractiveCM:
    """Default CLI behavior — reads from stdin via input(), with the existing
    UX styling (blank line before the prompt for visual breathing room, color
    formatting via nl2protocol.colors)."""

    def prompt(self, question: str) -> str:
        # Lazy imports to avoid coupling on the colors / pipeline modules at module-load time.
        from nl2protocol import colors as C
        print()  # blank line for visual breathing room before the prompt
        return input(C.prompt(question)).strip()


class AutoConfirmCM:
    """Always returns the empty string — 'Enter = accept default' in most flows.

    Use for non-interactive runs where the goal is to advance through the
    pipeline accepting every default suggestion. Not appropriate for flows
    where Enter has destructive meaning (review pipeline.py call sites if
    using this).
    """

    def prompt(self, question: str) -> str:
        return ""


class ScriptedCM:
    """Pops responses from a queue in order. For tests and evals.

    Construct with the list of answers the test wants to feed in. Each
    `prompt()` call pops the next answer. Raises `IndexError` if the
    pipeline asks more questions than the script anticipated — that's a
    test-design bug worth surfacing loudly.

    Pre:    `answers` is a list of strings, in the order they should be
            consumed by sequential `prompt()` calls.

    Post:   Each `prompt(...)` call returns the next answer in the queue
            (FIFO), regardless of the question content. Question text is
            ignored — answers are positional.

    Raises: IndexError if `prompt()` is called more times than there are
            answers in the queue.
    """

    def __init__(self, answers: List[str]):
        self._answers = list(answers)
        self._call_count = 0

    def prompt(self, question: str) -> str:
        if self._call_count >= len(self._answers):
            raise IndexError(
                f"ScriptedCM exhausted: pipeline asked {self._call_count + 1} "
                f"questions but only {len(self._answers)} answers were scripted. "
                f"Last question was: {question!r}"
            )
        answer = self._answers[self._call_count]
        self._call_count += 1
        return answer

    @property
    def remaining(self) -> int:
        """Number of unconsumed scripted answers."""
        return len(self._answers) - self._call_count
