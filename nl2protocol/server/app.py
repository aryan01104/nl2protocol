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
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

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
    helper and include them in the payload.

    Per ADR-0013 Phase 3b-1: the browser-side JS just inserts the
    pre-rendered HTML strings instead of duplicating the rendering
    logic. Reuses 100% of the existing Python rendering path.

    Pre:    `event_kind` is one of "extracted_spec" / "resolved_spec" /
            "completed_spec". `raw_data` is the event's data dict
            (carries `spec`). `instruction` is the most recent
            raw_instruction text (used by `_render_provenanced_value`
            to decide cite recoverability per cell).

    Post:   Returns a dict suitable for JSON serialization with:
              "steps":      list of step render dicts (dicts shape from
                            `_step_to_render_dict`)
              "spec":       _safe_data(spec)  — keeps the raw spec for
                            tools that want it
            On any error, falls back to plain `_safe_data(raw_data)`.
    """
    spec = raw_data.get("spec") if isinstance(raw_data, dict) else None
    if spec is None or not hasattr(spec, "steps"):
        return _safe_data(raw_data)
    try:
        from nl2protocol.reporting import _step_to_render_dict
        step_dicts = [
            _step_to_render_dict(s, idx, instruction)
            for idx, s in enumerate(spec.steps)
        ]
        return {
            "steps": step_dicts,
            "spec": _safe_data(spec),
        }
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
            loop = asyncio.get_event_loop()
            try:
                while True:
                    # Drain the queue without blocking the event loop.
                    # run_in_executor schedules the blocking get() in
                    # the default thread pool.
                    event = await loop.run_in_executor(None, self._event_queue.get)
                    if event is PIPELINE_DONE_SENTINEL:
                        await websocket.send_json({"kind": "pipeline_done"})
                        break
                    # Phase 3b-1: track instruction + enrich spec events.
                    if event.kind == "raw_instruction":
                        self._instruction_text = (
                            event.data.get("instruction", "") if isinstance(event.data, dict) else ""
                        )
                        data = _safe_data(event.data)
                    elif event.kind in ("extracted_spec", "resolved_spec",
                                          "completed_spec"):
                        data = _enrich_spec_event_data(
                            event.kind, event.data, self._instruction_text,
                        )
                    else:
                        data = _safe_data(event.data)
                    payload = {
                        "kind": event.kind,
                        "data": data,
                        "stage_name": event.stage_name,
                        "timestamp": event.timestamp.isoformat(),
                    }
                    await websocket.send_json(payload)
            except WebSocketDisconnect:
                # Browser closed the tab. Per ADR-0013, the pipeline
                # keeps running; the static HTMLReporter will still
                # write its artifact at end-of-run.
                pass

    def _run_pipeline(self):
        """Worker-thread entry point. Reads instruction, builds reporters,
        runs the pipeline, finalizes."""
        from nl2protocol.pipeline import ProtocolAgent
        from nl2protocol.reporting import HTMLReporter

        try:
            with open(self._instruction_path) as f:
                instruction = f.read()
        except Exception as e:
            # Push an error event onto the queue so the browser sees it.
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

        try:
            agent = ProtocolAgent(
                config_path=self._config_path,
                reporter=composite,
            )
            agent.run_pipeline(instruction)
        except Exception as e:
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
