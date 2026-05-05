# ADR-0011: HTML visualization and interactive surface — storytelling + confirmation in one canvas

**Status:** Proposed (planning ADR; implementation deferred to dedicated PRs)
**Date:** 2026-05-05

## Context

The CLI is the only surface a user touches today. After ADR-0008 collapsed the six legacy detect/fill/refine/confirm stretches into the orchestrator, the CLI runs cleaner — but a real-protocol smoke (Bradford, May 5 2026) surfaced two things the CLI form factor can't fix:

1. **Sequential prompts for bulk decisions.** Ten initial-contents volume entries hit the user as ten identical prompts in a row. Each one was a real decision (1500uL is a bad default for sample tubes that hold 50uL of buffer), but presenting them sequentially is the wrong shape. The user wants to scan all ten at once, edit the two that are wrong, confirm the rest. Sequential CLI prompting forces them through ten dialogs to do the work of one panel.

2. **Disconnection between prompt and context.** A constraint-violation prompt that says `Cannot resolve source 'reagent_reservoir' to any configured labware` doesn't tell the user WHICH step has the problem (well, the Gap object knows; the CLI doesn't render it). Even fixed (the simple "prepend Step N" patch), the user is still reading text and mentally cross-referencing against a spec they extracted minutes ago. The right surface SHOWS the affected step in context.

A separate concern surfaced during ADR design: the legacy "Inferred source containers — is this correct?" Y/N prompt mixes two different jobs into one modal. The mechanical job (append source-only wells to `spec.initial_contents` so the downstream well-state tracker doesn't false-warn about empty wells) is plumbing the orchestrator should do automatically when its `ConfigLookupSuggester` resolves a missing source. The UX job (let the scientist scan "wells you'll need to pre-fill before pressing Run on the robot") is real, but it's a scan-and-edit surface, not a gate. Conflating the two as a Y/N prompt is the wrong shape for both.

Plus the deferred work from PR3a step 3 — the labware whole-mapping panel that the legacy `_confirm_labware_assignments` still implements — is the same shape as items 1 and 2 above. It's a bulk-defaults panel; the user scans all assignments and overrides any that look wrong. The legacy CLI version exists; the new orchestrator-driven version is the deferred piece.

Two architectural questions the CLI form factor can't answer:

- **Where do interactive prompts physically live in a visual flow?** Inline at the affected step, in a panel, in a sidebar? The CLI sidesteps this by sequencing.
- **How does an audit trail of what just happened co-exist with active confirmation prompts?** The CLI lets these be separate (post-run summary text), but a visual surface has to show both at once and not have them collide.

The HTML report (ADR-0009 step 5) handles part of this — it renders post-run state with provenance details. But it's a static, end-of-run artifact. The user sees the report after every decision is already made via CLI. There's no way to interact with the report; no way to override a decision visually; no way to watch the orchestrator work in real time.

This ADR proposes a single surface that subsumes both: the storytelling layer (what happened) and the interactive layer (decide what should happen). Same DOM, same renderer, fed by either replayed events at end-of-run or live events during a real run.

## Decision

Build one HTML surface with:

1. A **horizontal five-column layout** representing the pipeline's transformation stages, with cross-column **arrows** showing each value's journey through gap resolution.
2. **Three interactive confirmation patterns**, each chosen to match the SHAPE of what the user is being asked:
   - **Bulk-defaults panel** (bottom-right, sequential per pipeline-stage point) for setup-stage decisions where the user scans many items with sensible defaults and overrides any that look wrong.
   - **Per-Gap mid-arrow panel** for orchestrator-loop decisions where each gap is a distinct judgment call — the gap renders as a dotted arrow from extracted to resolved spec with the prompt panel anchored at its midpoint; the arrow solidifies once resolved.
   - **Retry-then-override panel** for correctness errors that warrant retries before user judgment (fabrications), with full reasoning shown if override is exercised.
3. **Live mode and replay mode share a renderer.** Live mode runs a local FastAPI server with WebSocket events; replay mode is the existing `HTMLReporter` writing self-contained HTML.

This ADR is a planning doc. Implementation phases are sketched at the end; each phase will get its own PR.

## The five columns and what they mean

```
[ Instruction ]  →  [ Extracted Spec ]  →  [ Resolved Spec ]  →  [ Validated Spec ]  →  [ Python Script ]
```

| Column | Content | Mutability | When it appears |
|---|---|---|---|
| **1. Instruction** | The user's raw natural-language prompt, with cite spans color-matched to the values they ground in. | Immutable. | At pipeline start. |
| **2. Extracted Spec** | The LLM's first structured output: steps, values, provenance per atom. May have gaps. | Immutable (this is the LLM's commitment). | After Stage 2 extraction. |
| **3. Resolved Spec** | The spec after the orchestrator's gap-resolution loop converges. Every Gap that was resolvable is filled; every Provenance carries `review_status` reflecting reviewer + user actions. | Immutable as a snapshot, but ARROW-annotated to show what changed and how. | After Stage 3 orchestrator. |
| **4. Validated Spec** | The spec after labware resolution (in the orchestrator already, per PR3a) + constraint check. The thing that becomes `CompleteProtocolSpec`. | Immutable snapshot. | After Stage 4 (constraint check pass). |
| **5. Python Script** | The generated Opentrons protocol code. | Immutable; user can copy. | After Stage 6 codegen. |

This split is intentionally not "pipeline stages 1-8 each get a column." Stages 1, 2 collapse into "instruction → extracted." Stages 5, 6 collapse into "validated → script." The five columns are the columns where SOMETHING THE USER CARES ABOUT changes; intermediate stages without user-visible artifacts (input validation, schema-from-spec deterministic mapping) hide between columns.

### Sticky-left considered, deferred

We considered making the instruction column `position: sticky; left: 0;` so it stays visible as the user scrolls right. Rejected for v1 because the cross-column arrows can't bend around a sticky element without breaking their bezier shapes. Tradeoff accepted: when the user scrolls right to read the script column, they lose visual contact with the instruction. They scroll back to re-read. Revisit later if the cite-color-match infrastructure isn't enough to keep cross-column attention coherent.

## Arrows: the storytelling primitive

For every value that traveled from extracted spec (column 2) to resolved spec (column 3), an arrow gets drawn between its origin cell and destination cell. Arrows encode the journey:

| Visual property | What it encodes |
|---|---|
| **Origin** | Source cell in extracted spec (column 2) — the LLM's initial value, or a small ✗ for null/missing fields. |
| **Destination** | Resolved cell in resolved spec (column 3) — the value after orchestrator loop converged. For dotted arrows in flight, the destination is implied (the cell hasn't been committed yet). |
| **Style: solid** | The gap was resolved deterministically (auto-accepted suggester, or user-confirmed in a bulk panel). |
| **Style: dotted** | The gap is in flight — the orchestrator surfaced it for user judgment, the user hasn't decided yet. The mid-arrow panel anchors here. |
| **Color** | Resolution kind: subtle green (auto-accepted by suggester confidence), gray-green (user-confirmed-as-suggested), blue (user-edited), red dashed (reviewer-disagreed-but-user-overrode), dark red dashed (fabrication overridden by user). |
| **Origin marker** | Small ✗ for nulled/missing fields in extracted spec; empty for present-but-changed values (e.g., labware ambiguity resolved). |
| **Destination marker** | The existing ▴ marker from ADR-0009 retained for "non-instruction sourced" final values. |

For deduped Gaps from ADR-0010 (one Gap covering N affected steps), the visual treatment is N arrows sharing a single mid-arrow panel — origins fan out to N destinations from one panel. Same color, same dotted-then-solid lifecycle, but the user sees that one decision propagated to multiple cells.

Validated-spec column (column 4) gets simpler arrows from column 3: solid black for "labware resolved" mappings, red dashed for "constraint clipped." These don't have a dotted-in-flight phase — labware resolution and constraint checks are deterministic single-step transforms, not iterative.

### Iteration count is data, not visual

The orchestrator's iteration count (1, 2, 3...) is available in the `gap_iteration` event stream and can surface as a hover detail or a small badge. It does NOT drive primary visual encoding in v1 — most gaps resolve in iteration 1, so animating per-iteration would mean variable visual density that's mostly empty. Future revisions can encode iteration count if real runs show it materially differs across gaps. Today's design treats iteration count as auditable data, not visual storytelling.

## Three confirmation patterns

The CLI today has six different prompt shapes for what are really three kinds of decisions. The HTML surface compresses to three patterns, chosen by the SHAPE of the underlying decision. Mapping:

| Today's CLI surface | HTML pattern |
|---|---|
| 1. Initial contents volume prompts (per-Gap × N) | **Bulk-defaults panel** (lab-state) |
| 2. "Inferred source containers — is this correct?" (Y/N) | **Bulk-defaults panel** (lab-state) — folded into 1 |
| 3. Labware assignment panel (numbered table) | **Bulk-defaults panel** (labware-mapping) |
| 4. Constraint violation prompts (per-Gap) | **Per-Gap mid-arrow panel** |
| 5. Provenance fabrication blocks (no prompt) | **Retry-then-override panel** |
| 6. General orchestrator prompts (per-Gap) | **Per-Gap mid-arrow panel** |

### Pattern 1: bulk-defaults panel (sequential, bottom-right)

For decisions that are: (a) numerous, (b) each with a reasonable default, (c) bulk-scannable. Setup-stage decisions about "what's already in the lab" before the protocol runs.

**Position**: bottom-right of the viewport. Claims dead space below the column flow without competing with the columns themselves. The eye-flow is left-to-right across the columns, then snaps to bottom-right when a panel needs attention. Subsequent panels at later pipeline-stage points fade in over the previous (only one active panel at a time).

**Two panels, two pipeline-stage points** — NOT one panel per surface-type. The right unification is by WHEN the decision happens in the pipeline, not by WHAT kind of decision it is:

**Lab-state panel** — fades in pre-stage-3.5, after the orchestrator converges on the resolved spec. Combines today's surfaces 1 and 2 into one panel. Each row is a `WellContents` entry; the user can scan, edit, or delete:

```
LAB STATE BEFORE RUN                              [Confirm all] [Cancel]
─────────────────────────────────────────────────────────────────────────
  sample_rack    A1    unknown protein sample    [ 1500 uL ]   ▴ default
  sample_rack    A2    unknown protein sample    [ 1500 uL ]   ▴ default
  sample_rack    D6    BSA stock solution        [ 1500 uL ]   ▴ default
  reservoir      A1    Bradford reagent          [ 15000 uL ]  ▴ default
  reservoir      A2    water                     [ 15000 uL ]  ◆ inferred source
  sample_rack    B3    buffer                    [ 1500 uL ]   ◆ inferred source
─────────────────────────────────────────────────────────────────────────
   ▴ default  — value defaulted by suggester (well capacity)
   ◆ inferred source — derived from the protocol's aspiration pattern;
       you'll need to physically pre-fill these before running.
```

The `◆` marker carries the safety-check semantics that the legacy "Inferred source containers — is this correct?" Y/N prompt was attempting. By showing inferred-source rows alongside other lab-state rows with a distinct marker, the user can audit "I see, water comes from reservoir A2 — yes, my reservoir does have water at A2 in real life." Or "wait, I never put water at A2; I put it at A3" — they edit the cell. Or "this row shouldn't be inferred as a source at all" — they delete the row.

The append-to-`initial_contents` plumbing that the legacy Y/N prompt gated becomes a deterministic side effect of the orchestrator's source resolution: when `ConfigLookupSuggester` writes a `LocationRef`, it also appends a `WellContents` entry tagged as "inferred from aspiration." The bulk panel is the audit + edit surface for that mechanical action, not a separate gate.

**Labware-mapping panel** — fades in after the lab-state panel is confirmed, replacing it on screen. Bottom-right. Each row is a description→config-label assignment:

```
LABWARE ASSIGNMENTS                               [Confirm all] [Cancel]
─────────────────────────────────────────────────────────────────────────
  "tube rack"          →  sample_rack ▾                ✓ reviewer agreed
  "96-well plate"      →  assay_plate ▾                ✓ reviewer agreed
  "the reservoir"      →  reagent_reservoir ▾          ◆ inferred resolver
  "reservoir"          →  reagent_reservoir ▾          ⚠ duplicate mapping
─────────────────────────────────────────────────────────────────────────
```

Same panel widget, different data shape. The user picks alternative labels via dropdown when they want to override; the duplicate-mapping warning ("two descriptions point at the same config labware — is this intentional?") surfaces as a row annotation.

This panel resolves the deferred PR3a step 3 work (the labware whole-mapping panel). The legacy `_confirm_labware_assignments` in `pipeline.py` deletes once this is in place.

**Cross-column row highlighting**: when a panel row is hovered (or the panel is in active "first row" focus), the corresponding cells in the spec columns AND the citation spans in the instruction column get highlighted in the row's color. This extends the existing `data-prov-id` infrastructure from "hover-driven on a value cell" to "hover-driven on a panel row." Mechanically: each panel row carries `data-target-cells` listing the prov-ids it relates to; the JS that already drives cite-active highlighting reads from this attribute.

The bulk panel becomes the "look at this set of decisions in your spec by hovering rows" surface — a tour through the affected spec values, not just a list of inputs to fill.

**Submit semantics**: untouched rows on submit get `review_status="user_confirmed"` stamped on their provenance. Edited rows get `review_status="user_edited"`. Deleted rows lose their `WellContents` / mapping entry from the spec entirely (with that deletion captured in the audit log).

### Pattern 2: per-Gap mid-arrow panel

For decisions that are: (a) one-off judgment calls, (b) tied to a specific affected location, (c) need attention on context. Replaces the CLI's per-Gap prompts.

**Visual**: when an orchestrator iteration produces an unresolved gap, a dotted arrow draws from the affected cell in extracted-spec (column 2) toward where the resolved-spec cell would be (column 3). The destination is implied — the resolved cell hasn't been committed yet. A panel anchors at the midpoint of the dotted arrow, showing the gap's prompt:

```
            ┌─────────────────────────────────────────┐
            │  Step 5 destination wells               │
            │                                          │
            │  ⚠ Wells [A20] don't exist on           │
            │    sample_rack (A1-D6 valid).           │
            │                                          │
            │  Suggested fix: clip to A1-D6.          │
            │                                          │
            │  [Accept] [Edit] [Skip] [Quit]          │
            └─────────────────────────────────────────┘
                          /\
                         /  \
       extracted ─ ─ ─ ─    ─ ─ ─ ─ resolved
                  (dotted arrow, mid-anchored panel)
```

When the user submits, the panel disappears and the dotted arrow solidifies into a coloured solid arrow (color encodes user's action: accept = gray-green, edit = blue). The destination cell appears in the resolved-spec column. The animation IS the resolution: the user watches their decision land.

**Deterministic auto-accepts skip the dotted phase**: the orchestrator's auto-accept path produces a solid arrow from extracted to resolved immediately, no panel involved. The visual distinction between "the orchestrator handled this without me" and "the orchestrator asked me" is encoded in whether the arrow ever passed through a dotted phase.

**Multiple simultaneous in-flight gaps**: multiple dotted arrows can draw at once when re-detect surfaces several at the start of an iteration. Only one mid-arrow panel is active at a time (the orchestrator serializes prompts in topological order). Inactive arrows show as dotted-without-panel until their turn comes; as each is resolved the panel disappears and the arrow solidifies. The visual queue forms naturally without an explicit queue UI.

**Why mid-arrow vs. popover-at-cell**: a popover anchored to the destination cell would imply the cell already exists, but the gap exists precisely because the resolved value hasn't been committed yet. The mid-arrow position puts the prompt at "in transit" — between the source the gap has and the destination it's heading toward. Fits the visual semantic.

### Pattern 3: retry-then-override panel

For correctness errors that warrant a retry pass before user judgment. Today's primary case: provenance fabrications (the LLM cited a substring that doesn't appear in the instruction). May extend to other correctness signals later.

**Two-stage flow**:

1. **Retry pass**: when a fabrication is detected, the orchestrator runs an `LLMSpotSuggester` call with focused context — explicitly telling the model "your previous claim that '[cited_text]' is in the instruction was wrong; here's the exact instruction; either find a real cite or fall through to inferred reasoning." This often resolves the fabrication (the LLM was confused but can correct itself with focused prompting). The retry produces a clean spec; no user surface needed.

2. **Override panel** (only if retries fail): if after retries the value is still flagged as fabricated, surface a panel at the affected cell with full reasoning:

```
   ┌──────────────────────────────────────────────────────┐
   │  Step 3 volume                                        │
   │                                                        │
   │  ⚠ Fabrication detected                                │
   │                                                        │
   │  Value:        50 uL                                   │
   │  Claimed cite: "50uL of sample"                        │
   │  Why flagged:  cited substring not found in your       │
   │                instruction (case-insensitive,          │
   │                whitespace-normalized)                  │
   │                                                        │
   │  Retry result: still fabricated after focused retry.   │
   │                                                        │
   │  Positive reasoning (from retry):                      │
   │    "Bradford working volume per Pierce protocol"       │
   │  Why-not-in-instruction:                               │
   │    "instruction names the protocol but not the volume" │
   │                                                        │
   │  You may know something we don't. If you're confident  │
   │  this value is correct (and your instruction implies   │
   │  it through context we missed), you can override.      │
   │                                                        │
   │  [Override and proceed]  [Edit]  [Quit and re-run]     │
   └──────────────────────────────────────────────────────┘
```

**Override semantics**: when the user clicks [Override and proceed], the spec's Provenance for that value gets stamped with a NEW `review_status` value: `user_overrode_fabrication`. Distinct from `user_edited` because the semantics are different — the user is committing to a value the system flagged as ungrounded. The audit trail records "fabricated → user overrode," and the HTML report renders this with a dark red dashed arrow + warning glyph.

**Why allow override at all** (vs. block screen): the user might know something the verifier doesn't. Synonyms, abbreviations, paraphrases that the LLM normalized could leave a "cited" value that doesn't match the verifier's verbatim-substring check. Pre-PR3b, a "Proceed anyway?" prompt had this affordance but with no audit trail; the user's override produced a spec with no record of it. Post-ADR-0009, the audit trail exists; surfacing the override with full reasoning AND stamping a distinct review status is a much stronger affordance than the legacy Y/N.

**Why retry first**: fabrications are often transient — the LLM was sloppy with a citation in the first pass and can correct itself with focused prompting. Surfacing every fabrication directly to the user as override-or-quit means the user does work the orchestrator could have done. The retry pass removes the sloppy-cite cases; only genuine "the user knows something we don't" cases reach the override surface.

### Pattern 3 implementation note (deferred from ADR-0011 surface)

The retry-then-override flow requires a small expansion of the orchestrator's loop: a new `FabricationRetrySuggester` (or extension of `LLMSpotSuggester`) that fires specifically on fabrication-flagged provenances before the gap surfaces to the user. The `Provenance.review_status` Literal expands to include `user_overrode_fabrication`. This is its own piece of architecture; ADR-0011 just specifies what the visual surface looks like AFTER this orchestrator change lands. Worth its own ADR or addendum to ADR-0008.

## Live mode and replay mode share one renderer

Two ways to consume the surface:

**Live mode**: user runs `nl2protocol serve` (or similar). A local FastAPI server starts. The browser opens to a live page. The pipeline runs server-side; events stream over WebSocket; the page renders incrementally. When a Gap needs user input, the appropriate confirmation pattern appears (bulk panel bottom-right, or mid-arrow panel on a dotted arrow); the user's action sends a message back; the orchestrator unblocks and continues.

**Replay mode**: user runs `nl2protocol -i ... --html-report`. The pipeline runs end-to-end with CLI-mode confirmation handlers (today's setup). At the end, `HTMLReporter` writes a self-contained HTML file with all the stage events buffered. Opening the file gives a "replay" of the run with arrows animating in their original timing, but all confirmation panels are read-only summaries showing what was confirmed.

**Same DOM, same JS, same CSS**. The difference is only in event source: live = WebSocket pump; replay = pre-rendered events embedded as JSON in the HTML.

This dual-mode design has a real benefit beyond polish: replay gives users (and interviewers, advisors) a shareable artifact. "Here's what happened on this protocol" as a single self-contained HTML file. Live mode is the production interaction surface; replay is the audit / portfolio artifact.

## Why one ADR for both layers

The storytelling and interactive surfaces are tightly coupled in three ways:

1. **Same column structure**: panels anchor at specific columns; mid-arrow panels live in the column flow. Two ADRs would have to redundantly specify the layout.

2. **Same renderer**: the column DOM, the arrow overlay, the provenance tooltip, the panel infrastructure — all one HTML/CSS/JS codebase. Splitting the design across ADRs invites two implementations to drift.

3. **Live mode threads through both**: when a confirmation panel is up, the storytelling layer pauses with the affected column "in progress." When the user submits, the storytelling resumes with the resolved arrow drawing in. The interaction between the two layers is the whole point of live mode.

A two-ADR split would force one ADR to reference the other and define interfaces; one ADR keeps the interaction explicit.

## Alternatives considered

**(A) Storytelling-only, leave confirmation in the CLI.** Build the visualization for replay mode; users still confirm via terminal during live runs. Rejected because the smoke-test pain WAS the confirmation form factor, not the absence of a story; fixing replay without fixing live leaves the actual UX problem unsolved.

**(B) Confirmation-only, no storytelling.** Build a fancy confirmation UI but no five-column visualization. Rejected because the value proposition of the project is "transparent provenance-traced protocol generation"; without the storytelling layer, the user can't see WHY the spec ended up the way it did. Confirmation without context is just a form.

**(C) Single-page React/Svelte/Vue app.** Use a real frontend framework. Rejected for now — the existing HTML report is intentionally vanilla-JS for portability and zero-build-step distribution. Once interaction complexity grows past what vanilla-JS can carry cleanly, this revisits. Today's complexity (5 columns, 3 confirmation patterns, ~6 event types) is well within vanilla bounds.

**(D) Native desktop app (Tauri, Electron).** Rejected because the protocol-generation use case is occasional, not daily; users don't want an installed app for this; a browser tab is the right form factor.

**(E) Inline editing in the spec columns instead of bulk panel.** Have the user click any value cell in the spec columns directly. Rejected for setup-stage decisions because there can be 10+ items to confirm at once and they need bulk scannability + a single submit action. Inline editing IS the right pattern for individual cell overrides post-confirm — kept for that secondary use case.

**(F) Y/N modal for source-container confirmation, separate from lab-state panel.** The legacy CLI shape. Rejected because it conflates two jobs (mechanical append-to-initial_contents + UX safety check) and the right design is to do the mechanical part deterministically in the orchestrator and let the UX safety check live as a `◆` row marker in the lab-state panel.

**(G) Block screen for fabrications with no override.** Strong-form "fabrications halt the pipeline." Rejected because the user might know something the verifier doesn't (synonyms, abbreviations, paraphrases that the verbatim-substring check misses). Retry-then-override with full audit trail is a stronger affordance than legacy Y/N AND captures the override in provenance.

## Tradeoffs and known limitations

**Tradeoff 1: instruction column doesn't stick.** User has to scroll back to re-read instruction. Mitigated by the per-citation color match (cite span and value span share a hue); the user's eye can connect across the screen via color even when the columns aren't all visible. Sticky-left would have broken the cross-column arrows; we picked arrows over stickiness.

**Tradeoff 2: live mode requires a local HTTP server.** Users who just want a script generated will not want to run a server. Default behavior stays CLI; live mode is opt-in via `nl2protocol serve` (or a `--live` flag). Invertible later if usage shows live should be the default.

**Tradeoff 3: bulk panel for initial contents still uses 1500uL as the well-capacity default.** The bulk panel makes overrides easy, but the underlying default is wrong for the common case (50uL of sample, not 1500uL). This is a separate fix — the WellCapacitySuggester's confidence calibration should distinguish "stock reagent in a tube" from "sample aliquot in a tube." Surfaced as a known item; doesn't block ADR-0011 implementation.

**Tradeoff 4: replay mode is read-only.** Users can't edit a spec post-generation through the HTML report. They have to re-run with edits to the instruction or config. Live mode IS the editing surface; replay is the audit. Considered a feature: prevents post-hoc tampering with audit artifacts.

**Tradeoff 5: only one mid-arrow panel active at a time.** When multiple dotted arrows are simultaneously in flight, all but one show as dotted-without-panel. The active panel rotates as gaps resolve in topological order. Mostly a non-issue today (orchestrator already serializes prompts), but locks in an assumption: the design doesn't support batch-prompted gaps in parallel. If we ever batch user-side prompts in a future architecture, the per-Gap pattern needs revisiting.

**Tradeoff 6: validated-spec arrow decomposition stays simple in v1.** Arrows from column 3 to column 4 are solid black for resolved or red dashed for clipped. We considered finer-grained classes (per-constraint-type colors, per-resolution-source styles) but deferred; the simple two-class split is the right starting point, and decomposition is additive later.

## Implementation phases (deferred to dedicated PRs)

This ADR doesn't ship code. The following phases each warrant their own PR + commit:

**Phase 1 — Event plumbing (additive, no UI change)**
- Emit new stage events from the orchestrator and pipeline:
  - `gap_iteration_start` / `gap_iteration_end` (bracket events with iteration number)
  - `gap_detected` / `gap_resolved` (per-Gap with resolution kind: auto-accept / user-confirmed / user-edited / reviewer-overridden / fabrication-overridden)
  - `labware_resolution_done` (so column 4 can populate)
  - `constraint_check_done` (so column 4 can show validated state)
  - `panel_request` / `panel_response` (live-mode signals when interactive input is needed)
- The existing `HTMLReporter` ignores them; legacy report still works.
- Tests: capture the event stream in a CapturingReporter; assert events fire in the right order.

**Phase 2 — Five-column replay rendering**
- Extend `HTMLReporter` + the Jinja template to render the five-column layout.
- Implement the SVG arrow primitive (solid + dotted).
- Implement the bulk-defaults panel renderer (lab-state shape + labware-mapping shape).
- Implement the mid-arrow panel renderer.
- All panels render as static read-only summaries in replay mode.

**Phase 3 — Live mode infrastructure**
- New `nl2protocol/server/` module: FastAPI app, WebSocket endpoint streaming pipeline events.
- New `WebSocketReporter` implementation of the Reporter protocol.
- New `HTMLConfirmationHandler` implementation of ConfirmationHandler — sends panel-request over WebSocket, awaits panel-response.
- New `nl2protocol/server/static/` for the live-mode JS that consumes events incrementally.
- New CLI subcommand: `nl2protocol serve [-i …] [-c …]`.

**Phase 4 — Animation and interaction polish**
- Solid-arrow draw-in on resolved
- Dotted-arrow appearance + solidification animation
- Panel slide-in from bottom-right
- Cross-column row highlighting on hover
- Auto-scroll on stage advance
- Keyboard shortcuts (a/e/s/q on active panel, etc.)

**Phase 5 — Closes deferred PR3a step 3 work**
- Bulk-defaults labware-mapping panel becomes live-mode + replay.
- Legacy `_confirm_labware_assignments` in `pipeline.py` deleted; CLIConfirmationHandler routes labware mappings through the same bulk-panel mechanism in live mode, or through per-mapping CLI prompts in CLI mode.
- The orchestrator's source-resolution path appends `WellContents` to `initial_contents` deterministically (replaces the legacy `_infer_source_containers` Y/N gate; the bulk lab-state panel is the audit + edit surface for these now-deterministic appends).

**Phase 6 — Retry-then-override (separate ADR or addendum to ADR-0008)**
- New `FabricationRetrySuggester` that fires on fabrication-flagged provenances before user surface.
- `Provenance.review_status` Literal expands to include `user_overrode_fabrication`.
- The mid-arrow panel renderer learns to render the retry-then-override variant when applicable.

Phases 1, 2 land first as a self-contained replay improvement. Phase 3 is its own substantial PR (network layer + new abstractions). Phases 4, 5, 6 are polish + cleanup + separate orchestrator work.

## What this ADR does NOT specify

- **Animation timing** at CSS-level — picked at implementation time, calibrated against real Bradford runs.
- **Exact column widths and breakpoints** — depends on real content density.
- **Mobile/narrow viewport behavior** — desktop-first; mobile is out of scope.
- **Internationalization** — English only.
- **Accessibility audit** — pass at implementation time; not pre-specified.
- **Authentication / multi-user** — local-only tool; no auth.
- **The retry-then-override orchestrator architecture** — surface defined here; orchestrator change is its own ADR.

## References

- ADR-0008 — unified gap-resolution architecture (the orchestrator this surface visualizes).
- ADR-0009 — provenance schema expansion + initial HTML report rendering (this ADR's foundation).
- ADR-0010 — dedupe constraint-violation Gaps (deduped Gaps render as fan-out from one mid-arrow panel in this surface).
- `nl2protocol/reporting.py` — existing HTML report; this ADR extends it.
- `nl2protocol/reporting_templates/report.html.jinja` — existing template; phase 2 replaces it.
- Smoke test record: Bradford run, 2026-05-05, output at `output/report_20260505_055634.html` — the run that surfaced the confirmation-form-factor problem this ADR addresses.

## Addendum: Phase 2b/4 polish (2026-05-05, post-smoke)

Two bugs surfaced in the first user-visible smoke of the five-column report. Documenting the fixes here as an addendum rather than a new ADR — they're refinements within ADR-0011's surface, not architectural decisions.

### Bug 1: resolution arrows had no origin anchor

**Symptom**: Bradford smoke produced 5 resolution arrows in the embedded JSON (one per auto-resolved water-source via `ConfigLookupSuggester`), but ZERO drew on the page. Visual inspection by user.

**Root cause**: `_step_to_render_dict` skipped null fields entirely when building per-column `detail_lines`. For Bradford's water transfers, the LLM extracted `step.source = None` — the renderer omitted the source line in column 2 (Extracted Spec) entirely. The orchestrator filled the source via `ConfigLookupSuggester`, so column 3 (Resolved Spec) had a populated source cell. Resolution arrows need BOTH endpoint cells with matching `data-prov-id`; column 2's missing cell meant `extractedCol.querySelector(...)` returned null, and the JS bailed before drawing.

A second related issue: `_render_provenanced_value` only emitted `data-prov-id` when the cited_text was recoverable in the instruction (the cite ↔ value pair-link semantic). For non-instruction sources (the orchestrator's auto-fills), no `data-prov-id` was emitted on the value cell — so even when both columns rendered the cell, only column 2 might have the id.

**Fix**:

1. **Action-aware placeholder rendering**: introduced `_ACTION_EXPECTED_FIELDS` mapping each action to the set of tracked fields it expects (e.g. `transfer` expects `{volume, source, destination, substance}`; `pause` expects `{}`). For each tracked field on each step, the renderer now:
   - Emits a value cell when non-null (existing behavior)
   - Emits a `✗ (not extracted)` placeholder cell when null AND the field is in the action's expected set (new — gives arrows their origin)
   - Skips entirely when null AND not expected by the action (avoids cluttering pause/comment/module-command step blocks with irrelevant ✗ rows)
2. **Universal `data-prov-id`**: changed `_render_provenanced_value` to ALWAYS emit `data-prov-id` when `prov_id` is provided, regardless of cite recoverability. The cite ↔ value pair-link semantic now lives in the palette-N class (only added when the cite is recoverable); the JS pair-highlight no-ops gracefully when no matching cite span exists.

After the fix: Bradford's 5 water-source resolutions render as 5 leaf-green arrows from `✗` placeholders in column 2 to filled cells in column 3. The visual story works.

### Bug 2: hover highlight was too subtle

**Symptom**: hovering a panel row produced a thin same-color outline around target cells. User wanted "the highlight around it is of the same color and bright, and the text becomes black or white, whichever is more visually recommended."

**Root cause**: the existing `.prov-active` CSS rule was a generic `outline: 1px solid currentColor`. Same currentColor as the text means a same-hue outline against same-hue text — visible but not popping.

**Fix**: per-palette and per-source `.prov-active` (and `:hover`) rules that fill with the cell's own color and flip text to white or near-black for contrast. Mirrors the existing `cite-flip` pattern that already runs on instruction-column cite spans (e.g., `.cite-marker.cite-atomic-color.palette-3.cite-active { background: var(--palette-3); color: #fff; }`). The value-side now has the same affordance — when you hover or panel-row-target a cell, it FILLS with its color and the text becomes high-contrast.

Per-palette text-contrast pairing (chosen by visual-contrast against each palette hue):

| Palette  | Background hex | Text color |
|----------|----------------|------------|
| `palette-0` (deep red)        | `#c1272d` | white   |
| `palette-1` (burnt orange)    | `#e76f1c` | white   |
| `palette-2` (mustard)         | `#c79100` | dark    |
| `palette-3` (leaf green)      | `#4f8a2b` | white   |
| `palette-4` (teal)            | `#168a8a` | white   |
| `palette-5` (deep blue)       | `#1f5fa6` | white   |
| `palette-6` (indigo)          | `#5d3fa8` | white   |
| `palette-7` (magenta)         | `#b62f7a` | white   |

Plus categorical sources without a palette (`prov-config` purple, `prov-domain_default` gold/dark, `prov-inferred` orange/white, `prov-instruction` red/white as fallback when cite isn't recoverable).

The `.cell-empty` placeholder has its own subdued hover treatment — a dim red outline + light tint — so it still feels like a hover target without the loud color-fill (the cell's content is just `✗`; nothing to color-flip on).
