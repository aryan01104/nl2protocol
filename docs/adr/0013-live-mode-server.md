# ADR-0013: Live mode server — thread-bridge, single-process, browser-cancelable

**Status:** Accepted
**Date:** 2026-05-05

## Context

ADR-0011 sketched live mode at the architecture level — a FastAPI server, a `WebSocketReporter`, an `HTMLConfirmationHandler` — but left four concrete design decisions unpinned. This ADR pins them so Phase 3 implementation has a clean target.

The four open questions:

1. **Async or thread-bridge?** Live mode requires the orchestrator's `handler.present()` to wait for a network round-trip to the browser. Today's orchestrator is fully synchronous. Two paths to support waiting on the network: turn every function in the call chain into `async`, or keep the synchronous orchestrator and bridge the network wait through a thread.
2. **Static asset hosting.** The browser needs to load the HTML page from somewhere. The FastAPI server can serve it directly, or the page can live separately and connect via WebSocket only.
3. **Connection lifecycle.** What happens if the user closes the browser mid-pipeline? Pipeline keeps running silently, aborts immediately, or waits a reconnect window?
4. **Cancelation.** How does the user signal "stop the pipeline" from the browser?

## Decisions

### 1. Thread-bridge over async-all-the-way

`Orchestrator.run()`, `ProtocolAgent.run_pipeline()`, and the rest of the pipeline stay synchronous. When `HTMLConfirmationHandler.present()` needs a user response from the browser, it blocks via a thread-coordination primitive (a `threading.Event` or a blocking `queue.Queue.get()`). The FastAPI server runs on its own event loop in a separate thread; the pipeline runs in a worker thread; the bridge synchronizes them.

**Why**: the cost of async-everywhere is real and the benefit is illusory for our use case. Async's primary win is concurrency at scale — one event loop handling thousands of in-flight requests. We have one user, one pipeline, one connection. The orchestrator's loop is fundamentally serial (per-Gap iteration), so there's no parallelism to unlock. Making every function async would touch ~500-1000 lines (`async def` on every layer up the call chain, every `present()` call site becoming `await`, CLI mode wrapping with `asyncio.run`) for zero runtime benefit.

The thread-bridge confines the change to the new `HTMLConfirmationHandler` (~50-100 lines). The rest of the codebase doesn't know live mode exists.

**Tradeoff**: threads bring shared-state hazards. Mitigated by keeping the bridge a single-direction handoff per Gap — the pipeline thread waits on a queue, the server thread puts the answer on it. No shared mutable state, no locks, no race conditions. The pattern is well-trodden in Python (`concurrent.futures.Future` works similarly).

If we ever need to support many concurrent users, async-everywhere is the right migration. That's not a today problem.

### 2. FastAPI serves the page directly

`GET /` returns the HTML page. `GET /static/...` serves CSS/JS. `WebSocket /events` handles event streaming. One process, one port, all the concerns under one roof.

**Why**: the alternative (page hosted separately, server only handles WebSocket) adds operational complexity — the user has to know two URLs, the page's origin can mismatch the server's, CORS becomes a thing. Single-port serving is the standard pattern for a local-only tool. `nl2protocol serve` starts the server, opens `http://localhost:8000/` in the default browser, done.

**Tradeoff**: the FastAPI app does multiple jobs (static + WebSocket). Tiny app; not a real architectural cost.

### 3. Browser disconnect doesn't kill the pipeline

If the user closes the tab or the WebSocket drops, the pipeline keeps running on the server. The server logs the disconnect, the `WebSocketReporter` becomes a no-op for that connection, and at end-of-run the `HTMLReporter` still writes its static replay file to `output/report_<timestamp>.html`. The user can open that file manually to see what happened.

**Why**: closing a browser shouldn't destroy in-progress work. The static replay is the artifact-of-record either way. If the user reopens before end-of-run, ideally the page picks up the state — but that's reconnect logic which we're deferring; for v1, the page resets and shows whatever events arrive after reconnect. In the common case (no disconnect), this never matters.

**What about pipelines blocked on a confirmation prompt when the browser disconnects?** The bridge times out after a configurable interval (default 5 minutes), the pipeline aborts gracefully, and `HTMLReporter` writes the partial state. The user gets a static report showing the pipeline halted at the prompt.

### 4. Cancelation via UI button + WebSocket message

The browser-side page has a Cancel button (visible whenever the pipeline is running). Clicking it sends a `{"kind": "cancel"}` message over the WebSocket. The server's WebSocket handler signals the pipeline thread via a `threading.Event` that the orchestrator polls between iterations. On the next iteration boundary, the orchestrator returns with `aborted=True`; the pipeline cleans up and `HTMLReporter` writes its static replay.

**Why a button, not just closing the tab**: closing the tab is ambiguous (might be accidental). An explicit Cancel is unambiguous user intent.

**Why poll between iterations, not interrupt mid-iteration**: the orchestrator's iteration is short (seconds at most; bounded by deterministic suggesters and one LLM call). Mid-iteration interruption requires careful unwinding of partial state; iteration-boundary cancelation is clean. The user waits at most one iteration's worth of time after clicking Cancel.

## Architecture overview

```
┌───────────────────────────────────────────────┐
│  $ nl2protocol serve -i ... -c ...            │
└─────────────────┬─────────────────────────────┘
                  │
                  ▼
┌───────────────────────────────────────────────┐
│  Main process                                 │
│  ┌─────────────────────────────────────────┐  │
│  │ FastAPI app (uvicorn event loop)        │  │
│  │  - GET /          → HTML page           │  │
│  │  - GET /static/*  → CSS/JS              │  │
│  │  - WS /events     → event stream + UI   │  │
│  │                                          │  │
│  │  spawns ↓                               │  │
│  └─────────────────────────────────────────┘  │
│                                                │
│  ┌─────────────────────────────────────────┐  │
│  │ Pipeline thread                         │  │
│  │  - ProtocolAgent.run_pipeline()         │  │
│  │  - Orchestrator.run() (sync)            │  │
│  │  - WebSocketReporter.emit() → main loop │  │
│  │  - HTMLConfirmationHandler.present()    │  │
│  │      → threading.Event/queue bridge ↔   │  │
│  └─────────────────────────────────────────┘  │
│                                                │
│  bridge: queue.Queue (per-Gap input)          │
│  cancel: threading.Event (polled)             │
└───────────────────────────────────────────────┘
                  ▲
                  │ WebSocket (browser ↔ server)
                  ▼
┌───────────────────────────────────────────────┐
│  Browser                                       │
│  - JS event consumer renders incrementally    │
│  - User clicks panel buttons                  │
│  - Sends panel_response / cancel              │
└───────────────────────────────────────────────┘
```

## Implementation phases

**Phase 3a — backend network layer** (this PR):
- New module `nl2protocol/server/`
- `WebSocketReporter` (Reporter protocol implementation)
- FastAPI app skeleton (GET /, WS /events)
- `nl2protocol serve` CLI subcommand
- Tests for WebSocketReporter
- Browser-side rendering deferred — page still uses static-replay JS; events stream over WebSocket but the BROWSER doesn't update incrementally yet (visible in DevTools console for verification)

**Phase 3b — browser-side incremental rendering**:
- JS consumer that processes WebSocket events and updates the DOM as they arrive
- Columns fill in as the pipeline progresses
- Resolution arrows draw at gap_resolved time
- Bulk panels appear as their pipeline-stage events fire

**Phase 3c — interactive confirmation**:
- `HTMLConfirmationHandler` with thread-bridge
- Editable lab-state panel (rows are inputs, not just static text)
- Mid-arrow panels for per-Gap decisions
- Cancel button with backend cancelation primitive

**Phase 3d — polish**:
- Reconnect window for browsers that drop and come back
- Auto-open browser when `nl2protocol serve` starts
- Better error messages for connection failures

## Dependencies

`fastapi` and `uvicorn[standard]` are MAIN dependencies (not optional). Live mode is the primary user surface — `nl2protocol serve` is the recommended entry point. Every install gets the server.

CLI mode (`nl2protocol -i ...` writing a static HTML report) stays as a non-interactive fallback for tests, headless runs, and users who prefer the terminal. It uses no FastAPI code paths but the dependency is still installed (small footprint, one-time pip install cost).

`websockets` is bundled with `uvicorn[standard]` so we don't need it separately.

## Tradeoffs and known limitations

**Single-user assumption baked in.** The thread-bridge model assumes ONE pipeline running at a time. Two browser tabs both clicking Run would race for the bridge. Acceptable for a local-only tool; out of scope for any multi-user scenario.

**No persistent server.** Each `nl2protocol serve` invocation starts a fresh server, runs ONE pipeline, and exits when the pipeline completes. Restart between runs. Could be relaxed to a long-lived server later if useful.

**Live mode is the primary way; CLI is the fallback.** `nl2protocol serve` is the recommended entry point and gets the visual surface + interactive prompts. `nl2protocol -i ... --html-report` keeps producing static HTML reports for non-interactive runs (CI, headless, scripting). Same orchestrator and reporter shapes power both — different ConfirmationHandler at the front (CLI vs HTML) and different Reporter implementation (HTMLReporter buffers + writes once vs WebSocketReporter streams).

**Reconnect window deferred.** Phase 3a-3c don't preserve state across WebSocket disconnects. Closing a tab mid-pipeline + reopening loses the in-flight visual; the user gets the static replay file when the pipeline finishes. Fine for v1; reconnect logic is Phase 3d if it matters.

## References

- ADR-0008 — orchestrator architecture (the loop this lights up)
- ADR-0011 — HTML visualization + interactive surface (the visual contract this implements)
- `nl2protocol/reporting.py` — `Reporter` protocol that `WebSocketReporter` implements
- `nl2protocol/gap_resolution/handlers.py` — `ConfirmationHandler` protocol that `HTMLConfirmationHandler` will implement (Phase 3c)
- `nl2protocol/server/` — new module landing in Phase 3a

## Addendum (post-implementation phase note)

The roadmap above split into more sub-phases as it shipped. Recording the actual breakdown so the doc isn't stale:

- **Phase 3a** — backend network (FastAPI app + `WebSocketReporter` + `nl2protocol --serve` CLI flag).
- **Phase 3b-1** — live column rendering (server pre-renders step blocks via `_step_to_render_dict`; browser swaps innerHTML on spec events).
- **Phase 3b-2** — live arrows (server pre-renders cite-marked instruction HTML via `_render_instruction_with_marks`; tracks cumulative `gap_resolved` events and ships the running `_collect_resolution_arrows` set with each one).
- **Phase 3c** — per-Gap interactive prompts (`PendingRequest` primitive + `HTMLConfirmationHandler` + bottom-right modal). CLI parity audit landed alongside (severity literals, kind literals, keyboard shortcuts, edit input with field_path label, reviewer-objection display).
- **Phase 3d** — labware-assignments confirmation (`AssignmentsConfirmation` primitive + `HTMLAssignmentsHandler` + center-screen modal with editable description→config-label dropdowns).
- **Phase 3e** — stand-alone binary Y/N prompts for inferred-source acknowledgement and hardware-error proceed (`BinaryConfirmation` primitive + `HTMLBinaryConfirmHandler` + small Y/N modal).

Three parallel primitives instead of one generic confirmation type: each prompt shape carries different payload + different button semantics, and keeping them parallel made the CLI ↔ browser parity audit tractable (one CLI prompt format per primitive). The thread-bridge mechanism (sync `threading.Event` + per-request dict on `LiveModeApp`, drained by an `asyncio` WebSocket receiver coroutine) is identical across all three.

The original "Phase 3d — polish" slot (reconnect window, auto-open browser, error messages) reshuffled: auto-open browser shipped in 3a; reconnect-with-replay-of-outstanding-requests shipped in 3c; the rest stays under polish/follow-up.

Live mode is now functionally equivalent to CLI mode end-to-end.
