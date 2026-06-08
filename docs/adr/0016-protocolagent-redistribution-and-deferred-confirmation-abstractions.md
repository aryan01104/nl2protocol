# ADR-0016: ProtocolAgent function redistribution and deferred Confirmation abstractions

**Status:** Accepted (redistribution) / Deferred (ConfirmationStrategy, ConfirmationStep)
**Date:** 2026-06-08
**See also:** [ADR-0017](0017-dialogue-driven-pipeline-compiler-architecture.md) reframes this refactor as Phase 1 of a larger arc. ADR-0016's deferred ConfirmationStrategy corresponds to ADR-0017's `ConfirmationRequest` / `ConfirmationResponse` abstraction (#3 of seven); the deferred ConfirmationStep corresponds to ADR-0017's `Stage` abstraction (#1 of seven), generalised beyond confirmation.

## Context

`nl2protocol/pipeline.py` is 2139 lines. The bulk of that file is `ProtocolAgent`, a class that started as the pipeline orchestrator and accreted helpers as the pipeline grew. As of this ADR, the class carries 17 methods that cluster into seven concerns:

| Cluster | Methods | Concern |
|---|---|---|
| A. Construction | `__init__` | Dependency injection |
| B. Reporter facades | `_emit_stage_started`, `_emit_progress` | Outbound event emission |
| C. User-confirm flows | `_confirm_initial_contents_via_handler`, `_maybe_handle_namespace_split`, `_confirm_labware_assignments_via_handler`, `_confirm_labware_assignments` | Round-trip user interaction |
| D. Spec analysis (pure) | `_infer_source_containers` (already `@staticmethod`) | Read spec, return data |
| E. Spec mutation (pure-ish) | `_apply_namespace_split`, `_apply_labware_assignments`, `_build_user_action_provenance` | Mutate spec given a confirmed answer |
| F. Labware shape checks (pure) | `_shape_mismatch_warnings`, `_shape_mismatch_facts`, `_shape_mismatch_note` | Compute well-vs-labware fit |
| G. LLM-call adapters | `_review_labware_suggestions`, `_resolve_description_gaps_pre_pass` | Wrap LLM-using helpers |
| H. The algorithm | `run_pipeline` | Stage walker |

Diagnosis (function-by-function audit, 2026-06-08):

- ~7 of 16 non-init methods read **no load-bearing `self.*` state** — they live on the class for namespacing, not because they need to. `_infer_source_containers` is *literally* `@staticmethod`; `_apply_namespace_split` has zero `self.` reads in its body.
- The shape-mismatch trio (`_shape_mismatch_warnings`, `_shape_mismatch_facts`, `_shape_mismatch_note`) is labware-domain logic that only touches `self.config_loader`. `_shape_mismatch_note` is a four-line wrapper around `_shape_mismatch_facts` that adds a 14-word post-decision prefix.
- The CLI/browser fork for labware assignments runs as two parallel ~107-line methods (`_confirm_labware_assignments` vs `_confirm_labware_assignments_via_handler`). They build essentially the same table and call essentially the same apply step; only the "ask the user" leg differs.
- `run_pipeline` itself branches on `if self.X_handler is not None` three times to pick between CLI and browser paths.

Two of the three problems above are local cleanup. The third is an architecture decision.

## Decision

Two phases. Phase 1 is being executed concurrently with this ADR. Phase 2 is captured as deferred work with explicit triggers — it is *not* being done now.

### Phase 1 — Function redistribution (Accepted, this commit)

Each method is moved to the module whose concept it belongs to. Methods that read no load-bearing self-state become free functions taking their dependencies as parameters.

| # | Method | Action | New home |
|---|---|---|---|
| 1 | `__init__` | Keep | `pipeline.py` |
| 2 | `_emit_stage_started` | Move → free function | `for_cli/progress.py` |
| 3 | `_emit_progress` | Move → free function | `for_cli/progress.py` |
| 4 | `_infer_source_containers` | Move → free function | `spec_analysis.py` (new) |
| 5 | `_confirm_initial_contents_via_handler` | Keep | `pipeline.py` |
| 6 | `_maybe_handle_namespace_split` | Keep | `pipeline.py` |
| 7 | `_apply_namespace_split` | Move → free function | next to `NamespaceSplitDetector` |
| 8 | `_confirm_labware_assignments_via_handler` | Keep | `pipeline.py` |
| 9 | `_review_labware_suggestions` | Move → free function | `gap_resolution/` |
| 10 | `_resolve_description_gaps_pre_pass` | Keep | `pipeline.py` |
| 11 | `_confirm_labware_assignments` (CLI) | Move → free function | `for_cli/labware_confirm.py` |
| 12 | `_apply_labware_assignments` | Move → free function | next to provenance helper |
| 13 | `_build_user_action_provenance` | Move → free function | `models/spec.py` |
| 14 | `_shape_mismatch_warnings` | Move → free function | `models/labware.py` |
| 15 | `_shape_mismatch_facts` | Move → free function, **merged with #16** | `models/labware.py` |
| 16 | `_shape_mismatch_note` | **Collapse into #15** (kwarg `provenance_tail=True`) | (deleted) |
| 17 | `run_pipeline` | Keep | `pipeline.py` |

Result: `ProtocolAgent` shrinks from 17 methods to 6 — `__init__`, three user-confirm flows, the sub-orchestrator runner, and `run_pipeline`. Every remaining method reads two or more `self.*` slots in a load-bearing way; nothing hitchhikes.

The class is **not** being renamed in this phase. `ProtocolAgent` → `ProtocolPipeline` is a separate concern that touches every import site and deserves its own commit. The name is misleading (the class is a deterministic stage walker, not an LLM-driven agent), but renaming is mechanical and orthogonal to the layering work here.

### Principle: a method stays on the class iff it reads two or more `self.*` slots in a load-bearing way

This is the test applied to every method in the table. A method that reaches for one slot is a free function with that slot as a parameter; a method that reaches for several is a real method because the dependency bundle is what `self` exists to carry.

`_emit_stage_started` reads `self.reporter` — one slot. Becomes `emit_stage_started(reporter, number, name)`. `_resolve_description_gaps_pre_pass` reads `self.confirmation_handler`, `self.cm`, `self.reporter`, `self.config_loader` — four slots. Stays a method.

### Phase 2 — ConfirmationStrategy abstraction (Deferred)

**Problem this would solve:** the CLI/browser fork in `run_pipeline`. Three `if handler is not None: via_handler else: cli_loop` branches today; each surface adds one. The CLI `_confirm_labware_assignments` and browser `_confirm_labware_assignments_via_handler` are 107-line near-duplicates that build essentially the same table.

**Shape:**

```python
class ConfirmationStrategy(Protocol):
    def ask(self, kind: str, payload: dict) -> Optional[dict]: ...

class CLIStrategy:           # dispatches on kind, runs REPL loops
class BrowserStrategy:       # dispatches on kind, calls handler.confirm()
class TeeStrategy:           # runs primary, mirrors to secondary
class RecordingStrategy:     # wraps real strategy, persists to disk
class ReplayStrategy:        # replays a recorded session in tests
```

The orchestrator stops branching on mode; the choice is made at construction time.

**Why deferred:** the abstraction has cost — four new types plus a `kind`-dispatch table. Its payoff lands when the third surface arrives, the tee-for-debugging story is needed, or the two confirm methods start drifting apart at bug-fix time.

**Triggers for taking this on:**

1. A third confirmation surface is being added (e.g., headless eval mode that scripts confirmations from JSON).
2. The same bug needs to be fixed in both `_confirm_labware_assignments` and `_confirm_labware_assignments_via_handler` and you notice.
3. The "run both CLI and browser for debugging" capability is actually wanted, not hypothetical.

Until one of these fires, the duplication is annoying but bounded.

### Phase 3 — ConfirmationStep abstraction (Deferred)

**Problem this would solve:** the duplicated "build payload → ask user → apply" shape across the four confirmation surfaces (initial contents, namespace split, labware assignments, source-container ack). Each is implemented as a bespoke method on `ProtocolAgent`. Adding a fifth would mean another bespoke method.

**Shape:**

```python
class ConfirmationStep(Protocol):
    kind: str
    def applies(self, spec, ctx: dict) -> bool: ...
    def build(self, spec, ctx: dict) -> dict: ...
    def apply(self, spec, response: dict) -> None: ...
```

The orchestrator collapses to a loop:

```python
for step in steps:
    if not step.applies(spec, ctx): continue
    payload = step.build(spec, ctx)
    response = self.strategy.ask(step.kind, payload)
    if response is None: return  # user aborted
    step.apply(spec, response)
```

**Why deferred:** this is the bigger abstraction, with the bigger payoff per surface, but the bigger upfront cost. It is also a *successor* to Phase 2 — without `ConfirmationStrategy`, `Step` has nowhere to delegate the "ask" leg.

**Triggers for taking this on:**

1. Phase 2 is done.
2. A fifth confirmation surface is being added (a fifth would push it from "list of cases" to "needs an abstraction").
3. The `build` and `apply` logic for two surfaces start sharing code, suggesting a common substrate.

## Why not just delete the CLI path

Tempting: rip out `_confirm_labware_assignments` and the related CLI prompts, make the browser path the only one. But the CLI is the default for `nl2protocol` invocations without a server and it has real users (Aryan's smoke runs, evals). Killing it would either gate the tool behind a browser or require a synchronous CLI implementation of every modal — which is exactly the current CLI code. The CLI/browser fork is real product surface, not legacy cruft.

## Why not split ProtocolAgent into multiple classes

The earlier audit noted that the class is *also* a god object — 17 methods, ~700-line `run_pipeline`. Splitting into `LabwareConfirmationStep`, `GapResolutionStep`, `CodegenStep` is the eventual destination. But that split *is* Phase 3 (ConfirmationStep) for the user-flow methods. The non-user-flow methods (codegen, simulate) already split cleanly into module-level functions and are tracked in a separate cleanup. Splitting the class into more classes *without* the Step abstraction would just move methods around without reducing the underlying duplication.

## Consequences

After Phase 1:

- `pipeline.py` loses ~700 lines (the helpers move out). The remaining file reads as the algorithm: imports name the phases, methods name the stages.
- ~9 helpers become reusable from tests, evals, and debug scripts without constructing a `ProtocolAgent`.
- Shape-mismatch logic is in `models/labware.py` where it belongs — visible to anyone touching labware, not buried in an orchestrator.
- The two confirm-method ~107-line near-duplicates **remain**. Phase 1 does not address that; Phase 2 does.
- Adding a confirmation surface is still a bespoke method on `pipeline.py`. Phase 1 does not address that; Phase 3 does.

The trade Phase 1 makes is **layering over collapsing**: the methods stop hitchhiking on the wrong class, but the structural duplication between CLI and browser stays for now. That's deliberate. Phase 2 is the right place to address it, and Phase 2 should be done when the cost is justified, not as a side effect of moving methods around.

## Notes for future readers

- If you find yourself adding a fifth confirmation surface, **do not** add a fifth `_confirm_X` method to `pipeline.py`. Read Phase 2 + Phase 3 above and budget for them.
- If you find yourself fixing the same bug in `_confirm_labware_assignments` and `_confirm_labware_assignments_via_handler`, the drift bell has rung. Promote Phase 2.
- The shape-mismatch helpers (`models/labware.py`) are the test case for Phase 1's principle: they were tangled into the orchestrator because they were written *as part of* a confirmation flow, but they have no orchestration concern. They model labware geometry. Future helpers in the same shape (purity + one config dep) should land in `models/`, not on the class.
