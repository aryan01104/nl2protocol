"""
Contract tests for HTMLConfirmationHandler + PendingRequest (ADR-0013 Phase 3c).

These pin the thread-bridge primitive: how a Gap arriving on the pipeline
thread is converted to a panel_request, how the handler blocks until the
WebSocket receiver signals back, and how the browser's action token
maps onto a Resolution.

Network-level WS plumbing (FastAPI receive_json + create_task) is exercised
by the smoke run, not unit-tested here — these tests target the synchronous
core that doesn't require an event loop.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

import pytest

from nl2protocol.gap_resolution.targets import path_to_target
from nl2protocol.gap_resolution.types import Gap, Resolution, Suggestion
from nl2protocol.server.handlers import (
    AssignmentsConfirmation,
    BinaryConfirmation,
    HTMLAssignmentsHandler,
    HTMLBinaryConfirmHandler,
    HTMLConfirmationHandler,
    HTMLInitialContentsHandler,
    HTMLNamespaceSplitHandler,
    InitialContentsConfirmation,
    NamespaceSplitConfirmation,
    PendingRequest,
)


def _make_gap(kind: str = "missing_required",
              severity: str = "blocking",
              current_value: Any = None) -> Gap:
    return Gap(
        id="step0.volume",
        step_order=0,
        targets=[path_to_target("steps[0].volume")],
        kind=kind,
        current_value=current_value,
        description="volume missing on step 0",
        severity=severity,
        metadata={},
    )


def _make_suggestion(value: Any = "10 µL",
                     source: str = "deterministic",
                     confidence: float = 0.9) -> Suggestion:
    return Suggestion(
        value=value,
        provenance_source=source,
        positive_reasoning="this is a sensible default for the assay",
        why_not_in_instruction="instruction omits explicit volume",
        confidence=confidence,
    )


# ============================================================================
# PendingRequest.set_response_from_action
# ============================================================================

class TestPendingRequestActionMapping:
    """Browser sends an action token; the receiver maps it onto a Resolution
    and signals the waiting thread. These tests pin the mapping table."""

    def test_accept_with_suggestion_picks_suggestion_value(self):
        gap = _make_gap()
        sug = _make_suggestion(value="42 µL")
        pr = PendingRequest(gap=gap, suggestion=sug)
        pr.set_response_from_action("accept")
        assert pr.response is not None
        assert pr.response.action == "accept_suggestion"
        assert pr.response.new_value == "42 µL"
        assert pr.response.user_action_provenance == "user_accepted_suggestion"

    def test_accept_without_suggestion_falls_through_to_skip(self):
        # Defensive: browser shouldn't show Accept without a suggestion,
        # but if a malformed payload arrives we treat it as skip rather
        # than build an invalid accept_suggestion (which expects a value).
        gap = _make_gap(severity="quality")
        pr = PendingRequest(gap=gap, suggestion=None)
        pr.set_response_from_action("accept")
        assert pr.response.action == "skip"
        assert pr.response.user_action_provenance == "user_skipped"

    def test_override_on_fabricated_gap_keeps_current_value(self):
        # ADR-0012: override stamps user_overrode_fabrication, keeps the
        # existing fabricated value. GapKind literal is "fabricated" (not
        # "fabrication") — the modal's button-gating must use the same.
        gap = _make_gap(kind="fabricated", current_value="500 µL")
        pr = PendingRequest(gap=gap, suggestion=None)
        pr.set_response_from_action("override")
        assert pr.response.action == "override"
        assert pr.response.new_value == "500 µL"
        assert pr.response.user_action_provenance == "user_overrode_fabrication"

    def test_override_on_non_fabricated_gap_falls_through_to_skip(self):
        # Defensive: stray override token on a non-fabrication gap must
        # not write user_overrode_fabrication provenance. Mirrors the
        # CLI handler's "override only valid for fabrication" branch.
        gap = _make_gap(kind="missing", current_value=None)
        pr = PendingRequest(gap=gap, suggestion=None)
        pr.set_response_from_action("override")
        assert pr.response.action == "skip"
        assert pr.response.user_action_provenance == "user_skipped"

    def test_abort_returns_abort_resolution(self):
        pr = PendingRequest(gap=_make_gap(), suggestion=_make_suggestion())
        pr.set_response_from_action("abort")
        assert pr.response.action == "abort"
        assert pr.response.new_value is None
        assert pr.response.user_action_provenance == "user_aborted"

    def test_skip_returns_skip_resolution(self):
        pr = PendingRequest(gap=_make_gap(severity="quality"),
                            suggestion=_make_suggestion())
        pr.set_response_from_action("skip")
        assert pr.response.action == "skip"
        assert pr.response.new_value is None
        assert pr.response.user_action_provenance == "user_skipped"

    def test_unknown_token_defaults_to_skip(self):
        # Defensive: unknown tokens shouldn't crash the receiver; the
        # pipeline can't always recover from an abort, but a skip on a
        # blocking gap will surface as an unresolved-gap error from the
        # orchestrator's existing convergence check.
        pr = PendingRequest(gap=_make_gap(), suggestion=_make_suggestion())
        pr.set_response_from_action("nonsense")
        assert pr.response.action == "skip"

    def test_edit_with_numeric_field_path_coerces_to_float(self):
        # Edit on a volume gap should produce a float new_value so the
        # orchestrator's _apply_at_target can write `existing.value =
        # <float>` against a ProvenancedVolume.
        gap = _make_gap()  # field_path = "steps[0].volume" → numeric
        pr = PendingRequest(gap=gap, suggestion=None)
        pr.set_response_from_action("edit", "100")
        assert pr.response.action == "edit"
        assert pr.response.new_value == 100.0
        assert pr.response.user_action_provenance == "user_edited"

    def test_edit_with_non_numeric_value_passes_through_as_string(self):
        # Substance / labware-description fields stay as text.
        gap = Gap(
            id="step0.substance",
            step_order=0,
            targets=[path_to_target("steps[0].substance")],
            kind="missing",
            current_value=None,
            description="substance missing on step 0",
            severity="blocker",
            metadata={},
        )
        pr = PendingRequest(gap=gap, suggestion=None)
        pr.set_response_from_action("edit", "Bradford reagent")
        assert pr.response.action == "edit"
        assert pr.response.new_value == "Bradford reagent"

    def test_edit_without_raw_value_falls_through_to_skip(self):
        # Defensive: an "edit" action with no raw_value (malformed payload)
        # shouldn't crash; treat as skip.
        gap = _make_gap()
        pr = PendingRequest(gap=gap, suggestion=None)
        pr.set_response_from_action("edit", None)
        assert pr.response.action == "skip"

    def test_set_response_signals_event_last(self):
        # The event must only be set AFTER `response` is populated, so a
        # waiter that wakes up on the event always sees a non-None
        # response.
        pr = PendingRequest(gap=_make_gap(), suggestion=_make_suggestion())
        # Before set, event not signaled.
        assert not pr.event.is_set()
        pr.set_response_from_action("accept")
        # After set, event signaled AND response populated.
        assert pr.event.is_set()
        assert pr.response is not None


# ============================================================================
# HTMLConfirmationHandler.present
# ============================================================================

class TestHTMLConfirmationHandlerPresent:
    """The handler is the ConfirmationHandler used by the orchestrator in
    live mode. Its present() must:
      - generate a unique request_id
      - call send_panel_request with the serialized payload
      - block on the PendingRequest's event
      - return a Resolution built from the response
      - clean up the pending_requests dict in all paths
    """

    def test_present_pushes_payload_via_send_callable(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, PendingRequest] = {}
        handler = HTMLConfirmationHandler(
            send_panel_request=sent.append,
            pending_requests=pending,
            timeout_seconds=2,
        )
        gap = _make_gap()
        sug = _make_suggestion()

        # Simulate the browser responding asynchronously: signal the
        # PendingRequest after a short delay so present() unblocks.
        def _respond_after_delay():
            time.sleep(0.05)
            # The handler stores the PendingRequest under some rid.
            assert len(pending) == 1
            rid = next(iter(pending))
            pending[rid].set_response_from_action("accept")

        threading.Thread(target=_respond_after_delay, daemon=True).start()

        result = handler.present(gap, sug)

        # send_panel_request was called once with the right shape.
        assert len(sent) == 1
        payload = sent[0]
        assert "request_id" in payload
        assert payload["gap"]["kind"] == "missing_required"
        assert payload["suggestion"]["value_repr"] == "10 µL"

        # Returned Resolution matches what set_response_from_action built.
        assert result.action == "accept_suggestion"
        assert result.new_value == "10 µL"

    def test_present_cleans_up_pending_dict_on_signal(self):
        pending: Dict[str, PendingRequest] = {}
        sent: List[Dict[str, Any]] = []
        handler = HTMLConfirmationHandler(
            send_panel_request=sent.append,
            pending_requests=pending,
            timeout_seconds=2,
        )

        def _respond():
            time.sleep(0.05)
            rid = next(iter(pending))
            pending[rid].set_response_from_action("skip")

        threading.Thread(target=_respond, daemon=True).start()
        handler.present(_make_gap(severity="quality"), _make_suggestion())
        # Dict should be empty after present returns — no stale entries.
        assert pending == {}

    def test_present_returns_abort_on_timeout(self):
        # Timeout = 0.05s. Nobody signals the event → handler should
        # return an abort Resolution (per ADR-0013 "browser disconnect /
        # inactivity" branch).
        pending: Dict[str, PendingRequest] = {}
        handler = HTMLConfirmationHandler(
            send_panel_request=lambda _: None,
            pending_requests=pending,
            timeout_seconds=0.05,
        )
        result = handler.present(_make_gap(), _make_suggestion())
        assert result.action == "abort"
        assert result.user_action_provenance == "user_aborted"
        # Dict cleaned up even on timeout path.
        assert pending == {}

    def test_present_handles_no_suggestion(self):
        # Quality gaps may arrive without a Suggestion (no suggester
        # produced one). Browser should still get a payload with
        # suggestion: None.
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, PendingRequest] = {}
        handler = HTMLConfirmationHandler(
            send_panel_request=sent.append,
            pending_requests=pending,
            timeout_seconds=2,
        )

        def _respond():
            time.sleep(0.05)
            rid = next(iter(pending))
            pending[rid].set_response_from_action("skip")

        threading.Thread(target=_respond, daemon=True).start()
        result = handler.present(_make_gap(severity="quality"), None)
        assert sent[0]["suggestion"] is None
        assert result.action == "skip"

    def test_request_ids_are_unique_across_calls(self):
        # Each present() must produce a different request_id so concurrent
        # gaps can be disambiguated by the receiver.
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, PendingRequest] = {}
        handler = HTMLConfirmationHandler(
            send_panel_request=sent.append,
            pending_requests=pending,
            timeout_seconds=2,
        )

        def _respond_each():
            # Signal each new pending entry as it appears.
            seen = set()
            while len(seen) < 2:
                for rid in list(pending.keys()):
                    if rid not in seen:
                        pending[rid].set_response_from_action("skip")
                        seen.add(rid)
                time.sleep(0.01)

        threading.Thread(target=_respond_each, daemon=True).start()
        handler.present(_make_gap(severity="quality"), _make_suggestion())
        handler.present(_make_gap(severity="quality"), _make_suggestion())

        rids = [p["request_id"] for p in sent]
        assert len(rids) == 2
        assert rids[0] != rids[1]


# ============================================================================
# AssignmentsConfirmation + HTMLAssignmentsHandler (Phase 3d)
# ============================================================================

class TestAssignmentsConfirmationActionMapping:
    """Receiver writes the confirmed map (or aborted flag) onto the
    primitive; pipeline thread reads either off the same instance."""

    def test_confirm_stores_assignments_dict(self):
        ac = AssignmentsConfirmation()
        ac.set_response("confirm", {"tube rack": "tuberack_24", "plate": "wellplate_96"})
        assert ac.aborted is False
        assert ac.confirmed == {"tube rack": "tuberack_24", "plate": "wellplate_96"}
        assert ac.event.is_set()

    def test_abort_clears_confirmed_and_sets_aborted(self):
        ac = AssignmentsConfirmation()
        ac.set_response("abort", None)
        assert ac.aborted is True
        assert ac.confirmed is None
        assert ac.event.is_set()

    def test_confirm_with_none_assignments_yields_empty_dict(self):
        # Defensive: malformed payload (missing assignments) on confirm
        # shouldn't crash; treat as "user confirmed an empty mapping".
        ac = AssignmentsConfirmation()
        ac.set_response("confirm", None)
        assert ac.aborted is False
        assert ac.confirmed == {}


class TestHTMLAssignmentsHandlerConfirm:
    """The handler's confirm() blocks until the WS receiver fills the
    AssignmentsConfirmation; returns the dict (or None on abort/timeout).
    Empty input table short-circuits without touching the queue."""

    def test_confirm_pushes_payload_via_send_callable(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, AssignmentsConfirmation] = {}
        h = HTMLAssignmentsHandler(
            send_request=sent.append,
            pending_assignments=pending,
            timeout_seconds=2,
        )

        def _respond():
            time.sleep(0.05)
            rid = next(iter(pending))
            pending[rid].set_response("confirm", {"tube rack": "tuberack_24"})

        threading.Thread(target=_respond, daemon=True).start()
        result = h.confirm([
            {"description": "tube rack", "suggested_label": "tuberack_24",
             "candidates": ["tuberack_24", "wellplate_96"]},
        ])
        assert len(sent) == 1
        payload = sent[0]
        assert "request_id" in payload
        assert payload["assignments"][0]["description"] == "tube rack"
        assert result == {"tube rack": "tuberack_24"}

    def test_confirm_returns_none_on_abort(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, AssignmentsConfirmation] = {}
        h = HTMLAssignmentsHandler(
            send_request=sent.append,
            pending_assignments=pending,
            timeout_seconds=2,
        )

        def _respond():
            time.sleep(0.05)
            rid = next(iter(pending))
            pending[rid].set_response("abort", None)

        threading.Thread(target=_respond, daemon=True).start()
        result = h.confirm([
            {"description": "x", "suggested_label": "y", "candidates": ["y"]},
        ])
        assert result is None

    def test_confirm_returns_none_on_timeout(self):
        # Timeout = 0.05s, no signal → handler should clean up the slot
        # and return None so pipeline halts gracefully.
        pending: Dict[str, AssignmentsConfirmation] = {}
        h = HTMLAssignmentsHandler(
            send_request=lambda _: None,
            pending_assignments=pending,
            timeout_seconds=0.05,
        )
        result = h.confirm([
            {"description": "x", "suggested_label": "y", "candidates": ["y"]},
        ])
        assert result is None
        # Slot cleaned up so a subsequent run starts fresh.
        assert pending == {}

    def test_empty_table_short_circuits_without_pushing(self):
        # When there are no LocationRefs to confirm, don't bother the
        # browser — return an empty dict immediately. No queue push, no
        # pending slot, no timeout risk.
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, AssignmentsConfirmation] = {}
        h = HTMLAssignmentsHandler(
            send_request=sent.append,
            pending_assignments=pending,
            timeout_seconds=2,
        )
        result = h.confirm([])
        assert result == {}
        assert sent == []
        assert pending == {}

    def test_confirm_cleans_up_pending_slot_on_signal(self):
        pending: Dict[str, AssignmentsConfirmation] = {}
        sent: List[Dict[str, Any]] = []
        h = HTMLAssignmentsHandler(
            send_request=sent.append,
            pending_assignments=pending,
            timeout_seconds=2,
        )

        def _respond():
            time.sleep(0.05)
            rid = next(iter(pending))
            pending[rid].set_response("confirm", {"x": "y"})

        threading.Thread(target=_respond, daemon=True).start()
        h.confirm([{"description": "x", "suggested_label": "y", "candidates": ["y"]}])
        assert pending == {}

    def test_request_ids_unique_across_calls(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, AssignmentsConfirmation] = {}
        h = HTMLAssignmentsHandler(
            send_request=sent.append,
            pending_assignments=pending,
            timeout_seconds=2,
        )

        def _respond_each():
            seen = set()
            while len(seen) < 2:
                for rid in list(pending.keys()):
                    if rid not in seen:
                        pending[rid].set_response("confirm", {"x": "y"})
                        seen.add(rid)
                time.sleep(0.01)

        threading.Thread(target=_respond_each, daemon=True).start()
        h.confirm([{"description": "x", "suggested_label": "y", "candidates": ["y"]}])
        h.confirm([{"description": "x", "suggested_label": "y", "candidates": ["y"]}])
        rids = [p["request_id"] for p in sent]
        assert len(rids) == 2
        assert rids[0] != rids[1]


# ============================================================================
# BinaryConfirmation + HTMLBinaryConfirmHandler (Phase 3e)
# ============================================================================

class TestBinaryConfirmationActionMapping:
    """Yes/no slot. Used by both source-container ack and hardware-error
    proceed. Unknown tokens fall through to False so an ambiguous browser
    response halts the pipeline gracefully."""

    def test_yes_sets_confirmed_true(self):
        bc = BinaryConfirmation()
        bc.set_response("yes")
        assert bc.confirmed is True
        assert bc.event.is_set()

    def test_no_sets_confirmed_false(self):
        bc = BinaryConfirmation()
        bc.set_response("no")
        assert bc.confirmed is False
        assert bc.event.is_set()

    def test_unknown_token_defaults_to_false(self):
        bc = BinaryConfirmation()
        bc.set_response("maybe")
        assert bc.confirmed is False


class TestHTMLBinaryConfirmHandlerConfirm:
    """confirm() blocks until the receiver fills the slot; returns True
    iff the user picked yes. Timeout returns False so pipeline halts.
    The payload carries title + items + button labels so each prompt
    site can label its buttons appropriately (e.g. 'Proceed anyway' vs
    'Yes, these are correct')."""

    def test_yes_returns_true(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, BinaryConfirmation] = {}
        h = HTMLBinaryConfirmHandler(
            send_request=sent.append,
            pending_confirms=pending,
            timeout_seconds=2,
        )

        def _respond():
            time.sleep(0.05)
            rid = next(iter(pending))
            pending[rid].set_response("yes")

        threading.Thread(target=_respond, daemon=True).start()
        result = h.confirm("Confirm sources",
                            ["tube rack well A1 (sample)"])
        assert result is True

    def test_payload_carries_title_items_labels(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, BinaryConfirmation] = {}
        h = HTMLBinaryConfirmHandler(
            send_request=sent.append,
            pending_confirms=pending,
            timeout_seconds=2,
        )

        def _respond():
            time.sleep(0.05)
            rid = next(iter(pending))
            pending[rid].set_response("yes")

        threading.Thread(target=_respond, daemon=True).start()
        h.confirm("Hardware conflicts",
                   ["pipette overloaded", "labware collision"],
                   yes_label="Proceed anyway", no_label="Abort")
        assert sent[0]["title"] == "Hardware conflicts"
        assert sent[0]["items"] == ["pipette overloaded", "labware collision"]
        assert sent[0]["yes_label"] == "Proceed anyway"
        assert sent[0]["no_label"] == "Abort"

    def test_no_returns_false(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, BinaryConfirmation] = {}
        h = HTMLBinaryConfirmHandler(
            send_request=sent.append,
            pending_confirms=pending,
            timeout_seconds=2,
        )

        def _respond():
            time.sleep(0.05)
            rid = next(iter(pending))
            pending[rid].set_response("no")

        threading.Thread(target=_respond, daemon=True).start()
        assert h.confirm("Are you sure", ["x"]) is False

    def test_timeout_returns_false_and_cleans_up(self):
        pending: Dict[str, BinaryConfirmation] = {}
        h = HTMLBinaryConfirmHandler(
            send_request=lambda _: None,
            pending_confirms=pending,
            timeout_seconds=0.05,
        )
        result = h.confirm("Are you sure", ["x"])
        assert result is False
        assert pending == {}


# ============================================================================
# InitialContentsConfirmation + HTMLInitialContentsHandler (Phase 3f)
# ============================================================================

class TestInitialContentsConfirmationActionMapping:
    """Receiver writes the confirmed list (or aborted flag) onto the
    primitive; pipeline thread reads either off the same instance."""

    def test_confirm_stores_volumes_list(self):
        ic = InitialContentsConfirmation()
        ic.set_response("confirm", [
            {"labware": "tube rack", "well": "A1", "volume_ul": 100.0},
            {"labware": "reservoir", "well": "A1", "volume_ul": 5000.0},
        ])
        assert ic.aborted is False
        assert ic.confirmed == [
            {"labware": "tube rack", "well": "A1", "volume_ul": 100.0},
            {"labware": "reservoir", "well": "A1", "volume_ul": 5000.0},
        ]
        assert ic.event.is_set()

    def test_abort_clears_confirmed_and_sets_aborted(self):
        ic = InitialContentsConfirmation()
        ic.set_response("abort", None)
        assert ic.aborted is True
        assert ic.confirmed is None
        assert ic.event.is_set()

    def test_confirm_with_none_volumes_yields_empty_list(self):
        # Defensive: malformed payload → treat as "confirmed empty".
        ic = InitialContentsConfirmation()
        ic.set_response("confirm", None)
        assert ic.aborted is False
        assert ic.confirmed == []


class TestHTMLInitialContentsHandlerConfirm:
    """confirm(table) blocks until the receiver fills the slot; returns
    confirmed list (None on abort/timeout). Empty input table short-
    circuits without touching the queue."""

    def test_confirm_pushes_payload_via_send_callable(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, InitialContentsConfirmation] = {}
        h = HTMLInitialContentsHandler(
            send_request=sent.append,
            pending_initial_contents=pending,
            timeout_seconds=2,
        )

        def _respond():
            time.sleep(0.05)
            rid = next(iter(pending))
            pending[rid].set_response("confirm", [
                {"labware": "tube rack", "well": "A1", "volume_ul": 100.0},
            ])

        threading.Thread(target=_respond, daemon=True).start()
        result = h.confirm([
            {"labware": "tube rack", "well": "A1", "substance": "sample",
             "current_volume_ul": None, "suggested_volume_ul": 100.0},
        ])
        assert len(sent) == 1
        assert sent[0]["rows"][0]["labware"] == "tube rack"
        assert result == [
            {"labware": "tube rack", "well": "A1", "volume_ul": 100.0},
        ]

    def test_confirm_returns_none_on_abort(self):
        pending: Dict[str, InitialContentsConfirmation] = {}
        h = HTMLInitialContentsHandler(
            send_request=lambda _: None,
            pending_initial_contents=pending,
            timeout_seconds=2,
        )

        def _respond():
            time.sleep(0.05)
            rid = next(iter(pending))
            pending[rid].set_response("abort", None)

        threading.Thread(target=_respond, daemon=True).start()
        result = h.confirm([
            {"labware": "x", "well": "A1", "substance": "y",
             "current_volume_ul": None, "suggested_volume_ul": 10.0},
        ])
        assert result is None

    def test_empty_table_short_circuits_without_pushing(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, InitialContentsConfirmation] = {}
        h = HTMLInitialContentsHandler(
            send_request=sent.append,
            pending_initial_contents=pending,
            timeout_seconds=2,
        )
        result = h.confirm([])
        assert result == []
        assert sent == []
        assert pending == {}

    def test_confirm_returns_none_on_timeout(self):
        pending: Dict[str, InitialContentsConfirmation] = {}
        h = HTMLInitialContentsHandler(
            send_request=lambda _: None,
            pending_initial_contents=pending,
            timeout_seconds=0.05,
        )
        result = h.confirm([
            {"labware": "x", "well": "A1", "substance": "y",
             "current_volume_ul": None, "suggested_volume_ul": 10.0},
        ])
        assert result is None
        assert pending == {}


# ============================================================================
# NamespaceSplitConfirmation + HTMLNamespaceSplitHandler
# ============================================================================
# Same blocking pattern as AssignmentsConfirmation/HTMLAssignmentsHandler
# but the payload carries letter-prefix groups (A/B/C) + per-group
# candidate labware rather than free-text descriptions.


class TestNamespaceSplitConfirmationActionMapping:
    """Receiver writes the prefix-to-label map (or aborted flag) onto
    the primitive; pipeline thread reads either off the same instance."""

    def test_confirm_stores_prefix_mapping(self):
        nc = NamespaceSplitConfirmation()
        nc.set_response(
            "confirm",
            {"A": "sample_rack", "B": "reagent_rack", "C": "dest_block"},
        )
        assert nc.aborted is False
        assert nc.confirmed == {"A": "sample_rack", "B": "reagent_rack",
                                 "C": "dest_block"}
        assert nc.event.is_set()

    def test_abort_clears_confirmed_and_sets_aborted(self):
        nc = NamespaceSplitConfirmation()
        nc.set_response("abort", None)
        assert nc.aborted is True
        assert nc.confirmed is None
        assert nc.event.is_set()


class TestHTMLNamespaceSplitHandlerConfirm:
    """The handler's confirm() blocks until the WS receiver fills the
    NamespaceSplitConfirmation; returns the dict or None on abort/timeout.
    Empty input list short-circuits without touching the queue."""

    def test_empty_payload_returns_empty_dict_no_send(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, NamespaceSplitConfirmation] = {}
        h = HTMLNamespaceSplitHandler(
            send_request=sent.append,
            pending_namespace_splits=pending,
            timeout_seconds=2,
        )
        assert h.confirm([]) == {}
        assert sent == []

    def test_confirm_returns_prefix_to_label_mapping(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, NamespaceSplitConfirmation] = {}
        h = HTMLNamespaceSplitHandler(
            send_request=sent.append,
            pending_namespace_splits=pending,
            timeout_seconds=2,
        )

        def _respond():
            time.sleep(0.05)
            rid = next(iter(pending))
            pending[rid].set_response("confirm", {"A": "rack_a", "B": "rack_b"})

        threading.Thread(target=_respond, daemon=True).start()
        result = h.confirm([
            {"prefix": "A", "wells": ["A1", "A2"],
             "candidates": ["rack_a", "rack_b"], "suggested_label": "rack_a"},
            {"prefix": "B", "wells": ["B1"],
             "candidates": ["rack_a", "rack_b"], "suggested_label": "rack_b"},
        ])
        assert result == {"A": "rack_a", "B": "rack_b"}
        assert len(sent) == 1
        payload = sent[0]
        assert "request_id" in payload

    def test_confirm_returns_none_on_abort(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, NamespaceSplitConfirmation] = {}
        h = HTMLNamespaceSplitHandler(
            send_request=sent.append,
            pending_namespace_splits=pending,
            timeout_seconds=2,
        )

        def _respond():
            time.sleep(0.05)
            rid = next(iter(pending))
            pending[rid].set_response("abort", None)

        threading.Thread(target=_respond, daemon=True).start()
        result = h.confirm([
            {"prefix": "A", "wells": ["A1"], "candidates": ["rack_a"],
             "suggested_label": "rack_a"},
        ])
        assert result is None

    def test_confirm_returns_none_on_timeout(self):
        sent: List[Dict[str, Any]] = []
        pending: Dict[str, NamespaceSplitConfirmation] = {}
        h = HTMLNamespaceSplitHandler(
            send_request=sent.append,
            pending_namespace_splits=pending,
            timeout_seconds=0.05,
        )
        result = h.confirm([
            {"prefix": "A", "wells": ["A1"], "candidates": ["rack_a"],
             "suggested_label": "rack_a"},
        ])
        assert result is None
