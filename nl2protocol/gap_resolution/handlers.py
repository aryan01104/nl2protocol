"""
ConfirmationHandler implementations (ADR-0008 PR2).

CLIConfirmationHandler is the only implementation in PR2. It collapses
today's three CLI prompt formats (_confirm_provenance_items,
_build_initial_volume_queue, _confirm_labware_assignments) into one
uniform present(gap, suggestion) -> Resolution method.

Future: HTMLConfirmationHandler renders Gap+Suggestion to the existing
report DOM and accepts user input via a bridge mechanism (separate ADR).

Both handlers consume the same Gap + Suggestion shapes — that's the
swap point ADR-0008 makes possible.
"""

from __future__ import annotations

from typing import Optional

from nl2protocol.gap_resolution.protocols import ConfirmationHandler
from nl2protocol.gap_resolution.types import (
    Gap,
    Resolution,
    Suggestion,
)


class CLIConfirmationHandler:
    """Single-prompt CLI handler. One method serves every gap kind.

    The prompt format is uniform:
      - Header: gap description + severity tag
      - Current value (if any)
      - Suggested value + reasoning (if a Suggestion exists)
      - Reviewer objection (if review_status was reviewed_disagree)
      - Action prompt: [a]ccept / [e]dit / [s]kip / [q]uit

    Pre:    `cm` is a ConfirmationManager with a `prompt(text)` method
            (today's existing CLI prompt facility).
            `coerce_value` is an optional callback that converts a user-typed
            string into the right type for the gap's field. Defaults to
            identity (string in → string out).
    Post:   Returns a Resolution per the user's input. action="abort" halts
            the loop; "skip" leaves the gap unresolved; "edit" / "accept_suggestion"
            carry a new_value to apply.
    """

    def __init__(self, cm, coerce_value=None, log=None):
        self._cm = cm
        self._coerce = coerce_value or (lambda gap, raw: raw)
        # `log` is an optional callback; defaults to print so the handler
        # also works without the pipeline's _log helper available.
        self._log = log or print

    def present(self, gap: Gap, suggestion: Optional[Suggestion]) -> Resolution:
        self._render_gap(gap, suggestion)
        prompt_text = self._action_prompt(suggestion, gap)
        response = self._cm.prompt(prompt_text).strip().lower()
        return self._interpret_response(response, gap, suggestion)

    def _render_gap(self, gap: Gap, suggestion: Optional[Suggestion]) -> None:
        sev_tag = {"blocker": "BLOCKER", "suggestion": "needs review",
                    "quality": "optional"}.get(gap.severity, gap.severity)
        self._log(f"\n  [{sev_tag}] {gap.description}")
        if gap.current_value is not None:
            self._log(f"    current: {gap.current_value!r}")
        if suggestion is None:
            self._log(f"    (no suggestion available)")
            return
        self._log(f"    suggested: {self._format_value(suggestion.value)}")
        self._log(f"    reasoning: {suggestion.positive_reasoning}")
        if suggestion.why_not_in_instruction:
            self._log(f"    why not from instruction: {suggestion.why_not_in_instruction}")
        self._log(f"    confidence: {suggestion.confidence:.0%}")

    def _action_prompt(self, suggestion: Optional[Suggestion],
                        gap: Optional[Gap] = None) -> str:
        # ADR-0012: fabrication gaps add an [o]verride option — keeps the
        # current value as-is but stamps user_overrode_fabrication on the
        # audit trail. Rare path (most fabrications get fixed by LLMSpot
        # or edited by the user); option only appears when the gap is
        # actually a fabrication so other gap kinds aren't confused.
        is_fabrication = gap is not None and gap.kind == "fabricated"
        override_segment = ", [o]verride (keep current value)" if is_fabrication else ""
        if suggestion is None:
            return f"[e]dit (type value){override_segment}, [s]kip, [q]uit: "
        return f"[a]ccept suggestion, [e]dit{override_segment}, [s]kip, [q]uit: "

    def _interpret_response(
        self,
        response: str,
        gap: Gap,
        suggestion: Optional[Suggestion],
    ) -> Resolution:
        if response in ("q", "quit", "abort"):
            return Resolution(action="abort", new_value=None,
                              user_action_provenance="user_aborted")
        if response in ("s", "skip"):
            return Resolution(action="skip", new_value=None,
                              user_action_provenance="user_skipped")
        # Accept-suggestion path: only valid when a suggestion exists.
        # Empty input ALSO accepts the suggestion (since the prompt is
        # opinionated about that being the default action).
        if response in ("a", "accept", "y", "yes", ""):
            if suggestion is not None:
                return Resolution(action="accept_suggestion",
                                  new_value=suggestion.value,
                                  user_action_provenance="user_accepted_suggestion")
            # No suggestion to accept — treat as skip rather than silently dropping.
            # (User saw "no suggestion available" in the rendered gap; their
            # "yes" is most likely "yes I see it, move on.")
            return Resolution(action="skip", new_value=None,
                              user_action_provenance="user_skipped")
        if response in ("e", "edit"):
            raw = self._cm.prompt(f"  new value for {gap.field_path}: ").strip()
            try:
                value = self._coerce(gap, raw)
            except Exception as e:
                self._log(f"    invalid value ({e}); skipping.")
                return Resolution(action="skip", new_value=None,
                                  user_action_provenance="user_skipped")
            return Resolution(action="edit", new_value=value,
                              user_action_provenance="user_edited")
        # ADR-0012: override path. Only valid for fabrication gaps. Keeps
        # the current value and stamps the audit trail with
        # user_overrode_fabrication. Defensive: silently treat as skip
        # if the gap isn't a fabrication (option shouldn't have been
        # presented in that case anyway).
        if response in ("o", "override"):
            if gap.kind == "fabricated":
                return Resolution(action="override", new_value=None,
                                  user_action_provenance="user_overrode_fabrication")
            self._log(f"    override only valid for fabrication gaps; skipping.")
            return Resolution(action="skip", new_value=None,
                              user_action_provenance="user_skipped")
        # Anything else: treat as skip (defensive).
        self._log(f"    unrecognized response; skipping.")
        return Resolution(action="skip", new_value=None,
                          user_action_provenance="user_skipped")

    @staticmethod
    def _format_value(v) -> str:
        # Compact rendering for common spec value shapes.
        try:
            from nl2protocol.models.spec import (
                LocationRef, ProvenancedTemperature, ProvenancedDuration,
                ProvenancedVolume,
            )
        except ImportError:
            return repr(v)
        if isinstance(v, LocationRef):
            return f"{v.description} (well {v.well or v.wells})"
        if isinstance(v, (ProvenancedTemperature, ProvenancedDuration,
                          ProvenancedVolume)):
            return f"{v.value}"
        return repr(v)
