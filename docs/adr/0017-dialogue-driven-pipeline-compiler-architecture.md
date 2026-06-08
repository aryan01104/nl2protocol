# ADR-0017: nl2protocol is a dialogue-driven pipeline compiler — missing abstractions and the refactor sequence

**Status:** Recognised (diagnosis) / Phased adoption (refactor sequence)
**Date:** 2026-06-08
**Relates to:** [ADR-0016](0016-protocolagent-redistribution-and-deferred-confirmation-abstractions.md) (the immediate ProtocolAgent refactor — Phase 1 of this larger plan), [ADR-0013](0013-live-mode-server.md) (introduced the browser surface that started the CLI/browser fork).

## Context

ADR-0016 captures a tactical refactor of `ProtocolAgent`. While walking through the function-by-function audit that produced it, a deeper diagnosis surfaced: most of the convolutions in `pipeline.py` are not local accidents — they're symptoms of missing architectural abstractions that the codebase has never named.

Examples we have already found:

- **Same well-fit math computed in three places** (`validation/constraints.py:644`, `_shape_mismatch_warnings`, `_shape_mismatch_facts`). Each callsite walks the same set difference because there is no shared "labware query" concept.
- **CLI/browser fork repeated three times in `run_pipeline`** as `if self.X_handler is not None: ... else: ...`. Two ~107-line confirm methods exist for the same logical question (`_confirm_labware_assignments` vs `_confirm_labware_assignments_via_handler`) because there is no "ask the user" interface that both surfaces implement.
- **Build → ask → apply triplet repeated four times** across `_confirm_initial_contents_via_handler`, `_maybe_handle_namespace_split`, `_confirm_labware_assignments`, `_confirm_labware_assignments_via_handler`. Each is a bespoke method because there is no "confirmation step" type that pulls the shared shape out.
- **A 700-line `run_pipeline` method** that ties all stages together inline because there is no "pipeline stage" type. Stage numbers (`[Stage 1/8] ...`) are passed as integer literals to `_emit_stage_started` rather than carried by the stage itself.
- **~7 of 17 `ProtocolAgent` methods read no load-bearing `self.*` state** (per the audit in ADR-0016) but live on the class anyway because Python classes are the codebase's default namespace and there's no separate "domain analysis" / "application orchestration" / "presentation formatting" layering.
- **Shape-mismatch warnings recomputed at apply time** even though the modal already computed them — because the modal's response shape is a bare `{description: label}` dict, not a `Decision` carrying derived context forward.

These are not seven independent problems. They are seven faces of the same root: **the code has concepts but no types for them.**

## Naming the software

The honest category for `nl2protocol` is **dialogue-driven pipeline compiler**.

Like a compiler:

- Input in one form (English instruction) is transformed to output in another (Opentrons Python script).
- The transformation has explicit **stages**: input validation → extraction → labware resolution → confirmation → gap resolution → constraint check → schema build → codegen → simulation.
- It maintains an **intermediate representation** (`ProtocolSpec`) that downstream stages read and refine.
- It performs **semantic analysis** (constraint checking, completeness) and **error recovery** (gap resolution).

The wrinkle that breaks the pure-compiler picture: **some stages pause and ask a human.** Modern compilers don't do this — they fail with diagnostics. `nl2protocol` doesn't fail at these points; it negotiates. When the resolver can't pick between three tube racks, the pipeline blocks the worker thread until the user answers.

That dialogue-driven property puts `nl2protocol` in the same architectural family as:

- IDE refactoring tools that prompt for ambiguities mid-transform
- Interactive theorem provers (tactics blocked on user input)
- Schema migration wizards
- Modern LLM-driven coding agents with human-in-the-loop checkpoints

The category matters because **the established architecture for compilers is decades old and well-documented**, and the "interactive stage" wrinkle has a known solution: **continuations or request/response stages**. A stage runs until it needs an answer, emits a typed `Request`, gets a typed `Response`, resumes. The harness around it (CLI / browser / scripted / replay) decides *how* to deliver the request. `nl2protocol` does this informally today via handler slots; doing it formally is the architectural arc this ADR records.

## The root cause

`nl2protocol` grew organically without an architectural refactor pass. Each new requirement was added in-place on top of the existing structure:

- v1: instruction → LLM → script. One file, one function.
- v2: add validation. A `validate(spec)` call.
- v3 ([ADR-0008](0008-unified-gap-resolution.md)): add gap resolution → required pausing for user input. A handler was added.
- v4 ([ADR-0013](0013-live-mode-server.md)): add browser/live mode → required parallel handler types. Now there's a CLI handler *and* a browser handler, plus an `if handler is not None` fork.
- v5: namespace splits. Another method. Another fork.
- v6: shape warnings. Another method. Another duplication of the well-fit check.
- v7 ([ADR-0014](0014-revision-history-and-apply-path-overhaul.md)): provenance tracking. Stamps inserted at apply time. Another recomputation of warnings already shown.

Each step was a local change that shipped a feature. None of them stopped to introduce a type for the new concept. That is the normal way codebases grow — until they cross a complexity threshold where the in-place patches start producing the convolutions above.

**This is not a moral failure.** It is the point in a codebase's lifecycle where you owe yourself a refactor pass.

## The seven missing abstractions

Each row maps one missing abstraction to the visible damage from its absence and what it would unlock.

| # | Missing abstraction | Visible damage today | What it unlocks |
|---|---|---|---|
| 1 | **`Stage` type** — each pipeline phase as an object with `name`, `run(ctx) → Result`, declared events, declared preconditions | 700-line `run_pipeline`; stage numbers passed as integer literals; no way to reorder/skip/instrument a stage | Pipeline = `[Stage, Stage, ...]`. Stages testable in isolation. Numbering owned by the stage, not the orchestrator. |
| 2 | **`Context` type** — `{spec, config, instruction, extractor, reporter, ...}` carried explicitly through stages | Helpers that touch one config field have to be methods on `ProtocolAgent`; the "this isn't a model, it's analysis" critique has no clean home for pure functions | Free-function helpers everywhere. The `models/` vs `analysis/` layering becomes obvious because nothing needs to live on a class for namespacing. |
| 3 | **`ConfirmationRequest` / `ConfirmationResponse`** — typed request the stage emits when it needs a user decision; typed response carrying the decision + context | CLI/browser fork in `run_pipeline` (3× `if handler is not None`); 107-line near-duplicate confirm methods | Stages emit requests; harness routes them. CLI = one router, browser = another. Tee/record/replay strategies become composable. |
| 4 | **`Decision` record** — when the user picks, you store `{label, warnings_shown, alternatives_offered, timestamp}`, not just the label | Shape-mismatch math recomputed at apply time even though the modal already computed it. Audit trail loses the "user was warned" signal unless reconstructed. | Modal output becomes load-bearing context. No recomputation. Audit trail is a fact, not an inference. |
| 5 | **Detector / Suggester / Applier triplet exposed for labware** — `gap_resolution/` already has this pattern but labware-assignments doesn't use it, even though structurally identical | Two parallel gap-resolution paths today: the orchestrator's, and `ProtocolAgent`'s bespoke labware-confirm flow. They look different but are the same shape. | Labware confirmation collapses into the existing orchestrator. The four confirm-flow methods become one. |
| 6 | **Domain / Application / Presentation layering** — standard three-layer split | Labware geometry (domain), candidate iteration (application), `⚠` emoji formatting (presentation) all live in one method on `ProtocolAgent`. `models/` mixes types and analysis. The "this isn't a model" critique. | `models/` holds types only. Analysis lives in `analysis/` (or `domain/`). Orchestration in `app/`. Formatting in `presentation/`. Each layer testable independently. |
| 7 | **Null-object handlers** — `CLIHandler` and `BrowserHandler` both implement the same interface; construction picks one | Three `if X_handler is not None` branches in `run_pipeline` | Orchestrator stops asking "which mode am I in?" The mode is decided once, at construction. |

## Why these are a set, not independent

★ **These seven abstractions form a coherent vocabulary, not seven independent improvements.** You don't add them one at a time without coordination — half-adopted vocabulary is worse than no vocabulary, because you end up with two parallel patterns that disagree (e.g. some stages use `Context`, some still use `self`; some confirmations use `Request/Response`, some still use the handler-or-CLI fork).

The honest order for adopting them:

1. **`Context` first.** It has no behavior; it's just a dataclass. Adding it costs little and immediately starts unblocking pure-function extractions. Existing methods can be migrated one at a time to take `ctx` instead of using `self`.
2. **`ConfirmationRequest` / `ConfirmationResponse` second.** This is the "ConfirmationStrategy" from ADR-0016 Phase 2, but now with typed payloads. It eliminates the CLI/browser fork. Wait for the trigger described in ADR-0016 (third surface, drift bell, or explicit tee-debugging need).
3. **`Stage` third.** Once Context exists and Confirmation requests are typed, `run_pipeline` can be expressed as a list of `Stage` objects each holding its own number, label, and run method. This is the "ConfirmationStep" from ADR-0016 Phase 3 — but generalised: not just confirmation steps, *all* stages.
4. **`Decision` record falls out for free.** Once Confirmation responses are typed (#2), it costs nothing to extend the response to carry `warnings_shown` and friends.
5. **Domain / App / Presentation layering falls out for free.** Once Context exists (#1), pure helpers stop needing to live on `ProtocolAgent`, and the natural place for each is its layer.
6. **Null-object handlers fall out for free.** Once Confirmation is typed (#2), handler implementations are interchangeable; the fork dies.

So the ordered investment is: **Context → Confirmation Req/Resp → Stage.** Three abstractions, in that order, and the other four arrive as consequences.

## The deeper "why" — complexity budgets

Every codebase has a complexity budget. When complexity grows (new feature, new surface, new audit requirement), you can spend that budget on:

- **More code in the same shape** — methods on the existing class, branches in the existing function. *Cheap now, expensive later.*
- **A new abstraction that absorbs the complexity** — a type, an interface, a layer. *Expensive now, cheap later.*

Each time `nl2protocol` faced that fork — adding the live mode, adding shape warnings, adding provenance, adding namespace splits — it chose "more code in the same shape." That ships features. It accumulates structural debt. This ADR is the moment of naming the debt.

The wrong move would be to try to do all of this at once. The right move is the one this codebase is already starting: pay down one layer of debt at a time, ship the rest, come back when the next layer hurts. That is how compilers got the architecture they have now — nobody designed it on day one.

## Decision

1. **Recognise nl2protocol as a dialogue-driven pipeline compiler.** Use the compiler vocabulary (Stage, IR, Pass, Context) when discussing architecture decisions going forward. When you find yourself reaching for one of the seven missing abstractions, name it explicitly using these terms rather than inventing local jargon.

2. **Adopt the abstractions in the order Context → Confirmation Req/Resp → Stage**, not in any other order. The "four-fall-out" abstractions (Decision, Layering, Null Handler, Detector reuse) are consequences, not work items.

3. **ADR-0016 Phase 1 is the first concrete payment** — function redistribution out of `ProtocolAgent`. It is being executed concurrently with this ADR and addresses the layering symptom (#6 above) without yet introducing the layering abstractions. That is acceptable because Phase 1 is a *move* (reduce coupling, reveal concepts), not yet an *abstraction* (introduce types). The abstractions come later.

4. **Do not attempt to introduce all three core abstractions in one sweep.** Each gets its own ADR when adopted. ADR-0016's deferred Phase 2 (ConfirmationStrategy) corresponds to abstraction #3 above and should be reframed as "introduce `ConfirmationRequest` / `ConfirmationResponse`" when promoted. ADR-0016's deferred Phase 3 (ConfirmationStep) corresponds to abstraction #1 above, generalised to all stages, and should be reframed as "introduce `Stage` type" when promoted.

5. **Triggers for promoting each abstraction:**
   - **Context**: when the next pure-function extraction needs more than 2 config fields and threading them explicitly starts feeling silly. (Soft trigger — judgement call.)
   - **Confirmation Req/Resp**: when one of (a) a third confirmation surface is being added, (b) the same bug needs to be fixed in both `_confirm_labware_assignments` and `_confirm_labware_assignments_via_handler`, or (c) the tee-for-debugging story is actually wanted (not hypothetical). Same triggers as ADR-0016 Phase 2.
   - **Stage**: after the first two are in place, when adding a new stage to `run_pipeline` requires more than 50 lines of in-place code and the orchestrator method exceeds ~1000 lines.

## Consequences

After recognising this categorisation:

- Future feature additions can be evaluated against the missing abstractions: "does this add another `if handler is not None` branch? Are we now at the Confirmation Req/Resp trigger?"
- New ADRs that propose pipeline-shape changes should be checked against this map: which of the seven abstractions does the change rely on or sidestep?
- The CLI/browser duplication is no longer a mystery — it has a name (no Confirmation Req/Resp type) and a known fix.
- The `models/` vs `analysis/` confusion is no longer a question — it has a name (no Domain/App/Presentation layering) and a known fix (which lands automatically once Context exists).
- `run_pipeline`'s growth into a 700-line method is no longer a smell to suffer — it has a name (no Stage type) and a known fix.

What this ADR does *not* do:

- It does not commit to a timeline. The three core abstractions ship when their triggers fire, not on a schedule.
- It does not specify the exact Python shape of `Context`, `ConfirmationRequest`, or `Stage`. Each gets its own ADR when adopted — this one only sets the conceptual map.
- It does not retroactively change any code outside of what ADR-0016 already commits to. The compiler vocabulary is adopted; the compiler architecture is not yet enforced.

## Notes for future readers

- If you are adding a feature that requires another confirmation surface (e.g. headless eval mode, recorded session replay), **stop and read this ADR first**. The right move is probably to promote the Confirmation Req/Resp abstraction now rather than add a fourth `if handler is not None` branch.
- If you are adding a new pipeline stage and find yourself writing `_emit_stage_started(9, "...")`, **stop and read this ADR first**. You are about to add evidence that the Stage type is overdue.
- If you are adding a helper that does work and wondering where it belongs, the answer follows from the seven abstractions: a function that reads spec/labware/config and returns data is analysis (not a model); a function that returns a string for human eyes is presentation; a function that ties stages together is application. `models/` holds types only.
- The seven abstractions are not exhaustive — they are the seven this codebase is currently missing. As features evolve, others may appear (an `IR` type if `ProtocolSpec` and `ProtocolSchema` start needing common operations; a `Pass` type if optimisations enter the picture). The categorisation here is a tool, not a closed list.
