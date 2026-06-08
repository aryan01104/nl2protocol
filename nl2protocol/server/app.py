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
import os
import queue
import shutil
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from nl2protocol.server.handlers import (
    AssignmentsConfirmation,
    BinaryConfirmation,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    HTMLAssignmentsHandler,
    HTMLBinaryConfirmHandler,
    HTMLConfirmationHandler,
    HTMLInitialContentsHandler,
    HTMLNamespaceSplitHandler,
    InitialContentsConfirmation,
    NamespaceSplitConfirmation,
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
                             instruction: str,
                             inline_checks_by_step: Optional[dict] = None) -> dict:
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

    P1-8: `inline_checks_by_step` (when supplied — only by the
    completed_spec branch of the WebSocket sender) is the cached
    per-step dict of passing constraint checks. It threads through to
    `_step_to_render_dict` so each step's detail_lines get the green
    inline tags embedded in the right value cells. None for other spec
    events (extracted/resolved are pre-validation).

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
        inline_by_step = inline_checks_by_step or {}
        step_dicts = [
            _step_to_render_dict(
                s, idx, instruction,
                inline_checks=inline_by_step.get(s.order),
            )
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
    """FastAPI app + per-request pipeline state. One instance per
    `nl2protocol --serve` invocation; many pipeline runs over its lifetime.

    Pre:    `output_dir` is where the static HTMLReporter artifact lands
            at end of each run. `examples_dir` is the directory containing
            example subdirs (each with `instruction.txt` + `config.json`),
            exposed via `GET /examples`. A relative `examples_dir` is
            anchored to the project root so working-directory drift
            (e.g., uvicorn launched from a different cwd, Docker WORKDIR)
            doesn't strand the examples.

    Post:   `self.app` is a FastAPI instance with these routes wired:
              GET /                        → live-mode HTML page (form
                                              for instruction/config/key)
              GET /examples                → list of example names
              GET /examples/{name}/...     → example file content
              POST /start                  → JSON body
                                              {instruction, config, api_key};
                                              kicks off pipeline in a
                                              worker thread (single global
                                              lock: 409-equivalent if a
                                              run is already in flight)
              WS /events                   → streams events from the
                                              pipeline thread with
                                              spec-event payloads
                                              enriched with pre-rendered
                                              step dicts (Phase 3b-1)

            Each POST /start: validates inputs, writes config to a temp
            JSON file (cleaned up on completion), resets per-run state,
            and spawns a pipeline thread. The thread runs
            `ProtocolAgent.run_pipeline` with a CompositeReporter that
            fans events out to BOTH the WebSocketReporter (live stream)
            AND an HTMLReporter (static archive at end-of-run).
    """

    def __init__(self, output_dir: str = "output",
                 examples_dir: str = "test_cases/examples"):
        self.app = FastAPI(title="nl2protocol live mode")
        self._event_queue: "queue.Queue[Any]" = queue.Queue(maxsize=10000)
        self._pipeline_thread: Optional[threading.Thread] = None
        self._output_dir = output_dir
        # Local-dev convenience: when NL2PROTOCOL_LOCAL_DEV is truthy, the
        # POST /start handler falls back to ANTHROPIC_API_KEY from the server's
        # environment when the request omits api_key. Opt-in flag so a
        # deployed instance (Fly, etc.) — where we don't want to silently
        # bill the operator's key — must NOT set this. Read once at startup
        # so the value is fixed for the process's lifetime.
        self._local_dev_mode = (
            os.getenv("NL2PROTOCOL_LOCAL_DEV", "").strip().lower()
            in ("1", "true", "yes")
        )
        # Anchor a relative examples_dir to the project root so cwd drift
        # (Docker WORKDIR, uvicorn from elsewhere) doesn't strand it.
        examples_path = Path(examples_dir)
        if not examples_path.is_absolute():
            project_root = Path(__file__).resolve().parents[2]
            examples_path = project_root / examples_dir
        self._examples_dir = examples_path
        # Eval-mode routing: when the operator points --examples-dir at the
        # evals/ tree, treat every dropdown-picked run as a graded eval
        # case. Each run's artifacts (HTML report, metrics, pipeline state,
        # generated script) land in evals/runs/<case>/run_<ts>/ alongside
        # copies of the case fixtures + a result.json with hand_grade=TODO.
        # The per-case grouping means every run of "01_lotterhos_magbead"
        # — across server restarts, across days, across code versions —
        # sits in one folder ordered by timestamp, so a grader can scan
        # the case's history without juggling batch IDs. git_sha on
        # result.json carries the code-version dimension. Default
        # examples_dir (test_cases/examples) keeps the legacy flat
        # output/ behavior.
        self._eval_mode: bool = (examples_path.name == "evals")
        project_root = Path(__file__).resolve().parents[2]
        self._eval_runs_root: Path = project_root / "evals" / "runs"
        self._html_report_path: Optional[str] = None
        # Per-request inputs (set by POST /start, cleared between runs).
        # _config_path points at a temp file written from the uploaded JSON.
        self._instruction: str = ""
        self._config_path: Optional[str] = None
        self._api_key: str = ""
        # Eval-mode: which dropdown case the browser picked. None when
        # the user uploaded their own files (no case_name flows). Set by
        # POST /start, read by _run_pipeline + the post-finalize hook.
        self._case_name: Optional[str] = None
        self._case_run_dir: Optional[Path] = None
        self._run_started_utc: Optional[str] = None
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
        # Phase 3c: live reference to the running spec object. Captured
        # when an extracted/resolved/completed spec event arrives;
        # mutated in place by the pipeline worker as the gap loop
        # applies resolutions. Used at gap_resolved time to re-render
        # JUST the affected step's render dict, so the browser updates
        # that one step block in place instead of redrawing the column.
        self._live_spec = None
        # P1-8: cache the constraint checker's passed-check records by
        # step + detail_label between events. constraint_check_done
        # arrives BEFORE completed_spec, so by the time the validated-
        # spec column is being rendered we already know which inline
        # green tag to embed in each detail row. Populated in the
        # constraint_check_done branch of `_ws_sender`; consumed when
        # building the completed_spec payload via
        # `_enrich_spec_event_data`.
        self._inline_checks_by_step: dict = {}
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
        # Phase 5 capability matcher: NamespaceSplitDetector emits gaps
        # for "tube rack A1-A6, B1, C1-C7"-style descriptions that span
        # multiple letter-prefix racks. The handler bridges those gaps
        # to a browser modal asking the user to map each prefix to a
        # config labware. Same dict-by-rid pattern as the other handlers.
        self._pending_namespace_splits: Dict[str, NamespaceSplitConfirmation] = {}
        # Per-IP rate limit. Defense in depth on top of BYO-key: even if a
        # visitor brings their own key, we don't want a bot to spam /start
        # 100x and exhaust our Fly machine's CPU. Dict grows with unique
        # IPs — fine for a portfolio demo; LRU-evict if this ever scales.
        self._rate_limit_window_s: int = 3600
        self._rate_limit_max: int = 5
        self._rate_limit_history: Dict[str, deque] = defaultdict(deque)
        # Pre-render the live page once at boot so GET / serves a cached
        # string instead of re-compiling + re-rendering the 3.9k-line
        # template per request (~500ms saved per visitor). Embedded
        # timestamp is frozen to boot time — the WebSocket events that
        # follow carry their own per-event timestamps, so this is
        # display-only metadata. Restart the server to refresh it.
        self._cached_live_page: str = self._render_live_page()
        self._setup_routes()

    @staticmethod
    def _client_ip(request: "Request") -> str:
        """Resolve the real client IP behind Fly's proxy.

        Pre:    `request` is a FastAPI Request. Fly's edge sets
                `Fly-Client-IP`; standard HTTP proxies set
                `X-Forwarded-For` (comma-separated, first entry is origin).
        Post:   Returns the best-available client IP string. Falls back
                to the immediate socket peer if no proxy headers are set
                (local dev). Returns "unknown" if nothing resolves.
        """
        return (
            request.headers.get("fly-client-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "")
            or "unknown"
        )

    def _is_rate_limited(self, client_ip: str) -> bool:
        """Read-only check: is `client_ip` over the per-IP limit right now?
        Purges expired entries as a side effect; does not record the
        current attempt."""
        now = time.monotonic()
        history = self._rate_limit_history[client_ip]
        while history and now - history[0] > self._rate_limit_window_s:
            history.popleft()
        return len(history) >= self._rate_limit_max

    def _record_rate_limit_attempt(self, client_ip: str) -> None:
        """Record a real pipeline-kickoff attempt for `client_ip`. Only
        called after all validation passes and a worker thread is about
        to start, so noise attempts (busy server, bad body) don't burn
        a visitor's quota."""
        self._rate_limit_history[client_ip].append(time.monotonic())

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
            local_dev_mode=self._local_dev_mode,
        )

    def _reset_per_run_state(self) -> None:
        """Drain the event queue + clear per-run accumulators so a fresh
        POST /start starts clean. Confirmation dicts are also cleared
        defensively, though they should already be empty if the prior
        pipeline thread completed normally."""
        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
            except queue.Empty:
                break
        self._instruction_text = ""
        self._gap_resolved_events = []
        self._inline_checks_by_step = {}
        self._live_spec = None
        self._pending_requests.clear()
        self._pending_assignments.clear()
        self._pending_binary_confirms.clear()
        self._pending_initial_contents.clear()
        self._pending_namespace_splits.clear()

    def _list_examples(self) -> list:
        if not self._examples_dir.exists():
            return []
        return sorted([
            d.name for d in self._examples_dir.iterdir()
            if d.is_dir()
            and (d / "instruction.txt").exists()
            and (d / "config.json").exists()
        ])

    def _example_path(self, name: str, filename: str) -> Optional[Path]:
        # Path-traversal guard: name must be a single safe directory component.
        if not name or "/" in name or "\\" in name or ".." in name:
            return None
        path = self._examples_dir / name / filename
        if not path.exists() or not path.is_file():
            return None
        return path

    def _provision_eval_run_dir(self, case_name: str) -> Path:
        """Pick and create the per-run dir for an eval case.

        Pre:    Eval mode is on (`self._eval_mode` True) and `case_name`
                is the dropdown-selected case the browser sent.

        Post:   Returns an existing, writable directory at
                `evals/runs/<case_name>/run_<ts>/`, where `<ts>` is the
                local timestamp captured at provision time
                (YYYYMMDD_HHMMSS). The case dir is created on the first
                run of that case (lazy); the per-run subdir is created
                every call. On the unlikely sub-second collision (the
                global single-pipeline lock makes this effectively
                impossible during normal serve operation, but possible
                if the runs root is mutated externally), a numeric
                suffix `_2`, `_3`, … is appended.

        Side effects: Creates `evals/runs/<case_name>/` (one-time per
        case) and the per-run subdir (every call).

        Raises: OSError on directory creation failure (disk full, perms).
        """
        case_dir = self._eval_runs_root / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = case_dir / f"run_{ts}"
        if not candidate.exists():
            candidate.mkdir()
            return candidate
        n = 2
        while True:
            collision = case_dir / f"run_{ts}_{n}"
            if not collision.exists():
                collision.mkdir()
                return collision
            n += 1

    def _write_eval_run_artifacts(
        self,
        *,
        case_name: str,
        run_dir: Path,
        started_utc: Optional[str],
        run_meter: Any,
        error: Optional[Dict[str, Any]],
    ) -> None:
        """Finalize an eval-mode run: copy fixtures + write result.json.

        Pre:    `run_dir` is the per-case dir from _provision_eval_run_dir
                (already exists). `case_name` matches a directory in
                `self._examples_dir`. `started_utc` is the ISO-8601 start
                timestamp captured at run-kickoff (may be None on degenerate
                control flow). `run_meter` is the RunMeter the metering
                client recorded against during this run. `error` is None
                on success or {"type":..., "message":...} when the pipeline
                raised.

        Post:   Each of `instruction.txt`, `config.json`, `expected.md`,
                `source.md` that exists under the case dir is copied into
                `run_dir`. A `result.json` is written in `run_dir` with:
                  - case, run_dir (relative to project root)
                  - started_utc, finished_utc, duration_s
                  - git_sha (short)
                  - pipeline_returned (bool: ran without exception)
                  - error (None on success, dict on failure)
                  - hand_grade = "TODO"  ← grader fills
                  - notes = ""           ← grader fills
                Matches the on-disk shape produced by evals/run.py so a
                grader walking evals/runs/<batch>/ can't tell visual from
                headless runs apart.

        Side effects: Writes up to 5 files in `run_dir`.

        Raises: Best-effort: logs swallow on copy errors (missing source
                file is normal — only instruction + config are mandatory).
                JSON write may raise OSError on disk failure.
        """
        case_dir = self._examples_dir / case_name
        for fname in ("instruction.txt", "config.json", "expected.md", "source.md"):
            src = case_dir / fname
            if src.exists():
                try:
                    shutil.copy(src, run_dir / fname)
                except OSError:
                    pass
        finished_utc = datetime.now(timezone.utc).isoformat()
        duration_s: Optional[float] = None
        if started_utc:
            try:
                duration_s = (
                    datetime.fromisoformat(finished_utc)
                    - datetime.fromisoformat(started_utc)
                ).total_seconds()
            except ValueError:
                duration_s = None
        result = {
            "case": case_name,
            "run_dir": str(run_dir.relative_to(Path(__file__).resolve().parents[2])),
            "started_utc": started_utc,
            "finished_utc": finished_utc,
            "duration_s": duration_s,
            "git_sha": _git_sha_short(),
            "pipeline_returned": error is None,
            "error": error,
            "hand_grade": "TODO",
            "notes": "",
        }
        (run_dir / "result.json").write_text(json.dumps(result, indent=2))
        # In-flow grading hint. Server stdout reaches the operator's
        # terminal (where they launched `nl2protocol --serve`); putting
        # the path here keeps "what next?" inside their tmux/iTerm view
        # without forcing them to remember the convention.
        rel_run = result["run_dir"]
        print(
            f"\n[eval] Run artifacts: {rel_run}/"
            f"\n[eval] Grade with:    edit evals/{case_name}/actual.md"
            f"\n[eval] Skeleton:      evals/README.md (Per-case skeleton)\n",
            flush=True,
        )

    def _setup_routes(self):
        @self.app.get("/")
        async def serve_index():
            return HTMLResponse(self._cached_live_page)

        @self.app.get("/examples")
        async def list_examples():
            return {"examples": self._list_examples()}

        @self.app.get("/examples/{name}/instruction")
        async def example_instruction(name: str):
            path = self._example_path(name, "instruction.txt")
            if path is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            return PlainTextResponse(path.read_text())

        @self.app.get("/examples/{name}/config")
        async def example_config(name: str):
            path = self._example_path(name, "config.json")
            if path is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            return FileResponse(path, media_type="application/json")

        @self.app.post("/start")
        async def start_pipeline(request: Request):
            if self._pipeline_thread and self._pipeline_thread.is_alive():
                return {"status": "already_running"}

            client_ip = self._client_ip(request)
            if self._is_rate_limited(client_ip):
                return JSONResponse(
                    {
                        "status": "error",
                        "message": (
                            f"rate limit exceeded ({self._rate_limit_max} runs/hour per IP); "
                            "try again later"
                        ),
                    },
                    status_code=429,
                )

            try:
                body = await request.json()
            except Exception:
                return JSONResponse(
                    {"status": "error", "message": "request body must be JSON"},
                    status_code=400,
                )

            instruction = (body.get("instruction") or "").strip()
            config = body.get("config")
            api_key = (body.get("api_key") or "").strip()
            # Eval-mode: browser sends case_name when the user picked from
            # the dropdown. None / missing on uploads. Sanitized to a safe
            # single-component dir name; the path-traversal guard mirrors
            # _example_path's so an attacker can't escape evals/runs/.
            raw_case = body.get("case_name") or ""
            raw_case = raw_case.strip() if isinstance(raw_case, str) else ""
            case_name: Optional[str] = None
            if raw_case and "/" not in raw_case and "\\" not in raw_case and ".." not in raw_case:
                case_name = raw_case

            # Local-dev fallback: when the operator opted in via env flag AND
            # the request didn't carry a key, use the server's env key. Never
            # fires on a Fly deploy because the flag isn't set there.
            if not api_key and self._local_dev_mode:
                api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

            if not instruction:
                return JSONResponse(
                    {"status": "error", "message": "instruction is required"},
                    status_code=400,
                )
            if not isinstance(config, dict) or not config:
                return JSONResponse(
                    {"status": "error", "message": "config must be a non-empty JSON object"},
                    status_code=400,
                )
            if not api_key:
                return JSONResponse(
                    {"status": "error", "message": "api_key is required"},
                    status_code=400,
                )

            # Write config to a temp file so ConfigLoader (path-based) can read it.
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, encoding="utf-8",
            )
            json.dump(config, tmp)
            tmp.close()

            self._instruction = instruction
            self._config_path = tmp.name
            self._api_key = api_key
            self._case_name = case_name
            self._reset_per_run_state()

            # Record the rate-limit attempt now that all checks have passed
            # and we're committed to actually kicking off a real run.
            self._record_rate_limit_attempt(client_ip)

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
                # P1-8: only the validated-spec column surfaces constraint
                # outcomes, so inline_checks_by_step is plumbed in only
                # for the completed_spec event. Other spec snapshots
                # render with no green tags (they're pre-validation).
                inline = (self._inline_checks_by_step
                          if event.kind == "completed_spec" else None)
                # Phase 3c: capture the live spec reference. Both spec
                # events carry the same mutated object — keep the latest
                # pointer so gap_resolved can look up the affected step.
                if isinstance(event.data, dict):
                    s = event.data.get("spec")
                    if s is not None and hasattr(s, "steps"):
                        self._live_spec = s
                data = _enrich_spec_event_data(
                    event.kind, event.data, self._instruction_text,
                    inline_checks_by_step=inline,
                )
            elif event.kind == "constraint_check_done":
                # P1-8: cache passed_checks for the completed_spec event
                # processed downstream; pass the event through unchanged
                # so the browser's updateValidatedSummary handler still
                # receives the same payload it already consumes.
                from nl2protocol.reporting import _passes_to_inline_checks
                passed = (event.data or {}).get("passed_checks") if isinstance(event.data, dict) else None
                if passed:
                    self._inline_checks_by_step = _passes_to_inline_checks(passed)
                data = _safe_data(event.data)
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
                # Phase 3c: re-render JUST the affected step so the
                # browser can update one step block in place (chain
                # appears inline on the affected cell) instead of
                # redrawing the whole column. step_order is 1-based on
                # the event; spec.steps is 0-indexed.
                try:
                    step_order = data.get("step_order")
                    if (step_order is not None
                            and self._live_spec is not None):
                        step_idx = int(step_order) - 1
                        steps = getattr(self._live_spec, "steps", None) or []
                        if 0 <= step_idx < len(steps):
                            from nl2protocol.reporting import (
                                _step_to_render_dict,
                            )
                            inline_for_step = (
                                self._inline_checks_by_step or {}
                            ).get(step_order)
                            data["step_dict"] = _step_to_render_dict(
                                steps[step_idx], step_idx,
                                self._instruction_text,
                                inline_checks=inline_for_step,
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
            elif kind == "namespace_split_response":
                pending_ns = self._pending_namespace_splits.get(rid)
                if pending_ns is None:
                    continue
                pending_ns.set_response(action, msg.get("mappings"))

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

    def _send_namespace_split_request(self, payload: Dict[str, Any]) -> None:
        """Push a namespace_split_request envelope onto the outbound queue."""
        from nl2protocol.reporting import StageEvent
        try:
            self._event_queue.put_nowait(StageEvent(
                kind="namespace_split_request",
                data=payload,
                stage_name="stage_2_5_namespace_split",
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
        """Worker-thread entry point. Reads request-scoped state set by
        POST /start (instruction, config_path, api_key), builds reporters,
        runs the pipeline, finalizes, and cleans up the temp config file."""
        from nl2protocol.for_cli.confirmation import AutoConfirmCM
        from nl2protocol.metering import MeteredClient, RunMeter
        from nl2protocol.pipeline import ProtocolAgent
        from nl2protocol.reporting import HTMLReporter, MetricsReporter

        instruction = self._instruction
        config_path = self._config_path
        api_key = self._api_key
        case_name = self._case_name

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._run_started_utc = datetime.now(timezone.utc).isoformat()
        # Eval mode + a dropdown-selected case: route this run's artifacts
        # into evals/runs/<batch>/<case>__runN/. Other live-mode paths
        # (uploaded files, test_cases/examples, non-eval examples_dir) keep
        # the legacy flat output/ behavior so existing workflows don't
        # silently change destination.
        if self._eval_mode and case_name:
            self._case_run_dir = self._provision_eval_run_dir(case_name)
            effective_output_dir = str(self._case_run_dir)
        else:
            self._case_run_dir = None
            effective_output_dir = self._output_dir
        Path(effective_output_dir).mkdir(parents=True, exist_ok=True)
        self._html_report_path = f"{effective_output_dir}/report_{ts}.html"

        # Per-run meter: shared between MeteredClient (which records on
        # every messages.create) and MetricsReporter (which records
        # per-stage wall-clock + gap counts and writes metrics_{ts}.{json,md}
        # at finalize()). Constructed here, before the agent, so the meter
        # can be installed onto agent.config_loader.client below.
        run_meter = RunMeter()
        ws_reporter = WebSocketReporter(self._event_queue)
        html_reporter = HTMLReporter(self._html_report_path)
        metrics_reporter = MetricsReporter(
            meter=run_meter,
            output_dir=effective_output_dir,
            run_ts=ts,
        )
        # ConsoleReporter restores the terminal banners that the worker
        # thread used to print directly via _log/_stage. After the
        # ADR-0017 Observer-pattern completion, banners flow through the
        # reporter; live mode must include the console reporter to keep
        # the terminal informed during a browser-driven run.
        from nl2protocol.reporting import ConsoleReporter
        console_reporter = ConsoleReporter()
        composite = CompositeReporter(
            ws_reporter, html_reporter, metrics_reporter, console_reporter,
        )

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
        namespace_split_handler = HTMLNamespaceSplitHandler(
            send_request=self._send_namespace_split_request,
            pending_namespace_splits=self._pending_namespace_splits,
            timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        )

        run_error: Optional[Dict[str, Any]] = None
        try:
            agent = ProtocolAgent(
                api_key=api_key,
                config_path=config_path,
                confirmation_manager=AutoConfirmCM(),
                reporter=composite,
                confirmation_handler=confirmation_handler,
                assignments_handler=assignments_handler,
                binary_confirm_handler=binary_confirm_handler,
                initial_contents_handler=initial_contents_handler,
                namespace_split_handler=namespace_split_handler,
            )
            # Wrap the agent's Anthropic client with the metering proxy.
            # Downstream helpers (SemanticExtractor, LabwareMatcher,
            # gap-resolution suggesters, input validator) take their
            # client from agent.config_loader.client — by the transitive
            # property they all see the metered proxy without any
            # call-site changes.
            agent.config_loader.client = MeteredClient(
                agent.config_loader.client, run_meter,
            )
            metrics_reporter.model_name = agent.config_loader.model_name
            agent.run_pipeline(instruction)
        except Exception as e:
            # Surface the error to the browser. The status indicator
            # in handleEvent now sticks once "error" is set, so the
            # PIPELINE_DONE_SENTINEL that follows can't paint over it.
            # run_pipeline's own crash handler saves a rich state log
            # with accumulated spec snapshots before re-raising.
            run_error = {"type": type(e).__name__, "message": str(e)}
            self._event_queue.put_nowait(_make_error_event(
                f"Pipeline error: {e}",
            ))
        finally:
            composite.finalize()
            # Eval mode: copy the case fixtures into the run dir + write a
            # result.json with grading slots set to TODO. Mirrors evals/run.py's
            # per-run artifact bundle so visual + headless eval batches grade
            # the same way. No-op when not in eval mode.
            if self._eval_mode and self._case_run_dir is not None and case_name:
                self._write_eval_run_artifacts(
                    case_name=case_name,
                    run_dir=self._case_run_dir,
                    started_utc=self._run_started_utc,
                    run_meter=run_meter,
                    error=run_error,
                )
            # Clean up the per-request temp config file.
            if config_path and config_path.startswith(tempfile.gettempdir()):
                try:
                    os.unlink(config_path)
                except OSError:
                    pass


def _make_error_event(message: str):
    """Construct a synthetic StageEvent for surfacing errors to the
    browser via the event stream."""
    from nl2protocol.reporting import StageEvent
    return StageEvent(kind="error", data={"message": message})


def _git_sha_short() -> str:
    """Return the short HEAD SHA, or "nogit" if git is unavailable or this
    isn't a repo. Stamped into eval-mode result.json so each run is
    traceable back to a commit."""
    import subprocess
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "nogit"


def run_serve(host: str = "127.0.0.1", port: int = 8000,
              output_dir: str = "output",
              examples_dir: str = "test_cases/examples",
              open_browser: bool = True) -> None:
    """Entry point for `nl2protocol --serve`. Starts uvicorn on the given
    host:port, opens the default browser to the page, blocks until the
    server is killed (Ctrl-C). Instruction + config + api_key come from
    the browser form (POST /start), not from CLI args.

    Pre:    `host`/`port` define where the server listens. `output_dir`
            is where each run's static HTMLReporter artifact is written.
            `examples_dir` is the on-disk directory containing example
            subdirs (each with instruction.txt + config.json), exposed
            via GET /examples. `open_browser` controls auto-open
            (CI/headless contexts should pass False).

    Post:   Blocks until uvicorn exits. Each POST /start kicks off one
            pipeline run on a worker thread; the previous run must be
            done before the next one is accepted (global single-pipeline
            lock).

    Side effects:
      - Starts a uvicorn server (network listener)
      - Optionally opens a browser tab
      - Spawns a pipeline thread per POST /start
      - Writes a static HTML report at the end of each run
      - Writes a per-request temp config file (cleaned up on completion)
    """
    import uvicorn
    import webbrowser
    import threading as _threading
    import time as _time

    live = LiveModeApp(output_dir=output_dir, examples_dir=examples_dir)

    if open_browser:
        # Delay slightly so the server is up before the browser tries
        # to load the page.
        def _delayed_open():
            _time.sleep(0.5)
            webbrowser.open(f"http://{host}:{port}/")
        _threading.Thread(target=_delayed_open, daemon=True).start()

    uvicorn.run(live.app, host=host, port=port, log_level="info")
