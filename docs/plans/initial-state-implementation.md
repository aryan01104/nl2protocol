# Implementation plan: oracle-driven initial state + labware shape-match

Maps the agreed end-to-end logic onto concrete code. Legend: **[done]**, **[new]**.

> **Revision 3 — scope cut (supersedes Revision 2 and everything below).**
> Decision: **stop using initial contents for labware mapping**, and **stop relying on
> instruction-extracted initial contents**. The xlsx oracle *is* the initial contents.
> Labware mapping reverts to the original resolver (capability + type + LLM), no hints.
>
> **DELETE (files):**
> - `nl2protocol/stage_1_pre_extraction/shape_match.py`
> - `tests/test_shape_match.py`
> - `tests/test_resolver_hints.py`
> - `tests/test_initial_state_reconcile.py` (replaced by one seed test)
>
> **DELETE (in `nl2protocol/pipeline.py`):**
> - methods `_instruction_initial_wells`, `_normalize_initial_contents_ranges`,
>   `_overlay_initial_state`, `_append_initial_state`
> - their call sites: the `_normalize_…` call after extraction (~1739), the
>   shape-match block (`shape_hints` / `match_by_shape` / `_instruction_initial_wells`
>   ~1845–1850), the `hint_conflicts` loop (~1868), the `_overlay_…` call (~1929),
>   the `_append_…` call (~2029)
>
> **DELETE (in `nl2protocol/extraction/resolver.py`) — revert to original:**
> - the `external_hints` param + `self.external_hints` + `self.hint_conflicts`
>   (~313, 320–323) and the hint block in `_resolve_one` (~605–611)
>
> **DELETE (in `nl2protocol/stage_1_pre_extraction/initial_state.py`):**
> - `InitialStateSheet.wells_by_label()` and `.substance_index()` (only the dumped
>   shape-match / reference-resolution used them) + their assertions in
>   `tests/test_initial_state.py`
>
> **KEEP (the oracle delivery path, unchanged):**
> - `parse_initial_state` + `InitialStateSheet` (`cells`, `errors`)
> - server plumbing in `app.py` (parse, dropdown auto-load, thread), the browser
>   file input in `report.html.jinja`, `run_pipeline(initial_state=…)`, `openpyxl`,
>   and `_initial_state_provenance`
>
> **ADD (the swap, in `pipeline.py`):**
> - `_seed_initial_contents_from_oracle(spec, initial_state)` — when `initial_state`
>   is present, **replace** `spec.initial_contents` with one `WellContents(labware,
>   well, substance, volume_ul, volume_ul_provenance=_initial_state_provenance())`
>   per oracle cell, sorted by `(labware, well)`. The labware key is the **config
>   label** straight from the sheet (no nickname, no mapping needed for contents).
> - call it right after extraction (after the None-check) when `initial_state` is set
> - guard the Stage-4 **source-container inference** block with `if initial_state is
>   None` — the oracle is authoritative, so skip inference when it's present
> - revert both resolver constructions in `run_pipeline` to the original (drop
>   `external_hints=…`)
>
> **Resulting flow (oracle present):** extract → seed IC from oracle (config-label
> keyed, volumes set) → labware resolution (original resolver) → IC modal shows the
> oracle entries already settled → assignments + apply (IC untouched, already config
> labels) → orchestrator → constraints → codegen. No map uploaded → fully original
> behavior.
>
> **Verify:** parser tests still pass; new seed test (oracle → one `WellContents`
> per cell, config-label labware, oracle provenance); imports of pipeline/resolver/
> server clean; re-run `pcr_mastermix` → 25 settled rows, no crash, mapping via the
> original resolver.
>
> ---
>
> **Revision 2 — converged design (superseded by Revision 3 above; kept for history).**
> Diagnosing the `pcr_mastermix` crash surfaced a cleaner model. Key decisions:
>
> 1. **One normalized comparison form, two producers.** `WellContents` (extractor,
>    rich: provenance/history, nickname-keyed) and `InitialStateSheet` (xlsx, raw,
>    config-label-keyed) stay separate *types* but both project into a shared form
>    before any comparison: `wells_by_labware() → {labware: set(well)}` for the
>    subset/shape-match, and `contents_map() → {(labware, well): (substance,
>    volume)}` for conflict detection. Nothing ever compares raw objects. This is
>    what was missing when it broke (null wells + nickname-vs-config-label compared
>    blind).
> 2. **Footprint from `LocationRef`s, not `WellContents`.** Build the nickname →
>    well-set footprint by expanding `well`/`wells`/`well_range` on the instruction's
>    **source** `LocationRef`s (reuse the existing range-expansion util). Do NOT
>    reconstruct it from the (often null-well) `WellContents`, and do NOT match back
>    by substance — that was fragile.
> 3. **Oracle is authoritative, by reference — no object mutation.** Drop
>    `_overlay_initial_state` / `_append_initial_state`. `spec.initial_contents`
>    (extractor) is used ONLY for conflict-detection + matching. `InitialStateSheet`
>    is the starting state; the few consumers that read starting state (well-state
>    seeding, report lab-state rows, IC modal) branch to read the oracle when present.
> 4. **Gap-resolver accept/apply atomicity (oracle-independent crash fix).** Resolving
>    a gap must change the spec; a resolution that produces no state change must
>    surface/abort, never silently re-detect and loop. This is what hung the run, and
>    it would hang `pcr_mastermix` even with no xlsx.
> 5. **Range normalization of the extractor's own initial_contents** (oracle-independent):
>    a `WellContents` with a null well but a ranged source `LocationRef` should be
>    expanded to concrete per-well entries, so a ranged initial content never becomes
>    an unresolvable null-well gap. Fixes correctness on the no-oracle path too.
>
> **Implementation order:** (4) atomicity + (5) range-normalization first — they fix
> the crash and are correct regardless of the feature. Then (2)+(1) footprint +
> normalized form — makes the shape-match actually match. Then (3) no-mutation
> consumer rewire. Then conflict detection. Re-run `pcr_mastermix` after each.

---

## Already built [done]
- **Parser** — `nl2protocol/stage_1_pre_extraction/initial_state.py`:
  `parse_initial_state → InitialStateSheet` (`cells`, `errors`, `wells_by_label()`,
  `substance_index()`). Tests: `tests/test_initial_state.py` (9).
- **Apply-oracle** — `_overlay_initial_state` (pre-modal) + `_append_initial_state`
  (post-apply) in `pipeline.py`; `run_pipeline(initial_state=...)` param. Tests:
  `tests/test_initial_state_reconcile.py` (4). This is Step 4 of the flow.

## New work

### A. Instruction initial-well footprint [new]
`pipeline.py` helper `_instruction_initial_wells(spec) -> (wells, substances)`:
- `wells: {nickname: set(well)}` — union of (a) wells in `spec.initial_contents`
  grouped by labware nickname, and (b) source-only wells from
  `_infer_source_containers(spec)`. **Destination-only wells excluded.**
- `substances: {(nickname, well): substance}` from the same two sources.
Both `spec.initial_contents` and `_infer_source_containers` are available before
labware resolution, so this can run early.

### B. Shape-match [new] — pure function, fully testable
New file `nl2protocol/stage_1_pre_extraction/shape_match.py`:
`match_by_shape(wells, substances, oracle) -> ShapeMatchResult`
- `ShapeMatchResult(bindings: dict[nick,label], conflicts: list[str], ambiguous: list[nick])`.
- Candidates `C(n) = { c : I(n) ⊆ E(c) }`, where `E = oracle.wells_by_label()`;
  skip nicknames with empty `I(n)` (no discriminating power).
- Substance soft-filter (lenient normalize) drops a candidate whose substance
  clearly contradicts the instruction's on a shared well; records the conflict.
- Empty `C(n)` for a non-empty `I(n)` → conflict.
- One-to-one assignment by constraint propagation: assign singletons, remove the
  taken label, repeat. Leftovers with options → `ambiguous`; leftovers whose
  options are all taken → contention conflict. Conservative: only forced bindings.
Tests `tests/test_shape_match.py`: unique subset match; tie broken by substance;
tie unbroken → ambiguous; two nicknames contend → propagation/conflict; empty
candidate → conflict; empty footprint skipped.

### C. Resolver consumes hints [new]
`nl2protocol/extraction/resolver.py`:
- `LabwareMatcher` accepts `external_hints: dict[description,label]` (default None).
- In `_resolve_one`, after `_type_filter` survivors:
  - hint present AND in survivors → return `("deterministic", [hint])` — the oracle
    confirms/narrows, **replacing the LLM branch**.
  - hint present but NOT in survivors → record a conflict (geometry/type ruled it
    out) and fall through to normal dispatch — **never override geometry**.
- Thread `external_hints` through `_EarlyLabwareResolver` / `suggest`.

### D. Pipeline wiring (Steps 2–3 order) [new]
In `run_pipeline`, before labware resolution (~1604): if `initial_state`, run A → B,
pass `bindings` as `external_hints` into the resolver, collect conflicts. Overlay/
append (Step 4) stay where they are — by then the mapping already reflects the hint.

### E. Conflict surfacing (Step 5) [new]
Emit shape-match + reconciliation conflicts through the existing warning/`StageEvent`
channel so the report and the confirmation modal show them. Three kinds:
"not in initial state", substance contradiction, "row unused".

### F. Plumbing — the fuel line [new]
- `server/app.py` `/start` (~721): accept optional `initial_state_b64` → base64
  decode → `parse_initial_state(bytes, config)` → thread `InitialStateSheet` through
  `_run_pipeline` → `run_pipeline(initial_state=...)`. Surface parser errors.
- Browser template posting to `/start`: add a `.xlsx` file input → base64 → include
  `initial_state_b64` in the body.

### G. Dependency [new]
Add `openpyxl` to `[project] dependencies` in `pyproject.toml`.

## Deferred (Phase 2+)
- Reference resolution: substance-only step references → well via
  `oracle.substance_index()`.
- Dedicated provenance source `initial_state_sheet` (today reuses `inferred`).
- Catalog-driven config generation.

## Verification
- Unit: shape-match (B), parser + reconcile (done).
- Integration: `run_pipeline` with oracle + instruction → bindings drive the
  resolver, contents become authoritative, conflicts surface.
- Browser E2E: upload a real `.xlsx` live and watch volumes/labels reflect it.

## Order of implementation
B (pure) → A → C → D → E → F → G. Each verified before the next.
