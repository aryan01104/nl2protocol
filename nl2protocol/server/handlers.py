"""
HTMLConfirmationHandler — browser-bridged ConfirmationHandler for live mode.

Per ADR-0013 Phase 3c. Implements the same ConfirmationHandler protocol
as `CLIConfirmationHandler`, but routes the gap prompt through the
browser instead of stdin:

  1. Pipeline thread calls `present(gap, suggestion)`.
  2. Handler constructs a panel_request payload + a per-Gap PendingRequest
     record (a threading.Event + a slot for the eventual Resolution).
  3. Handler pushes the panel_request onto the server's outbound channel
     (the WebSocket sender drains and forwards to the browser).
  4. Pipeline thread BLOCKS on `pending.event.wait(timeout)`.
  5. User clicks a button in the browser; browser sends panel_response
     over WebSocket.
  6. Server's WebSocket receiver finds the matching PendingRequest by
     request_id, fills in the Resolution, sets the Event.
  7. Pipeline thread unblocks, returns the Resolution to the orchestrator.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Dict, Optional

from nl2protocol.gap_resolution.types import Gap, Resolution, Suggestion


# Per-Gap timeout. Browser disconnect / inactivity for longer than this
# aborts the pipeline gracefully. Per ADR-0013 default is 5 minutes.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 300


class PendingRequest:
    """Coordination primitive for one in-flight Gap → browser round-trip.

    Pre:    Constructed before the panel_request is sent. The owning
            handler stores it in the shared `pending_requests` dict
            keyed on request_id so the WebSocket receiver can look it
            up when the response arrives. `gap` and `suggestion` are
            carried alongside the Event so the receiver can build the
            full Resolution from just the browser's action token (it
            otherwise wouldn't know the suggestion's value or the
            gap's current value, which are needed for accept/override
            respectively).

    Post:   The pipeline thread calls `event.wait(timeout)`; the
            WebSocket receiver thread (FastAPI event loop) calls
            `set_response_from_action(token)` which fills `response`
            and sets the event. Single-direction handoff — no other
            shared state touches `response`, so no lock needed.
    """

    def __init__(self, gap: Gap, suggestion: Optional[Suggestion]):
        self.gap = gap
        self.suggestion = suggestion
        self.event = threading.Event()
        self.response: Optional[Resolution] = None

    def set_response_from_action(self, action_token: str,
                                  raw_value: Optional[str] = None) -> None:
        """Map a browser action token (`accept` / `edit` / `skip` /
        `override` / `abort`) onto a Resolution and signal the waiting
        pipeline thread.

        Pre:    `action_token` is one of: "accept", "edit", "skip",
                "override", "abort". For "edit", `raw_value` is the
                user-typed string from the modal's input field; it is
                coerced to float when the gap's current value is
                numeric-shaped, otherwise passed through as-is. Unknown
                tokens fall through to a defensive "skip" so the pipeline
                can keep moving.
        Post:   `self.response` is populated with a Resolution whose
                action and user_action_provenance match the token. For
                "edit", `new_value` is the coerced scalar — the
                orchestrator's _apply_at_path mutates `existing.value`
                with it, so the field's Provenanced* shape stays intact.
                `self.event` is set last so the waiting thread sees a
                fully-populated `response`.
        """
        suggestion_value = self.suggestion.value if self.suggestion is not None else None
        if action_token == "accept" and self.suggestion is not None:
            self.response = Resolution(
                action="accept_suggestion",
                new_value=suggestion_value,
                user_action_provenance="user_accepted_suggestion",
            )
        elif action_token == "edit" and raw_value is not None:
            coerced = _coerce_edit_value(self.gap, raw_value)
            self.response = Resolution(
                action="edit",
                new_value=coerced,
                user_action_provenance="user_edited",
            )
        elif action_token == "override":
            # ADR-0012: override is only valid for fabrication gaps. Mirror
            # the CLI handler's defensive "fall through to skip" branch so
            # a stray override token on a non-fabrication gap can't write
            # bogus user_overrode_fabrication provenance.
            if self.gap.kind == "fabricated":
                self.response = Resolution(
                    action="override",
                    new_value=self.gap.current_value,
                    user_action_provenance="user_overrode_fabrication",
                )
            else:
                self.response = Resolution(
                    action="skip",
                    new_value=None,
                    user_action_provenance="user_skipped",
                )
        elif action_token == "abort":
            self.response = Resolution(
                action="abort",
                new_value=None,
                user_action_provenance="user_aborted",
            )
        else:
            # "skip" or unknown — treat as skip so the loop can advance.
            self.response = Resolution(
                action="skip",
                new_value=None,
                user_action_provenance="user_skipped",
            )
        self.event.set()


class AssignmentsConfirmation:
    """Coordination primitive for one in-flight labware-assignments
    confirmation round-trip — analog to PendingRequest, but for the
    full description→label table that pipeline.py used to confirm via
    `_confirm_labware_assignments` over stdin.

    Pre:    Constructed before the labware_assignments_request envelope
            is sent. The owning handler stores it on a slot the WS
            receiver can read; pipeline thread blocks on `event.wait`.
    Post:   `set_response(action, assignments)` populates response slots
            and signals the event. Pipeline thread reads `confirmed`
            (None on abort, dict otherwise) and returns it.
    """

    def __init__(self):
        self.event = threading.Event()
        self.confirmed: Optional[Dict[str, str]] = None
        self.aborted: bool = False

    def set_response(self, action: str,
                      assignments: Optional[Dict[str, str]]) -> None:
        """Map browser response onto the confirmation slots and signal.

        Pre:    `action` is "confirm" or "abort". For "confirm",
                `assignments` is a {description: label} dict the browser
                computed from the modal's current row state.
        Post:   On "abort", `aborted=True` and `confirmed=None`.
                On "confirm", `confirmed=assignments` (may be empty dict
                if there were no assignments to make).
                `event` is set last so the waiter sees fully-populated
                slots.
        """
        if action == "abort":
            self.aborted = True
            self.confirmed = None
        else:
            self.aborted = False
            self.confirmed = assignments or {}
        self.event.set()


class HTMLAssignmentsHandler:
    """Browser-bridged handler for labware-assignments confirmation
    (ADR-0013 Phase 3d). Same blocking pattern as
    HTMLConfirmationHandler, different payload shape.

    Pre:    `send_request` is a callable that pushes a payload dict
            onto the outbound queue (the WS sender drains it).
            `pending_slot` is a single-slot reference the owning
            LiveModeApp uses (only ONE in-flight assignments
            confirmation at a time per pipeline run, unlike per-Gap
            which can have many).
            `timeout_seconds` controls the per-request wait.

    Post:   Each `confirm(table)` call:
              - Generates a request_id (process-local counter).
              - Constructs a payload with the assignments table.
              - Stores an AssignmentsConfirmation on the slot.
              - Sends the payload via the supplied callable.
              - Blocks on the AssignmentsConfirmation's event.
              - On signal: clears the slot and returns the dict
                (None on abort).
              - On timeout: clears the slot and returns None
                (treated as abort — pipeline halts gracefully).
    """

    def __init__(
        self,
        send_request: Callable[[Dict[str, Any]], None],
        pending_assignments: Dict[str, "AssignmentsConfirmation"],
        timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ):
        self._send = send_request
        self._pending = pending_assignments
        self._timeout = timeout_seconds
        self._counter = 0
        self._lock = threading.Lock()

    def _next_request_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"asgn-{self._counter}"

    def confirm(self, table: list) -> Optional[Dict[str, str]]:
        """Send the assignments table to the browser and block until
        the user confirms or aborts.

        Pre:    `table` is a list of dicts, each carrying
                `{description, suggested_label, candidates}` where
                `candidates` is a list of available config labels the
                user can pick from. An empty table means there's
                nothing to confirm — return immediately.
        Post:   Returns dict of {description: confirmed_label} on
                confirm, or None on abort/timeout. Empty dict when the
                input table was empty (no assignments to make).
        """
        if not table:
            return {}
        rid = self._next_request_id()
        confirmation = AssignmentsConfirmation()
        self._pending[rid] = confirmation
        try:
            self._send({
                "request_id": rid,
                "assignments": table,
            })
            signaled = confirmation.event.wait(timeout=self._timeout)
        finally:
            self._pending.pop(rid, None)
        if not signaled:
            return None
        if confirmation.aborted:
            return None
        return confirmation.confirmed


class NamespaceSplitConfirmation:
    """Coordination primitive for one in-flight namespace-split
    confirmation round-trip. Analog of `AssignmentsConfirmation` but
    keyed on letter-prefix groups rather than free-text descriptions.

    Pre:    Constructed before the namespace_split_request envelope is
            sent. Owning handler stores it on a slot the WS receiver
            can read; pipeline thread blocks on `event.wait`.
    Post:   `set_response(action, mappings)` populates `confirmed`
            (a {prefix: chosen_label} dict on confirm, None on abort)
            and signals the event. Pipeline thread reads `confirmed`
            and routes the mappings back into the spec by rewriting
            each affected ref's description to include the prefix
            tag + its resolved label.
    """

    def __init__(self):
        self.event = threading.Event()
        self.confirmed: Optional[Dict[str, str]] = None
        self.aborted: bool = False

    def set_response(self, action: str,
                      mappings: Optional[Dict[str, str]]) -> None:
        """Map browser response onto the confirmation slots and signal.

        Pre:    `action` is "confirm" or "abort". For "confirm",
                `mappings` is a {prefix_letter: chosen_label} dict
                the browser collected from the modal rows.
        Post:   On "abort": `aborted=True`, `confirmed=None`.
                On "confirm": `confirmed=mappings` (may be empty if
                there were no groups to map — the handler short-
                circuits empty payloads earlier).
                `event` is set last so the waiter sees fully-populated
                slots.

        Raises: Never.
        """
        if action == "abort":
            self.aborted = True
            self.confirmed = None
        else:
            self.aborted = False
            self.confirmed = mappings or {}
        self.event.set()


class HTMLNamespaceSplitHandler:
    """Browser-bridged handler for namespace-split confirmation. Same
    blocking pattern as `HTMLAssignmentsHandler`, payload carries the
    detected prefix-group partition + per-group candidate labware
    instead of a free-text assignments table.

    Pre:    `send_request` pushes a payload dict onto the outbound
            queue (WS sender drains). `pending_namespace_splits` is
            the shared dict keyed on request_id (one in-flight at a
            time per pipeline run). `timeout_seconds` controls the
            per-request wait — same default as the other handlers.

    Post:   Each `confirm(payload)` call:
              - Empty payload short-circuits to `{}` (nothing to ask).
              - Otherwise: generates a request_id, stores a
                `NamespaceSplitConfirmation` on the dict, sends the
                payload, blocks on the event.
              - On signal: clears the slot, returns the
                `{prefix: chosen_label}` dict.
              - On abort: returns `None`.
              - On timeout: returns `None` (treated as abort —
                pipeline halts gracefully).
    """

    def __init__(
        self,
        send_request: Callable[[Dict[str, Any]], None],
        pending_namespace_splits: Dict[str, "NamespaceSplitConfirmation"],
        timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ):
        self._send = send_request
        self._pending = pending_namespace_splits
        self._timeout = timeout_seconds
        self._counter = 0
        self._lock = threading.Lock()

    def _next_request_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"nssplit-{self._counter}"

    def confirm(self, payload: list) -> Optional[Dict[str, str]]:
        """Send the namespace-split partition to the browser and block
        until the user confirms or aborts.

        Pre:    `payload` is a list of dicts, each carrying
                `{prefix, wells, candidates, suggested_label}` for one
                detected prefix group. An empty list means no
                namespace-split groups to confirm — return `{}`
                immediately.
        Post:   Returns `{prefix: confirmed_label}` on confirm, or
                `None` on abort/timeout. Empty dict when input was
                empty. The dict's keys are the prefix letters from
                the payload (one per row).
        """
        if not payload:
            return {}
        rid = self._next_request_id()
        confirmation = NamespaceSplitConfirmation()
        self._pending[rid] = confirmation
        try:
            self._send({
                "request_id": rid,
                "namespace_split": payload,
            })
            signaled = confirmation.event.wait(timeout=self._timeout)
        finally:
            self._pending.pop(rid, None)
        if not signaled:
            return None
        if confirmation.aborted:
            return None
        return confirmation.confirmed


class InitialContentsConfirmation:
    """Coordination primitive for one in-flight initial-contents
    batch confirmation. Same shape as AssignmentsConfirmation, but
    confirmed values are floats (volumes in µL) rather than strings
    (config labels).

    Pre:    Constructed before the labware_initial_contents_request
            envelope is sent. Owning handler stores it on a slot the
            WS receiver can read.
    Post:   `set_response(action, volumes)` populates `confirmed`
            (None on abort) and signals the event. `volumes` is a list
            of dicts `{labware, well, volume_ul}` keyed by their
            position in the table; the pipeline applies them to
            spec.initial_contents by matching (labware, well) tuples.
    """

    def __init__(self):
        self.event = threading.Event()
        self.confirmed: Optional[list] = None   # list of {labware, well, volume_ul}
        self.aborted: bool = False

    def set_response(self, action: str,
                      volumes: Optional[list]) -> None:
        """Map browser response onto confirmation slots and signal.

        Pre:    `action` is "confirm" or "abort". For "confirm",
                `volumes` is a list of dicts the browser computed from
                the modal table's row state.
        Post:   On "abort", `aborted=True` and `confirmed=None`.
                On "confirm", `confirmed=volumes` (may be empty list).
                `event` is set last so the waiter sees fully-populated
                slots.
        """
        if action == "abort":
            self.aborted = True
            self.confirmed = None
        else:
            self.aborted = False
            self.confirmed = list(volumes or [])
        self.event.set()


class HTMLInitialContentsHandler:
    """Browser-bridged handler for batched initial-contents volume
    confirmation (ADR-0013 Phase 3f / Group D). Replaces N per-Gap
    modals during gap resolution with one table modal where the user
    accepts or edits all volumes at once.

    Pre:    `send_request` is a callable that pushes the
            initial_contents_request payload onto the outbound queue.
            `pending_initial_contents` is the dict the WS receiver
            writes responses into.

    Post:   Each `confirm(table)` call:
              - Generates a request_id.
              - Stores an InitialContentsConfirmation on the slot.
              - Pushes the payload (rows: labware, well, substance,
                current_volume_ul, suggested_volume_ul).
              - Blocks on the InitialContentsConfirmation's event.
              - Returns the confirmed list (None on abort/timeout).
            Empty input table short-circuits — no modal pop, returns
            empty list immediately.
    """

    def __init__(
        self,
        send_request: Callable[[Dict[str, Any]], None],
        pending_initial_contents: Dict[str, "InitialContentsConfirmation"],
        timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ):
        self._send = send_request
        self._pending = pending_initial_contents
        self._timeout = timeout_seconds
        self._counter = 0
        self._lock = threading.Lock()

    def _next_request_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"ic-{self._counter}"

    def confirm(self, table: list) -> Optional[list]:
        """Send the IC table to the browser and block until the user
        confirms or aborts.

        Pre:    `table` is a list of dicts: each carries
                `{labware, well, substance, current_volume_ul,
                  suggested_volume_ul}`. Empty table → no-op.
        Post:   Returns list of `{labware, well, volume_ul}` on
                confirm, or None on abort/timeout. Empty list when the
                input table was empty.
        """
        if not table:
            return []
        rid = self._next_request_id()
        confirmation = InitialContentsConfirmation()
        self._pending[rid] = confirmation
        try:
            self._send({
                "request_id": rid,
                "rows": table,
            })
            signaled = confirmation.event.wait(timeout=self._timeout)
        finally:
            self._pending.pop(rid, None)
        if not signaled:
            return None
        if confirmation.aborted:
            return None
        return confirmation.confirmed


class BinaryConfirmation:
    """Coordination primitive for one in-flight binary (yes/no)
    confirmation. Shared with HTMLBinaryConfirmHandler the same way
    PendingRequest is shared with HTMLConfirmationHandler.

    Pre:    Constructed before the binary_confirm_request envelope is
            sent. Owning handler stores it on a slot the WS receiver
            can read.
    Post:   `set_response(action)` populates `confirmed` (True for
            "yes", False for "no" or unknown) and signals the event.
    """

    def __init__(self):
        self.event = threading.Event()
        self.confirmed: Optional[bool] = None

    def set_response(self, action: str) -> None:
        """Map browser response onto the boolean slot and signal.

        Pre:    `action` is "yes" or "no". Unknown tokens fall through
                to False so an ambiguous response halts gracefully.
        Post:   `confirmed` is True iff action=="yes"; False otherwise.
                `event` is set last so waiters see a populated slot.
        """
        self.confirmed = (action == "yes")
        self.event.set()


class HTMLBinaryConfirmHandler:
    """Browser-bridged Y/N handler. One handler instance per
    LiveModeApp; each `confirm()` call generates a unique request_id
    and blocks until the browser submits or times out.

    Pre:    `send_request` is a callable that pushes the binary_confirm
            payload onto the outbound queue. `pending_confirms` is the
            dict the WS receiver writes responses into.

    Post:   `confirm(title, items, yes_label, no_label)`:
              - Generates a request_id.
              - Stores a BinaryConfirmation on `pending_confirms[rid]`.
              - Pushes a payload with the title, items list, button
                labels (so each prompt's labels match its CLI analog).
              - Blocks on the BinaryConfirmation's event.
              - Returns True iff the browser sent action="yes". On
                timeout returns False (treated as "no/abort" — pipeline
                halts gracefully).
    """

    def __init__(
        self,
        send_request: Callable[[Dict[str, Any]], None],
        pending_confirms: Dict[str, "BinaryConfirmation"],
        timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ):
        self._send = send_request
        self._pending = pending_confirms
        self._timeout = timeout_seconds
        self._counter = 0
        self._lock = threading.Lock()

    def _next_request_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"binc-{self._counter}"

    def confirm(self, title: str, items: list,
                 yes_label: str = "Yes", no_label: str = "Quit") -> bool:
        """Send a binary_confirm_request to the browser; block on the
        user's choice.

        Pre:    `title` is the modal heading. `items` is a list of
                strings to show as a bullet list (each one rendered
                line-by-line). `yes_label` / `no_label` set the button
                text — defaults match the most common CLI prompt
                ("Yes" / "Quit").
        Post:   Returns True iff the user clicked the yes-action button.
                On timeout, returns False so the pipeline halts (matches
                the CLI behavior where empty input typically aborts).
        """
        rid = self._next_request_id()
        confirmation = BinaryConfirmation()
        self._pending[rid] = confirmation
        try:
            self._send({
                "request_id": rid,
                "title": title,
                "items": list(items),
                "yes_label": yes_label,
                "no_label": no_label,
            })
            signaled = confirmation.event.wait(timeout=self._timeout)
        finally:
            self._pending.pop(rid, None)
        if not signaled:
            return False
        return bool(confirmation.confirmed)


def _coerce_edit_value(gap: Gap, raw: str) -> Any:
    """Coerce a user-typed string from the browser modal into the type
    expected by the gap's field. Numeric-shaped current values (volume,
    duration, temperature) → float; everything else → stripped string.

    Pre:    `raw` is the verbatim text from the modal's input. `gap` is
            the gap whose field is being edited.
    Post:   Returns a scalar suitable for `existing.value = <returned>`
            in the orchestrator. On float-parse failure for a numeric
            field, falls through to the stripped string (orchestrator
            will then raise validation, which set_response_from_action
            doesn't try to recover from — caller can re-prompt).
    """
    raw = raw.strip()
    cur = gap.current_value
    cur_value_attr = getattr(cur, "value", None) if cur is not None else None
    looks_numeric = isinstance(cur_value_attr, (int, float)) or (
        # When current_value is None, infer from field name.
        cur is None and any(tok in gap.field_path.lower()
                            for tok in ("volume", "duration", "temperature",
                                          "celsius", "minutes", "seconds"))
    )
    if looks_numeric:
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


class HTMLConfirmationHandler:
    """ConfirmationHandler implementation for live mode (ADR-0013 Phase 3c).

    Pre:    `send_panel_request` is a callable taking a panel_request
            dict and pushing it onto the outbound channel the WebSocket
            sender drains. `pending_requests` is a shared dict the
            handler writes pending records into; the WebSocket receiver
            reads from it. `timeout_seconds` controls the per-Gap wait.

    Post:   Each `present(gap, suggestion)` call:
              - Generates a unique request_id (process-local counter).
              - Constructs a panel_request payload with gap + suggestion.
              - Stores a PendingRequest at `pending_requests[request_id]`.
              - Sends the panel_request via the supplied callable.
              - Blocks on the PendingRequest's event with timeout.
              - On signal: removes the entry from pending_requests and
                returns the Resolution.
              - On timeout: removes the entry and returns an abort
                Resolution (pipeline halts gracefully).

    Side effects: blocks the calling thread (intentional — the
    orchestrator needs the user's answer before continuing).

    Thread-safety: the request_id counter is incremented under a lock.
    `pending_requests[id]` is set and deleted by this handler thread;
    the WebSocket receiver only reads + signals. The Event itself is
    thread-safe by stdlib guarantee.
    """

    def __init__(
        self,
        send_panel_request: Callable[[Dict[str, Any]], None],
        pending_requests: Dict[str, PendingRequest],
        timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ):
        self._send = send_panel_request
        self._pending = pending_requests
        self._timeout = timeout_seconds
        self._counter = 0
        self._lock = threading.Lock()

    def _next_request_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"req-{self._counter}"

    def present(self, gap: Gap, suggestion: Optional[Suggestion]) -> Resolution:
        rid = self._next_request_id()
        pending = PendingRequest(gap=gap, suggestion=suggestion)
        self._pending[rid] = pending

        try:
            payload = {
                "request_id": rid,
                "gap": _serialize_gap(gap),
                "suggestion": _serialize_suggestion(suggestion),
            }
            self._send(payload)
            signaled = pending.event.wait(timeout=self._timeout)
        finally:
            # Always remove the entry — whether the bridge fired or
            # we timed out — so the dict doesn't accumulate stale records.
            self._pending.pop(rid, None)

        if not signaled:
            # Timeout: treat as user abort so the pipeline halts cleanly
            # (matches "browser disconnect / inactivity" branch in ADR-0013).
            return Resolution(
                action="abort",
                new_value=None,
                user_action_provenance="user_aborted",
            )
        if pending.response is not None:
            return pending.response
        # Defensive: signaled but no response set. Should never happen
        # in normal flow; treat as skip.
        return Resolution(
            action="skip",
            new_value=None,
            user_action_provenance="user_skipped",
        )


def _serialize_gap(gap: Gap) -> Dict[str, Any]:
    """Convert a Gap to a JSON-serializable dict for the panel_request
    payload. The browser-side renderer uses these fields to populate
    the prompt panel."""
    return {
        "id": gap.id,
        "kind": gap.kind,
        "field_path": gap.field_path,
        "step_order": gap.step_order,
        "current_value": _safe_repr(gap.current_value),
        "description": gap.description,
        "severity": gap.severity,
        "metadata": _safe_metadata(gap.metadata or {}),
    }


def _serialize_suggestion(suggestion: Optional[Suggestion]) -> Optional[Dict[str, Any]]:
    """Convert a Suggestion to a JSON-serializable dict. Returns None
    when no suggestion was produced (the user gets the gap with edit/skip
    only — no accept option)."""
    if suggestion is None:
        return None
    return {
        "value_repr": _safe_repr(suggestion.value),
        "provenance_source": suggestion.provenance_source,
        "positive_reasoning": suggestion.positive_reasoning,
        "why_not_in_instruction": suggestion.why_not_in_instruction,
        "confidence": suggestion.confidence,
    }


def _safe_repr(value: Any) -> str:
    """Render a value as a compact display string. Suggestion values
    can be arbitrary Pydantic models (Provenanced*, LocationRef); the
    browser doesn't need the full structure, just a label."""
    if value is None:
        return ""
    try:
        from nl2protocol.models.spec import (
            LocationRef,
            ProvenancedDuration,
            ProvenancedString,
            ProvenancedTemperature,
            ProvenancedVolume,
        )
    except ImportError:
        return repr(value)[:200]
    if isinstance(value, LocationRef):
        loc = value.description or value.resolved_label or "<unknown>"
        well = value.well or (value.wells and value.wells[0]) or value.well_range or ""
        return f"{loc} ({well})" if well else loc
    if isinstance(value, ProvenancedVolume):
        return f"{value.value} {value.unit}"
    if isinstance(value, ProvenancedDuration):
        return f"{value.value} {value.unit}"
    if isinstance(value, ProvenancedTemperature):
        return f"{value.value}°C"
    if isinstance(value, ProvenancedString):
        return value.value
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return repr(value)[:200]


def _safe_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce gap.metadata to JSON-serializable form. Most metadata is
    strings/ints; stringify anything that isn't."""
    out: Dict[str, Any] = {}
    for k, v in metadata.items():
        if v is None or isinstance(v, (str, int, float, bool, list, dict)):
            out[k] = v
        else:
            out[k] = str(v)
    return out
