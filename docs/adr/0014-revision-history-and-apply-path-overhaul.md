# ADR-0014: Per-field revision history and apply-path overhaul

**Status:** Accepted
**Date:** 2026-05-27

## Context

After ADR-0008's unified gap-resolution loop and ADR-0009's provenance schema landed, a cluster of related defects emerged when actually running the loop end-to-end (western blot smoke test, 2026-05-20 → 2026-05-23):

1. **The gap loop bounced.** Users would override a fabrication or accept a constraint-clip suggestion; on the *next* iteration the verifier re-ran its cited_text check on the same Provenance and re-raised the same Gap. The orchestrator ran to its iteration cap (3) without converging. ADR-0008's `Orchestrator.run` re-detects on every iteration; ADR-0009's `review_status` lifecycle tracked the user action but the verifier didn't consult it.

2. **Resolutions overwrote the audit trail.** When the apply path wrote a new value to a tracked field, the previous (value, provenance) pair was lost. The state-log JSON dumps and the HTML report had no way to show "extractor said C7; constraint clip rewrote it to A1." The intermediate state existed only in the user's head.

3. **The tooltip on resolved values lied about provenance.** After the user accepted a suggestion that changed wells from C7 to A1, the head's `wells_provenance` still carried `source="instruction"` with `cited_text=["C7"]`. The cited text grounded the *old* value, not the new one; the suggester's actual `positive_reasoning` ("Wells [C7] exceed the labware's valid range; clipping to in-range wells…") was dropped on the floor. The user could see "A1" in the cell but couldn't see why.

4. **The HTML report flashed.** Every spec event (extracted_spec, resolved_spec, completed_spec) triggered a full column re-render via `appendStepBlocks` — every `.step` child was destroyed and rebuilt. Modal closes during the gap loop didn't show targeted feedback; the affected cell updated only at end-of-loop alongside everything else.

5. **The visual layout buried the temporal evolution.** Five columns (instruction / extracted / resolved / validated / generated) tried to show how each value changed by lining up snapshots horizontally. SVG arrows between columns added more visual noise. The reader couldn't easily see "this cell's value went C7 → D1 because of a constraint clip"; the information was spread across columns.

These are five symptoms of one root cause: the system tracked value state but not value *history*. Adding history changes how the verifier, the apply path, and the UI all behave.

## Decision

Introduce per-field revision history as a substrate, then route the gap-resolution apply path, the verifier, and the UI through it. Six interlocking changes:

1. **Verifier short-circuits on terminal `review_status`** — the cited_text check skips Provenances whose review lifecycle has terminated (user action or reviewer agreement).
2. **Tracked-field types gain `prior_revisions: List[Self]`** — every Provenanced* type, `LocationRef`, `WellContents`, and `LabwarePrefill` carry a flat history list.
3. **Apply path routes through `push_revision`** — one logical write per `_apply_at_path` call snapshots the head's state into `prior_revisions` before mutating. Holds for the orchestrator's apply path and the pre-orchestrator stage 2.5 writes.
4. **Value-subfield accepts/edits replace the provenance** — when `accept_suggestion` or `edit` changes a LocationRef value subfield (`description` / `well` / `wells` / `well_range`), the apply path constructs a fresh `Provenance(source="inferred", positive_reasoning=suggestion.positive_reasoning, …)` and writes it to the corresponding `*_provenance` slot. The stale instruction citation moves into `prior_revisions[0]`'s provenance.
5. **The HTML renderer surfaces the chain inline per cell** — each value cell renders as `prior → prior → head` with consecutive-identical runs collapsed.
6. **The report collapses three spec columns into one** — `Protocol Steps` carries the live spec; per-field chains carry the temporal evolution that previously spanned three columns. Live mode updates one step block in place on each `gap_resolved` event instead of redrawing the column.

Each change is described in its own section below with the relevant code pointers and tests.

## 1. Verifier respects terminal review states

**Commit:** `de420da`

`SemanticExtractor._verify_claimed_instruction_provenance` (`nl2protocol/extraction/extractor.py`) re-runs three checks on every Provenance whose `source == "instruction"`:
1. cited_text is non-empty
2. cited_text appears in the instruction
3. the value is contained within at least one cited_text entry

A new short-circuit before those checks: if `prov.review_status` is in `TERMINAL_REVIEW_STATUSES`, return. The terminal states are:

```python
TERMINAL_REVIEW_STATUSES = frozenset({
    "user_confirmed",
    "user_edited",
    "user_accepted_suggestion",
    "user_overrode_fabrication",
    "reviewed_agree",
})
```

Once a user (or the independent reviewer) has acted on a value, re-running the cited_text check would only re-raise a gap they already resolved. The check belongs at the *pre-resolution* boundary; once a value's lifecycle has terminated, the verifier has nothing useful to say about it.

**Why this was safe to add without ADR 2**: Phase 1 of this ADR shipped standalone as commit `de420da`. The terminal short-circuit preserves the `source="instruction"` attribution on the Provenance — it just stops re-checking. With revision history in place (this ADR's later sections), the stricter "flip source to inferred on user action" approach becomes available — section 4 takes that step for value subfields.

**Tests:** `tests/test_gap_resolution_detectors.py::TestProvenanceWarningDetector` adds five parametrized cases (one per terminal status) asserting no fabrication gap fires, plus a counter-check that `review_status="original"` still raises.

## 2. Tracked-field types gain `prior_revisions`

**Commit:** `dc59296`

Seven leaf types in `nl2protocol/models/spec.py` gain:

```python
prior_revisions: List["Self"] = Field(default_factory=list)

@field_validator('prior_revisions')
@classmethod
def _no_nested_history(cls, v):
    if any(rev.prior_revisions for rev in v):
        raise ValueError(...)
    return v
```

The seven types: `ProvenancedVolume`, `ProvenancedDuration`, `ProvenancedTemperature`, `ProvenancedString`, `LocationRef`, `WellContents`, `LabwarePrefill`.

**Shape choice — flat list of Self, not recursive linked list, not separate Revision wrapper.** Considered alternatives in design discussion (transcribed during the Phase 2 walk-through). Flat list wins on three criteria:
- LocationRef has multiple sub-fields that evolve together; a generic `Revision[T]` wrapper doesn't model the temporal coupling cleanly. Snapshotting a full LocationRef does.
- Push is O(1) (snapshot only the head's own fields; never copy the existing chain).
- JSON serialization is flat — readable in `pipeline_state_*.json` dumps without nested-depth descent.

**Invariant: entries in `prior_revisions` themselves have empty `prior_revisions`.** The head owns the chain; revisions are frozen point-in-time snapshots. Enforced by the validator above.

**Helpers** (also in `models/spec.py`):

```python
def push_revision(field, **new_state) -> None:
    """Snapshot field's current state into prior_revisions (with the
    snapshot's own prior_revisions emptied), then mutate the head per
    new_state. Deep-copies on snapshot to avoid sharing mutable
    sub-objects with the head."""

def replace_with_history_preserved(old_field, new_field):
    """When an apply path needs to SWAP one tracked field for a fresh
    one (typical pattern: a suggester emits a whole new Provenanced*
    instance), transfer old_field.prior_revisions + a snapshot of
    old_field onto new_field. Returns a new model instance; caller
    assigns. Without this, the swap would drop the old field's
    chain on the floor."""
```

**Backward compatibility:** the field has `default_factory=list`, so every existing constructor call (and every captured spec in older state logs) deserializes fine — `prior_revisions` defaults to empty. Read-sites (`field.value`, `field.provenance`, `step.destination.wells`) keep working unchanged; `prior_revisions` is opt-in for code that wants history.

**Tests:** `tests/test_revision_history.py` covers default-empty on each leaf type, `push_revision` atomic semantics, snapshot/head independence, LocationRef multi-field revisions, the no-nested-history validator.

## 3. Apply path routes through `push_revision`

**Commit:** `aaa3445`

`_apply_at_path` in `nl2protocol/gap_resolution/orchestrator.py` previously did:

```python
setattr(parent, slot, new_value)               # overwrite value
setattr(parent, prov_slot, new_provenance)     # overwrite provenance — old one gone
```

Now: every branch that mutates a tracked field calls `push_revision(target)` *before* the existing setattr/stamp logic. Exactly **one** `push_revision` per logical write, even when the write touches multiple sub-fields (a wells change plus the corresponding provenance stamp count as one revision, not two).

Branches updated:
- **Fabrication-shaped path** (`steps[N].field.provenance$`) — accept_suggestion / override / edit all push the parent (the Provenanced* or LocationRef carrying the provenance slot) before mutating.
- **Whole-field accept_suggestion** (`steps[N].field`, e.g. swap a whole LocationRef) — uses `replace_with_history_preserved` so the new instance carries the old field's chain + a snapshot of the old head.
- **Whole-field edit** (`steps[N].field` with a typed scalar) — pushes before mutating `existing.value`.
- **Subfield path** (`steps[N].field.subfield`) — pushes the parent (LocationRef) before setting the subfield + stamping. Section 4 below extends this branch further.
- **`initial_contents[N].volume_ul`** — `push_revision(wc, volume_ul=float(new_value))` writes the new volume and snapshots simultaneously.

Pre-orchestrator stage 2.5 writes in `nl2protocol/pipeline.py` also route through `push_revision`:
- IC batch confirmation `ic.volume_ul = …` → `push_revision(ic, volume_ul=new_vol)` (line 725)
- LocationRef labware-assignment (`ref.resolved_label = …; ref.resolved_label_provenance = …`) collapses to one `push_revision` call writing both (line 935)
- `wc.labware = confirmed[…]` and `pf.labware = confirmed[…]` → `push_revision` (lines 947 / 950)

**`_stamp_user_action` and `_stamp_resolution_action` stay pure** — they mutate provenance slots without pushing. The push happens at the apply-path branch top so calling these helpers afterwards doesn't double-push.

**Tests:** `tests/test_revision_history.py::TestApplyPathPushesRevisions` covers the subfield path and the fabrication-shaped path end-to-end via `default_apply_resolution`.

## 4. Value-subfield accepts/edits replace the provenance

**Commit:** `fe132a6` (fix 2)

The subfield branch of `_apply_at_path` previously called `_stamp_user_action(target, user_action)` after writing the new value. `_stamp_user_action` walks every Provenance slot on the parent and flips `review_status` while preserving `source` and `cited_text`. For LocationRef value subfields, that leaves the head with a `cited_text` that grounded the OLD value — misleading at hover time.

New behavior: when the subfield is one of `description` / `well` / `wells` / `well_range` AND the action is `accept_suggestion` or `edit`, replace the corresponding provenance slot with a fresh `Provenance(source="inferred", …)`:

```python
_LOCATIONREF_VALUE_SUBFIELDS = {
    "well": "wells_provenance",
    "wells": "wells_provenance",
    "well_range": "wells_provenance",
    "description": "description_provenance",
}

# inside the subfield branch, after setattr(target, subfield, new_value):
if subfield in _LOCATIONREF_VALUE_SUBFIELDS:
    if action == "accept_suggestion" and suggestion is not None:
        new_prov = Provenance(
            source="inferred",
            positive_reasoning=suggestion.positive_reasoning,
            why_not_in_instruction=suggestion.why_not_in_instruction,
            review_status="user_accepted_suggestion",
            confidence=suggestion.confidence,
        )
    elif action == "edit":
        new_prov = Provenance(
            source="inferred",
            positive_reasoning="User edited this value directly during gap resolution; not lifted from the instruction.",
            why_not_in_instruction="User chose this value; it was not cited from the instruction.",
            review_status="user_edited",
            confidence=1.0,
        )
    setattr(target, _LOCATIONREF_VALUE_SUBFIELDS[subfield], new_prov)
    return
```

The original instruction-cited provenance isn't lost — it lives on as `prior_revisions[0]`'s provenance for that slot. The chain's leftmost segment carries the extractor's original `cited_text`, and the head's tooltip carries the suggester's reasoning. Both audit trails are visible.

**`resolved_label` subfield is unchanged** — `_stamp_resolution_action` already constructs a fresh provenance for that slot. Section 4 just extends the same shape to the value subfields.

**Signature change:** `_apply_at_path` and `default_apply_resolution` now thread the `suggestion` argument through so the value-subfield branch can read `suggestion.positive_reasoning`. The orchestrator already passes the matching `Suggestion` instance to `apply_resolution`.

**Behavior change to call out:** the previous `_stamp_user_action` broad-stamped review_status on EVERY provenance slot of the LocationRef when ANY subfield changed. After this commit, editing wells leaves `description_provenance.review_status` untouched. That's the more precise contract — "user touched wells" no longer claims "user touched description."

**Tests:** `tests/test_revision_history.py::TestApplyPathPushesRevisions::test_subfield_accept_suggestion_migrates_suggester_reasoning_to_head` and `test_subfield_edit_writes_user_edited_inferred_provenance`.

## 5. Renderer chains with consecutive-dedup

**Commits:** `582c8bf` (initial chain rendering), `76d4016` (first dedup pass), `fe132a6` (fix 1: consecutive-dedup).

`_render_revisioned_value` in `nl2protocol/reporting.py` walks `field.prior_revisions + [head]` projecting each to (text, prov) via the caller's `value_formatter` + `prov_getter`. It produces an inline HTML chain:

```
<span class="prior-rev">…</span><span class="rev-arrow">→</span><span …>…</span>
```

`prior-rev` carries strikethrough + 55% opacity; `rev-arrow` is the dimmed `→`; the head span uses today's `_render_provenanced_value` with its prov_id intact for cite ↔ value pair-highlight + lab-state row hover.

**Filter rules** (run in order):

1. **Drop priors whose projected provenance is None.** A LocationRef's `wells_provenance` may be null in an older snapshot; rendering an unattributed value span would mislead.
2. **Collapse consecutive identical-text runs.** A `push_revision` call snapshots the whole tracked object even when the sub-field projection didn't change (e.g., the stage 2.5 labware-assignment push leaves the wells row's projection identical). From each run, prefer the head when its text is in the run (so the head's provenance — typically the most-resolved attribution — survives); else keep the first item in the run.

The dedup rule supersedes the earlier "drop priors matching head" approach: any prior that texts-matches its successor (head or next prior) is dropped, not just those matching the head. Tested by `test_consecutive_identical_priors_collapse` (the western-blot screenshot bug: `C7 → C7 → A1` collapses to `C7 → A1`).

**Each chain segment is independently hoverable** — data-prov-* attributes on every span carry that revision's source, cited_text, positive_reasoning, why_not_in_instruction, review_status, confidence. The existing tooltip JS reads these per-element.

**Only the head segment carries `data-prov-id`.** Priors are frozen snapshots and don't participate in cite ↔ value pair-highlight or panel-row hover.

**Tests:** `tests/test_revision_history.py::TestRendererChain` covers no-history fallback, single-prior chain shape, head-only prov_id, LocationRef subfield projection, None-prov skip, consecutive-dedup, and the source-changed-but-text-same case (filter wins; head styling shows the source change).

## 6. UI collapse: 3 columns, grid per step, per-cell live update

**Commits:** `219464f` (3-column collapse), `9a08cb0` (grid + ▴ removal), `0f90486` (per-cell live update).

### Three columns, not five

Previous layout: `Instruction | Extracted Spec | Resolved Spec | Validated Spec | Generated Python`. Three of those carried the same spec at different temporal snapshots. After per-field chains exist, the temporal evolution lives inside each value cell — the three spec columns collapse into one.

New layout: `Instruction | Protocol Steps | Generated Python`.

Template structure changes in `nl2protocol/reporting_templates/report.html.jinja`:
- Delete the Extracted Spec and Resolved Spec column blocks.
- Rename the Validated Spec column to "Protocol Steps".
- Keep `col-validated-spec` as an alias CSS class on the renamed column so existing CSS rules (`.col-validated-spec .step.is-script-linked`, etc.) and JS selectors continue to match without a sweeping rename.
- CSS grid drops `cols-4` and `cols-5`; live-mode column-gating only reveals `cols-1` → `cols-2` → `cols-3`.

Static path: `HTMLReporter.finalize` builds ONE step list (`protocol_steps`) from the most-resolved spec available (completed > resolved > extracted). All three spec events reference the same mutated spec object by finalize time; the picker is a graceful fallback for failed runs.

### 3-column grid per step

Each step block's parameter rows render as a CSS grid: `label | value | check`. No borders, whitespace alignment. The label column sizes to the widest label, the value column flexes, the check column right-aligns.

`_step_to_render_dict.detail_lines` is now a list of dicts (was a list of pre-baked HTML strings):

```python
{
    "label": "volume",            # template appends ":"
    "value_html": "<span…>15 uL</span>",
    "check": "within p20 range",  # empty when no check
    "indented": False,            # True for wells rows under source/destination
    "prov_id": "s0-volume",
    "is_empty": False,            # True for ✗ placeholder rows
}
```

Both the static Jinja loop and the live-mode `appendStepBlocks` JS build the same grid HTML from these dicts.

**The `▴` non-instruction marker is removed.** Color class + dotted underline already communicate "model-filled"; the marker was a third visual cue for the same fact. The legend's marker entry is replaced with a chain-rendering entry showing `~~prior~~ → head`.

### Per-cell live update on `gap_resolved`

Previously: every spec event called `appendStepBlocks` which dropped every `.step` child and rebuilt the column. Modal closes during the gap loop produced a final flash at end-of-loop.

Now: the server's WebSocket sender captures the live spec on each spec event (`self._live_spec = …`). When `gap_resolved` arrives, it re-renders just the affected step via `_step_to_render_dict(self._live_spec.steps[step_order-1], …)` and attaches the result as `step_dict` on the event payload.

The browser's `gap_resolved` handler:

```js
case "gap_resolved":
  if (e.data.resolution_arrows) updateResolutionArrows(e.data.resolution_arrows);
  if (e.data.step_dict) updateStepBlock(e.data.step_dict);
  break;
```

`updateStepBlock` finds the existing step block by `data-step-order` and rebuilds its body in place. The chain on the affected cell appears inline automatically because the spec's `prior_revisions` are up-to-date by the time the event fires.

The `resolved_spec` event handler is gutted of its `appendStepBlocks` call — per-cell updates have already happened during the gap loop. `resolved_spec` now only refreshes the instruction column (cite spans may shift) and bulk panels. `completed_spec` still does a final pass to surface inline ✓ tags + per-step warnings that arrive with `constraint_check_done`.

**Threading note:** the orchestrator emits `gap_resolved` AFTER `apply_resolution` returns. The WebSocket sender processes events sequentially from the queue, so when the bridge reads `self._live_spec.steps[step_idx]`, the apply mutation is already complete. No locking needed.

## Schema additions

`nl2protocol/models/spec.py`:
- Seven leaf types gain `prior_revisions: List[Self] = Field(default_factory=list)`.
- Seven `_no_nested_history` `@field_validator`s.
- Module-level `push_revision(field, **new_state) -> None`.
- Module-level `replace_with_history_preserved(old_field, new_field)`.

`nl2protocol/extraction/extractor.py`:
- Module-level `TERMINAL_REVIEW_STATUSES: frozenset[str]`.

`nl2protocol/gap_resolution/orchestrator.py`:
- Module-level `_LOCATIONREF_VALUE_SUBFIELDS: dict[str, str]`.
- `_apply_at_path` signature now takes `suggestion: Optional[Suggestion] = None`.

No new external interface; `default_apply_resolution`'s public signature is unchanged.

## Alternatives considered

**(A) Recursive history on Provenance only, not on the tracked containers.** Each Provenance carries `prior: Optional[Provenance] = None` linking backward. Rejected: LocationRef has three independent provenance slots (description / wells / resolved_label); a per-provenance chain doesn't model temporal coupling between sub-field writes. The container-level chain (LocationRef snapshots the whole shape) captures that the wells write and the resolved_label write happened at different times under different actions.

**(B) Recursive linked-list on the container (`previous: Optional[Self]`).** Rejected: each push deep-copies the entire chain (because the container's `previous` is part of the model), so an N-deep history costs O(N²) copies over N writes. JSON nests N levels deep instead of being a flat list — harder to read in state-log dumps. Flat list with `Optional[List[Self]]` gives O(1) push and flat JSON.

**(C) Generic `TrackedField[T]` wrapper + separate `Revision[T]` type.** Rejected: LocationRef can't be wrapped neatly — its multiple sub-fields would force either a per-sub-field wrapper (loses temporal coupling) or a wrap-the-whole-LocationRef approach (forces a `.value` indirection at every read-site). The "field owns its own history" shape (each tracked type adds `prior_revisions: List[Self]` directly) avoids both.

**(D) Verifier flips `source` to `inferred` on user action (without revision history).** Rejected as a standalone change at the Phase 1 stage because it would have destroyed the original instruction attribution. With revision history in place (this ADR), the equivalent flip happens at section 4 (apply path replaces the provenance on value-subfield writes) and the original attribution is preserved in `prior_revisions[0]`.

**(E) Bigger UI refactor: separate "history pane" instead of inline chains.** Rejected for being more disruptive than inline chains and harder to scan — the user has to look in two places (the cell, plus the pane) to understand a value's evolution. Inline chains keep the temporal context next to the value.

**(F) Keep the SVG cross-column arrows and just collapse columns visually.** Rejected: the arrows were the OLD way to show value evolution (cross-column). Inline chains do the same job per-cell and don't need coordinate math. Deleting the arrow code is deferred cleanup; it's dormant (no anchors to find, silently no-ops) but should be removed in a follow-up.

## Tradeoffs

**State-log files grow.** Every `pipeline_state_*.json` now carries `prior_revisions` arrays on every mutated field. Bounded — revisions only accumulate during the pipeline run, max ~3 iterations of the gap loop, so worst case ~3 prior revisions per field. Acceptable. If it becomes a problem, structural sharing (append-only chain; snapshots share Provenance references with their successors) would eliminate the duplication; not implemented today.

**Renderer logic is more complex.** Single-span fallback, chain rendering, dedup with head-preference, per-segment hover routing. Tested but it's more surface area than the pre-3a renderer.

**The apply path is now `suggestion`-aware.** Threading `suggestion` through `_apply_at_path` is mostly mechanical, but means the apply path can no longer be exercised in isolation from the suggester machinery. Tests now build a `Suggestion` instance instead of just a `Resolution`.

**Live mode's per-cell update assumes spec mutations are sequential.** True today (worker thread emits events sequentially after each apply call; bridge processes sequentially). If a future change parallelizes the gap loop, the bridge would need a snapshot at gap_resolved time rather than a live reference. Documented in section 6's threading note.

**The static SVG arrow code is dormant but not deleted.** `_collect_resolution_arrows` still runs and emits arrow data, the template's SVG layer still mounts, the renderArrows JS still tries to draw — they all silently no-op because the source anchors (col-extracted-spec elements) are gone. Safe but messy; follow-up cleanup.

## References

**Commits (chronological):**
- `de420da` Verifier respects terminal review_status; stops fabrication-loop bouncing
- `dc59296` Add prior_revisions field + push_revision helper to spec leaf types
- `aaa3445` Route gap-resolution + stage 2.5 writes through push_revision
- `582c8bf` Render inline revision chains in spec value cells (Phase 3a)
- `219464f` Collapse extracted/resolved/validated columns into one Protocol Steps column
- `76d4016` Filter no-op revision chains in renderer
- `9a08cb0` Render step parameters as a 3-column grid; drop the ▴ marker
- `0f90486` Per-cell live update on gap_resolved (no full column re-render)
- `fe132a6` Dedup chain segments + migrate suggester reasoning onto head provenance

**Files (production code):**
- `nl2protocol/models/spec.py` — `prior_revisions` on 7 leaf types, `push_revision`, `replace_with_history_preserved`
- `nl2protocol/extraction/extractor.py` — `TERMINAL_REVIEW_STATUSES`, verifier short-circuit
- `nl2protocol/gap_resolution/orchestrator.py` — `_apply_at_path` push routing + suggestion-aware subfield branch, `_LOCATIONREF_VALUE_SUBFIELDS`
- `nl2protocol/pipeline.py` — stage 2.5 push_revision routing
- `nl2protocol/reporting.py` — `_render_revisioned_value`, `_step_to_render_dict` dict-shaped detail_lines
- `nl2protocol/reporting_templates/report.html.jinja` — 3-column grid, chain CSS, per-cell update JS handler
- `nl2protocol/server/app.py` — `self._live_spec` capture, `gap_resolved` enrichment with `step_dict`

**Tests:**
- `tests/test_revision_history.py` — data model + push_revision + renderer chain + apply-path integration
- `tests/test_gap_resolution_detectors.py::TestProvenanceWarningDetector` — verifier terminal-status short-circuit
- `tests/test_gap_resolution_orchestrator.py::TestDefaultApplyStampsUserAction::test_subfield_write_stamps_parent_provenance` — updated to the precise-stamp contract (only affected provenance changes)
- `tests/test_reporting.py::TestUnifiedProtocolStepsColumn` — 3-column layout assertions

**Related ADRs:**
- ADR-0008 — unified gap-resolution loop (this ADR extends its apply path)
- ADR-0009 — provenance schema (this ADR adds `prior_revisions` containers but leaves the Provenance schema itself unchanged)
- ADR-0011 — HTML visualization (this ADR collapses its 5-column layout to 3)
- ADR-0012 — fabrication override (this ADR's terminal-status short-circuit applies to overrides too)
- ADR-0013 — live mode (this ADR refines the gap_resolved bridge to attach per-cell step_dict)
