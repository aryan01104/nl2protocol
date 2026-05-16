"""
LiveModeApp — FastAPI app + per-run state for `nl2protocol serve`.

Per ADR-0013 Phase 3a + 3b-1:
  - 3a: backend network layer. WS /events streams pipeline events to
    the browser; POST /start kicks off the pipeline in a worker thread.
  - 3b-1: GET / renders the existing Jinja template with empty data +
    a `live_mode=True` flag. The page loads with the full 5-column
    structure + bulk-panel containers + SVG arrow overlay (all empty);
    the live-mode JS at the end of the template handles WebSocket
    events and inserts step blocks as they arrive. Server pre-renders
    step dicts via `_step_to_render_dict` and includes them in spec
    event payloads so the JS just sets innerHTML.

3b-2/3 will add dynamic resolution-arrow drawing and bulk-panel
population (today the panels stay at their empty-state placeholders
during the run; the static HTMLReporter still writes the full
artifact at end-of-run for archive).
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from nl2protocol.server.handlers import (
    AssignmentsConfirmation,
    BinaryConfirmation,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    HTMLAssignmentsHandler,
    HTMLBinaryConfirmHandler,
    HTMLConfirmationHandler,
    HTMLInitialContentsHandler,
    InitialContentsConfirmation,
    PendingRequest,
)
from nl2protocol.server.reporter import (
    CompositeReporter,
    PIPELINE_DONE_SENTINEL,
    WebSocketReporter,
)


# DEPRECATED in Phase 3b-1. Kept for backwards-compat only — the
# Phase 3a placeholder page that just logs WebSocket events as a
# styled <pre>. New live-mode renders the full Jinja template via
# `LiveModeApp._render_live_page()` instead. If we ever want to fall
# back to a debugging-friendly raw-event log, this string is still
# servable as `HTMLResponse(_PLACEHOLDER_PAGE_LEGACY)`.
_PLACEHOLDER_PAGE_LEGACY = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>nl2protocol — live mode (Phase 3a)</title>
<style>
  body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; padding: 24px; background: #fdf6e3; color: #1a1a1a; }
  h1 { font-size: 20px; margin: 0 0 8px; }
  .meta { color: #6b6b6b; font-size: 13px; margin-bottom: 16px; }
  .controls { margin-bottom: 16px; }
  button { font: 14px -apple-system, sans-serif; padding: 6px 12px;
           border: 1px solid #c0c0c0; background: white; cursor: pointer;
           border-radius: 4px; }
  button:hover { background: #f0f0f0; }
  button:disabled { opacity: 0.5; cursor: default; }
  #status { display: inline-block; margin-left: 12px; padding: 2px 8px;
            border-radius: 3px; font-size: 12px; }
  #status.ok { background: #d4edda; color: #155724; }
  #status.err { background: #f8d7da; color: #721c24; }
  #status.run { background: #fff3cd; color: #856404; }
  #log { background: #1a1a1a; color: #d4d4d4; padding: 16px;
         border-radius: 4px; font-family: ui-monospace, "SF Mono", Menlo, monospace;
         font-size: 12px; line-height: 1.4; max-height: 70vh;
         overflow-y: auto; white-space: pre-wrap; word-break: break-word; }
  .ev { margin-bottom: 4px; }
  .ev-kind { color: #6cb6ff; font-weight: 600; }
  .ev-stage { color: #b6b6b6; font-style: italic; }
  .ev-data { color: #d4d4d4; }
  .ev-done { color: #4f8a2b; font-weight: 600; }
</style>
</head>
<body>
<h1>nl2protocol — live mode</h1>
<div class="meta">
  Phase 3a placeholder. Browser-side rendering lands in Phase 3b.
  This page connects to <code>/events</code> over WebSocket and logs
  every event the server emits.
</div>
<div class="controls">
  <button id="start-btn">▶ Start pipeline</button>
  <span id="status" class="ok">idle</span>
</div>
<div id="log"></div>
<script>
(function () {
  const log = document.getElementById("log");
  const status = document.getElementById("status");
  const startBtn = document.getElementById("start-btn");
  let ws = null;

  function setStatus(text, cls) {
    status.textContent = text;
    status.className = cls;
  }

  function append(html) {
    const div = document.createElement("div");
    div.className = "ev";
    div.innerHTML = html;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function connectWS() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + window.location.host + "/events";
    ws = new WebSocket(url);
    ws.onopen = () => {
      append('<span class="ev-kind">[ws connected]</span>');
    };
    ws.onmessage = (msg) => {
      try {
        const e = JSON.parse(msg.data);
        if (e.kind === "pipeline_done") {
          append('<span class="ev-done">[pipeline done]</span>');
          setStatus("done", "ok");
          startBtn.disabled = false;
          return;
        }
        const stageBit = e.stage_name ? ' <span class="ev-stage">' + escapeHtml(e.stage_name) + '</span>' : "";
        const dataBit = e.data ? ' <span class="ev-data">' + escapeHtml(JSON.stringify(e.data).slice(0, 200)) + '</span>' : "";
        append('<span class="ev-kind">' + escapeHtml(e.kind) + '</span>' + stageBit + dataBit);
      } catch (err) {
        append('<span class="ev-kind">[parse error]</span> ' + escapeHtml(String(err)));
      }
    };
    ws.onerror = () => { setStatus("ws error", "err"); };
    ws.onclose = () => { append('<span class="ev-kind">[ws closed]</span>'); };
  }

  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    setStatus("starting...", "run");
    try {
      const r = await fetch("/start", { method: "POST" });
      const j = await r.json();
      if (j.status === "started") {
        setStatus("running", "run");
      } else if (j.status === "already_running") {
        setStatus("already running", "run");
      } else {
        setStatus("start failed", "err");
        startBtn.disabled = false;
      }
    } catch (err) {
      setStatus("network error", "err");
      startBtn.disabled = false;
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", connectWS);
  } else {
    connectWS();
  }
})();
</script>
</body>
</html>
"""


def _safe_data(data: Any) -> Any:
    """Coerce event data to JSON-serializable form. ProtocolSpec instances
    don't serialize directly; convert via model_dump when present.

    Pre:    `data` is whatever the event emitted — typically a dict that
            may contain Pydantic models.
    Post:   Returns a dict (or primitive) safe to pass to json.dumps.
            Pydantic models are dumped via `.model_dump()`. Other
            unrecognized types fall through to `str()` representation.
    """
    if data is None:
        return None
    if isinstance(data, dict):
        return {k: _safe_data(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_safe_data(x) for x in data]
    if hasattr(data, "model_dump"):
        try:
            return data.model_dump(mode="json")
        except Exception:
            return str(data)
    if isinstance(data, (str, int, float, bool)):
        return data
    return str(data)


def _enrich_spec_event_data(event_kind: str, raw_data: Any,
                             instruction: str) -> dict:
    """For spec events (extracted/resolved/completed), pre-render the
    step blocks server-side using the existing `_step_to_render_dict`
    helper and include them in the payload, plus the cite-marked
    instruction HTML so column 1's anchors update with each new spec
    snapshot.

    Per ADR-0013 Phase 3b-1: the browser-side JS just inserts the
    pre-rendered HTML strings instead of duplicating the rendering
    logic. Reuses 100% of the existing Python rendering path.

    Per ADR-0013 Phase 3b-2: also emit `instruction_html` (the cite-
    marked version) so step-trigger arrows have anchor spans to point
    from. Built using the SAME `_render_instruction_with_marks` +
    `_collect_arrow_targets` the static HTMLReporter uses, so the live
    column is byte-equivalent to the static archive.

    Pre:    `event_kind` is one of "extracted_spec" / "resolved_spec" /
            "completed_spec". `raw_data` is the event's data dict
            (carries `spec`). `instruction` is the most recent
            raw_instruction text (used by `_render_provenanced_value`
            to decide cite recoverability per cell).

    Post:   Returns a dict suitable for JSON serialization with:
              "steps":             list of step render dicts (dicts shape from
                                   `_step_to_render_dict`)
              "instruction_html":  cite-marked HTML for column 1 (only
                                   when both spec and instruction are
                                   available)
              "spec":              _safe_data(spec)  — keeps the raw
                                   spec for tools that want it
            On any error, falls back to plain `_safe_data(raw_data)`.
    """
    spec = raw_data.get("spec") if isinstance(raw_data, dict) else None
    if spec is None or not hasattr(spec, "steps"):
        return _safe_data(raw_data)
    try:
        from nl2protocol.reporting import (
            _collect_arrow_targets,
            _collect_lab_state_rows,
            _collect_labware_mapping_rows,
            _render_instruction_with_marks,
            _step_to_render_dict,
        )
        step_dicts = [
            _step_to_render_dict(s, idx, instruction)
            for idx, s in enumerate(spec.steps)
        ]
        out: Dict[str, Any] = {
            "steps": step_dicts,
            "spec": _safe_data(spec),
        }
        if instruction:
            arrow_targets = _collect_arrow_targets(spec)
            out["instruction_html"] = _render_instruction_with_marks(
                instruction, arrow_targets,
            )
        # Phase 3b-3 (Group C): bulk panels populate live. The static
        # path's helpers compute the same rows the Jinja template
        # iterates — return them in the same shape so the browser can
        # rebuild the panel tables on each spec snapshot. ProtocolSpec
        # carries initial_contents + prefilled_labware on every event,
        # so all three spec events (extracted/resolved/completed) drive
        # an updated panel; the user sees the lab state evolving as the
        # orchestrator fills in initial-contents volumes.
        out["lab_state_rows"] = _collect_lab_state_rows(spec)
        out["labware_mapping_rows"] = _collect_labware_mapping_rows(spec)
        return out
    except Exception:
        return _safe_data(raw_data)


class LiveModeApp:
    """FastAPI app + per-run pipeline state. One instance per
    `nl2protocol serve` invocation.

    Pre:    `instruction_path` and `config_path` point at readable
            files. `output_dir` is where the static HTMLReporter
            artifact will land at end-of-run.

    Post:   `self.app` is a FastAPI instance with three routes wired:
              GET /        → live-mode HTML page (Phase 3b-1: full
                             5-column template with empty data +
                             live-mode JS that consumes WebSocket
                             events and inserts step blocks as they
                             arrive)
              POST /start  → kicks off pipeline in a worker thread
              WS /events   → streams events from the pipeline thread
                             to the browser via the queue bridge,
                             with spec-event payloads enriched with
                             pre-rendered step dicts (Phase 3b-1)

            The pipeline thread runs `ProtocolAgent.run_pipeline` with
            a CompositeReporter that fans events out to BOTH the
            WebSocketReporter (live stream) AND an HTMLReporter (static
            archive at end-of-run).
    """

    def __init__(self, instruction_path: str, config_path: str,
                 output_dir: str = "output"):
        self.app = FastAPI(title="nl2protocol live mode")
        self._event_queue: "queue.Queue[Any]" = queue.Queue(maxsize=10000)
        self._pipeline_thread: Optional[threading.Thread] = None
        self._instruction_path = instruction_path
        self._config_path = config_path
        self._output_dir = output_dir
        self._html_report_path: Optional[str] = None
        # Phase 3b-1: track the most recent raw_instruction text so spec
        # events can be enriched with pre-rendered step dicts (which need
        # the instruction for cite-recoverability decisions).
        self._instruction_text: str = ""
        # Phase 3b-2: accumulate gap_resolved events so we can attach the
        # cumulative resolution-arrow set to each gap_resolved event the
        # browser receives. Browser replaces the embedded arrows JSON +
        # re-renders the SVG layer on each update. List grows for the
        # whole run; same as the static path's _collect_resolution_arrows
        # walking the captured event list.
        self._gap_resolved_events: list = []
        # Phase 3c: shared dict between the worker thread (writes when
        # HTMLConfirmationHandler.present is called) and the WebSocket
        # receiver coroutine (reads + signals when panel_response arrives).
        # Keyed by request_id; values are PendingRequest records. Both
        # readers and writers operate on distinct keys so no lock is
        # needed beyond Python's dict-level atomicity.
        self._pending_requests: Dict[str, PendingRequest] = {}
        # Phase 3d: same pattern for labware-assignments confirmation —
        # one in-flight request at a time, but a dict keeps the API
        # symmetric with `_pending_requests` and lets the receiver use
        # the same lookup-by-rid pattern.
        self._pending_assignments: Dict[str, AssignmentsConfirmation] = {}
        # Phase 3e: stand-alone Y/N prompts (source containers,
        # hardware-error proceed). Same dict-by-rid pattern.
        self._pending_binary_confirms: Dict[str, BinaryConfirmation] = {}
        # Phase 3f: batched initial-contents volume confirmation
        # (replaces N per-Gap modals during gap resolution). Same
        # dict-by-rid pattern.
        self._pending_initial_contents: Dict[str, InitialContentsConfirmation] = {}
        self._setup_routes()

    def _render_live_page(self) -> str:
        """Render the existing Jinja template with empty data + live_mode
        flag. The page loads with the 5-column skeleton, all CSS,
        and the live-mode JS that consumes WebSocket events.

        Per ADR-0013 Phase 3b-1: same template Phase 2's static path
        uses; the only difference is `live_mode=True` toggles in extra
        JS at the end. Server-render-once + client-update-as-events-
        arrive is the simplest path that reuses 100% of existing CSS
        and 95% of existing JS (hover, arrows, panel-row hover).
        """
        from datetime import datetime
        from pathlib import Path
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        # Locate the template file.
        template_dir = Path(__file__).parent.parent / "reporting_templates"
        env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=select_autoescape(["html"]),
        )
        template = env.get_template("report.html.jinja")

        return template.render(
            instruction="",
            instruction_html="<em style='color:#6b6b6b'>(loading — pipeline starts when you click ▶ Start)</em>",
            spec_steps=[],
            resolved_steps=[],
            validated_steps=[],
            generated_script="",
            success=False,
            prov_stats={"total": 0, "non_instr": 0, "non_instr_pct": 0.0},
            resolution_arrows_json="[]",
            lab_state_rows=[],
            labware_mapping_rows=[],
            constraint_summary=None,
            script_step_line_map={},
            generated_script_html="",
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            live_mode=True,
        )

    def _setup_routes(self):
        @self.app.get("/")
        async def serve_index():
            return HTMLResponse(self._render_live_page())

        @self.app.post("/start")
        async def start_pipeline():
            if self._pipeline_thread and self._pipeline_thread.is_alive():
                return {"status": "already_running"}
            self._pipeline_thread = threading.Thread(
                target=self._run_pipeline, daemon=True,
            )
            self._pipeline_thread.start()
            return {
                "status": "started",
                "html_report_path": self._html_report_path,
            }

        @self.app.websocket("/events")
        async def stream_events(websocket: WebSocket):
            await websocket.accept()
            # Re-deliver any panel_requests that were outstanding when
            # the previous WS dropped. New tab / reconnect should never
            # silently strand a gap.
            self._replay_pending_requests()
            sender = asyncio.create_task(self._ws_sender(websocket))
            receiver = asyncio.create_task(self._ws_receiver(websocket))
            try:
                # When EITHER side finishes (pipeline_done from sender, or
                # WebSocketDisconnect from receiver), tear down the other.
                done, pending = await asyncio.wait(
                    {sender, receiver},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
            except WebSocketDisconnect:
                # Browser closed the tab. Per ADR-0013, the pipeline
                # keeps running; the static HTMLReporter will still
                # write its artifact at end-of-run.
                sender.cancel()
                receiver.cancel()

    async def _ws_sender(self, websocket: WebSocket) -> None:
        """Drain the event queue, forward each item to the browser as JSON.

        Pre:    `websocket` is an accepted WebSocket. The pipeline thread
                pushes StageEvents (and the PIPELINE_DONE_SENTINEL) onto
                `self._event_queue`. Panel-request envelopes arrive as
                StageEvents with kind="panel_request" — same code path,
                same enrichment skip.
        Post:   Sends each event as `{kind, data, stage_name, timestamp}`
                JSON until the sentinel arrives, at which point sends
                `{kind: "pipeline_done"}` and returns. Spec events are
                enriched with pre-rendered step dicts via
                `_enrich_spec_event_data` so the browser doesn't duplicate
                rendering logic.
        """
        loop = asyncio.get_event_loop()
        while True:
            event = await loop.run_in_executor(None, self._event_queue.get)
            if event is PIPELINE_DONE_SENTINEL:
                await websocket.send_json({"kind": "pipeline_done"})
                return
            if event.kind == "raw_instruction":
                self._instruction_text = (
                    event.data.get("instruction", "")
                    if isinstance(event.data, dict) else ""
                )
                data = _safe_data(event.data)
            elif event.kind in ("extracted_spec", "resolved_spec",
                                "completed_spec"):
                data = _enrich_spec_event_data(
                    event.kind, event.data, self._instruction_text,
                )
            elif event.kind == "gap_resolved":
                # Phase 3b-2: track for cumulative arrow set.
                self._gap_resolved_events.append(event)
                data = _safe_data(event.data) or {}
                if not isinstance(data, dict):
                    data = {"_raw": data}
                try:
                    from nl2protocol.reporting import _collect_resolution_arrows
                    data["resolution_arrows"] = _collect_resolution_arrows(
                        self._gap_resolved_events,
                    )
                except Exception:
                    pass
            elif event.kind == "generated_script":
                # Phase 3i (#72): pre-render the script-with-step-tags
                # HTML so the browser can drop it into col 5 directly,
                # same shape the static path produces.
                data = _safe_data(event.data) or {}
                if not isinstance(data, dict):
                    data = {"_raw": data}
                try:
                    from nl2protocol.reporting import _render_script_with_step_tags
                    raw_script = data.get("script", "") if isinstance(data, dict) else ""
                    raw_map = data.get("step_line_map", {}) if isinstance(data, dict) else {}
                    data["script_html"] = _render_script_with_step_tags(
                        raw_script, raw_map,
                    )
                except Exception:
                    pass
            else:
                data = _safe_data(event.data)
            payload = {
                "kind": event.kind,
                "data": data,
                "stage_name": event.stage_name,
                "timestamp": event.timestamp.isoformat(),
            }
            await websocket.send_json(payload)

    async def _ws_receiver(self, websocket: WebSocket) -> None:
        """Read panel_response messages from the browser and signal the
        matching PendingRequest so the pipeline thread unblocks.

        Pre:    `websocket` is accepted. Browser sends JSON of shape
                `{"kind": "panel_response", "request_id": "<rid>",
                  "action": "accept"|"edit"|"skip"|"override"|"abort",
                  "new_value": "<raw text>"  # only for action="edit"
                 }`.
        Post:   Each well-formed panel_response with a known request_id
                triggers `PendingRequest.set_response_from_action`, which
                builds the Resolution and signals the pipeline thread.
                Unknown request_ids and malformed payloads are silently
                dropped. Any unexpected receive error closes the
                receiver cleanly (pipeline keeps running; on browser
                reconnect, outstanding pending requests are re-emitted).
        """
        while True:
            try:
                msg = await websocket.receive_json()
            except (WebSocketDisconnect, RuntimeError):
                return
            except Exception:
                # Defensive: malformed frame, JSON decode error, etc.
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("kind")
            rid = msg.get("request_id")
            action = msg.get("action")
            if not rid or not action:
                continue
            if kind == "panel_response":
                pending = self._pending_requests.get(rid)
                if pending is None:
                    continue
                pending.set_response_from_action(action, msg.get("new_value"))
            elif kind == "labware_assignments_response":
                pending_asgn = self._pending_assignments.get(rid)
                if pending_asgn is None:
                    continue
                pending_asgn.set_response(action, msg.get("assignments"))
            elif kind == "binary_confirm_response":
                pending_bc = self._pending_binary_confirms.get(rid)
                if pending_bc is None:
                    continue
                pending_bc.set_response(action)
            elif kind == "initial_contents_response":
                pending_ic = self._pending_initial_contents.get(rid)
                if pending_ic is None:
                    continue
                pending_ic.set_response(action, msg.get("volumes"))

    def _replay_pending_requests(self) -> None:
        """When the browser reconnects mid-run, re-emit a panel_request
        envelope for every outstanding PendingRequest. The pipeline's
        original push to the queue was drained by the now-dead WS, so
        without re-emission the user would see a blank report and the
        pipeline would block forever on a gap.

        Pre:    Called from the WS coroutine right after `accept()`.
                `self._pending_requests` may be empty (clean reconnect)
                or carry one+ in-flight gaps.
        Post:   For each pending entry, pushes a panel_request StageEvent
                onto the outbound queue. The new sender drains naturally.
                Request IDs are preserved so the receiver still maps
                responses correctly.
        """
        from nl2protocol.server.handlers import _serialize_gap, _serialize_suggestion
        for rid, pending in list(self._pending_requests.items()):
            self._send_panel_request({
                "request_id": rid,
                "gap": _serialize_gap(pending.gap),
                "suggestion": _serialize_suggestion(pending.suggestion),
            })

    def _send_initial_contents_request(self, payload: Dict[str, Any]) -> None:
        """Push an initial_contents_request envelope onto the outbound queue."""
        from nl2protocol.reporting import StageEvent
        try:
            self._event_queue.put_nowait(StageEvent(
                kind="initial_contents_request",
                data=payload,
                stage_name="stage_2_5_initial_contents",
            ))
        except queue.Full:
            pass

    def _send_binary_confirm_request(self, payload: Dict[str, Any]) -> None:
        """Push a binary_confirm_request envelope onto the outbound queue."""
        from nl2protocol.reporting import StageEvent
        try:
            self._event_queue.put_nowait(StageEvent(
                kind="binary_confirm_request",
                data=payload,
                stage_name="binary_confirm",
            ))
        except queue.Full:
            pass

    def _send_assignments_request(self, payload: Dict[str, Any]) -> None:
        """Push a labware_assignments_request envelope onto the outbound
        queue. Wrapped as a synthetic StageEvent the existing WS sender
        path handles transparently."""
        from nl2protocol.reporting import StageEvent
        try:
            self._event_queue.put_nowait(StageEvent(
                kind="labware_assignments_request",
                data=payload,
                stage_name="stage_3.5_assignments",
            ))
        except queue.Full:
            pass

    def _send_panel_request(self, payload: Dict[str, Any]) -> None:
        """Push a panel_request envelope onto the outbound event queue.

        Wrapped as a synthetic StageEvent (kind="panel_request") so the
        existing WS sender path handles it without special-casing.
        Called by HTMLConfirmationHandler from the pipeline thread; the
        queue is thread-safe.
        """
        from nl2protocol.reporting import StageEvent
        try:
            self._event_queue.put_nowait(StageEvent(
                kind="panel_request",
                data=payload,
                stage_name="gap_resolver",
            ))
        except queue.Full:
            # Drop silently — same defensive policy as WebSocketReporter.
            # The handler will time out and abort gracefully.
            pass

    def _run_pipeline(self):
        """Worker-thread entry point. Reads instruction, builds reporters,
        runs the pipeline, finalizes."""
        from nl2protocol.confirmation import AutoConfirmCM
        from nl2protocol.pipeline import ProtocolAgent
        from nl2protocol.reporting import HTMLReporter

        try:
            with open(self._instruction_path) as f:
                instruction = f.read()
        except Exception as e:
            self._event_queue.put_nowait(_make_error_event(
                f"Failed to read instruction file: {e}",
            ))
            self._event_queue.put_nowait(PIPELINE_DONE_SENTINEL)
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        Path(self._output_dir).mkdir(parents=True, exist_ok=True)
        self._html_report_path = f"{self._output_dir}/report_{ts}.html"

        ws_reporter = WebSocketReporter(self._event_queue)
        html_reporter = HTMLReporter(self._html_report_path)
        composite = CompositeReporter(ws_reporter, html_reporter)

        # Phase 3c: gap-resolver prompts route through the browser.
        # Phase 3d: labware-assignments confirmation also routes through
        # the browser. Source-container Y/n and constraint-error proceed
        # still auto-accept via AutoConfirmCM — wiring those through the
        # browser is a follow-up.
        confirmation_handler = HTMLConfirmationHandler(
            send_panel_request=self._send_panel_request,
            pending_requests=self._pending_requests,
            timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        assignments_handler = HTMLAssignmentsHandler(
            send_request=self._send_assignments_request,
            pending_assignments=self._pending_assignments,
            timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        binary_confirm_handler = HTMLBinaryConfirmHandler(
            send_request=self._send_binary_confirm_request,
            pending_confirms=self._pending_binary_confirms,
            timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )
        initial_contents_handler = HTMLInitialContentsHandler(
            send_request=self._send_initial_contents_request,
            pending_initial_contents=self._pending_initial_contents,
            timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )

        try:
            agent = ProtocolAgent(
                config_path=self._config_path,
                confirmation_manager=AutoConfirmCM(),
                reporter=composite,
                confirmation_handler=confirmation_handler,
                assignments_handler=assignments_handler,
                binary_confirm_handler=binary_confirm_handler,
                initial_contents_handler=initial_contents_handler,
            )
            agent.run_pipeline(instruction)
        except Exception as e:
            # Surface the error to the browser. The status indicator
            # in handleEvent now sticks once "error" is set, so the
            # PIPELINE_DONE_SENTINEL that follows can't paint over it.
            # run_pipeline's own crash handler saves a rich state log
            # with accumulated spec snapshots before re-raising.
            self._event_queue.put_nowait(_make_error_event(
                f"Pipeline error: {e}",
            ))
        finally:
            composite.finalize()


def _make_error_event(message: str):
    """Construct a synthetic StageEvent for surfacing errors to the
    browser via the event stream."""
    from nl2protocol.reporting import StageEvent
    return StageEvent(kind="error", data={"message": message})


def run_serve(instruction_path: str, config_path: str,
              host: str = "127.0.0.1", port: int = 8000,
              output_dir: str = "output", open_browser: bool = True) -> None:
    """Entry point for `nl2protocol serve`. Starts uvicorn on the given
    host:port, opens the default browser to the page, blocks until the
    server is killed (Ctrl-C).

    Pre:    `instruction_path` and `config_path` point at existing files.
            `host`/`port` define where the server listens. `open_browser`
            controls whether the browser auto-opens (CI/headless contexts
            should pass False).

    Post:   Blocks until uvicorn exits. The pipeline runs server-side
            in a worker thread spawned via POST /start; the worker writes
            its static HTMLReporter artifact to `output_dir` at end-of-run.

    Side effects:
      - Starts a uvicorn server (network listener)
      - Optionally opens a browser tab
      - Spawns a pipeline thread when the user clicks "Start pipeline"
      - Writes a static HTML report at the end of each run
    """
    import uvicorn
    import webbrowser
    import threading as _threading
    import time as _time

    live = LiveModeApp(
        instruction_path=instruction_path,
        config_path=config_path,
        output_dir=output_dir,
    )

    if open_browser:
        # Delay slightly so the server is up before the browser tries
        # to load the page.
        def _delayed_open():
            _time.sleep(0.5)
            webbrowser.open(f"http://{host}:{port}/")
        _threading.Thread(target=_delayed_open, daemon=True).start()

    uvicorn.run(live.app, host=host, port=port, log_level="info")
