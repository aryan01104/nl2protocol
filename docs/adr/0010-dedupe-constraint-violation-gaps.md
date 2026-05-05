# ADR-0010: Dedupe constraint-violation Gaps by `(violation_type, dedupe key)`

**Status:** Accepted (PR3b follow-up)
**Date:** 2026-05-05

## Context

ADR-0008's orchestrator emits one Gap per piece of detected work, then routes each Gap through suggester → confirmation handler → apply. This per-Gap design works cleanly when each problem is genuinely independent — a missing volume here, a fabricated cited_text there, an ambiguous labware over there. The user sees one prompt per logical issue.

The shape breaks down for one specific class of problems: **the same logical issue surfacing in N steps simultaneously**. When the live Bradford run hit Stage 3 with the new orchestrator path, the user saw five identical "[BLOCKER] Cannot resolve source ..." prompts in a row — same broken description, same answer, same five-keystroke ritual. This is not a useful user experience. It's also not what "one Gap per logical issue" means; it's "one Gap per *instance* of a logical issue."

The trigger was a real bug (PR3b ConfigLookupSuggester wrote a decorated description that the constraint check couldn't match — see commit fixing Bug 1), but even after that bug is fixed the dedupe concern stands: there are real cases where one config typo ripples into many steps' constraint violations, or one out-of-range well set is reused across multiple transfers, or one too-large volume appears in multiple places.

The orchestrator's per-Gap loop has no way to know two Gaps share a logical answer. By the time it sees them, they're already independent records. The only place that knows the underlying violations all stem from the same problem is the detector itself, where it walks the violation list and decides what to emit.

This ADR says: **deduplication is a detector responsibility, not an orchestrator one.** When a detector knows two violations have the same answer, it groups them into one Gap before emission.

## Decision

`ConstraintViolationDetector` groups ERROR-severity violations by `(violation_type, dedupe_key)` before emitting Gaps. One Gap per group. The Gap's `metadata` carries `affected_steps` (for display) and `affected_paths` (for apply). The orchestrator's `default_apply_resolution` checks for `affected_paths` and writes the user's answer to every path in the list, not just the representative `field_path`.

The dedupe key is per-violation-type. The discipline:

> **A dedupe key must include every axis that the user's fix depends on, and nothing more.**

Two violations with the same key are guaranteed to accept the same answer. Two with different keys are guaranteed to need different answers. The key shapes:

| `violation_type`        | Dedupe key                              | Reasoning                                                                                                                                                                                                                                              |
|-------------------------|------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `LABWARE_NOT_FOUND`     | `("description", description)`           | The fix is "what config labware should this description map to?" The answer is the same for every step that references the same broken description string.                                                                                            |
| `WELL_INVALID` (list)   | `("wells", labware, sorted invalid wells)` | The fix is "which valid wells should we use?" The answer depends on the labware's geometry. Two steps with the same offending wells on different labware → different valid ranges → different answers → must NOT be grouped.                          |
| `WELL_INVALID` (single) | `("well", labware, well)`                | Same reasoning, single-well variant.                                                                                                                                                                                                                   |
| `PIPETTE_CAPACITY`      | `("volume", requested_volume)`           | The fix is "add a bigger pipette to config OR reduce the volume." Same volume → same fix, regardless of which step demanded it (transfer volume vs post-action mix volume vs serial-dilution chain).                                                  |
| Other types             | `("instance", id(violation))`            | Default: never dedupe. `id()` returns a unique-per-Python-object integer, so each violation hashes uniquely. Conservative — types we haven't reasoned through stay one-Gap-per-instance until we have explicit evidence dedupe is safe and useful.    |

The orchestrator side is minimal — `default_apply_resolution` checks `gap.metadata["affected_paths"]` and dispatches to a small `_apply_at_path` helper for each path in the list. Single-path Gaps (the common case) take the same code path with one entry in the list, so there's no special-case branching downstream.

## Why this design

### Dedupe at detect time, not at present time

An alternative was: keep one Gap per step, but make `CLIConfirmationHandler` notice "the user just answered this question 30 seconds ago" and replay the previous answer. Rejected because:

1. **Cache lookup is a separate concern.** Inserting a "have I seen this Gap before?" check at the handler layer means the handler is suddenly stateful, owns deduplication policy, and has to decide how to identify equivalence. That's a lot of new responsibility for what's currently a thin "render gap → return resolution" adapter.

2. **Auto-accept paths bypass the handler.** When `confidence ≥ threshold AND kind ∉ ALWAYS_CONFIRM`, the orchestrator skips the handler entirely and applies the suggestion directly. A handler-level cache wouldn't kick in for those, leaving the user-visible win only on prompted Gaps. Detect-time dedupe is uniform: applies regardless of whether the Gap auto-accepts or surfaces.

3. **Stable IDs require detect-time work anyway.** The orchestrator's loop relies on stable Gap IDs to match "same gap as last iteration" vs "new gap surfaced this round." For dedupe to work across iterations, the detector has to produce a stable id whose hash represents the *logical* problem, not the per-step instance. That means the detector has to know the dedupe key. Once it knows the key, it might as well group at the same time.

### Keying must equal the user's fix axes — not less, not more

The temptation in keying is to pick something simple and correct most of the time. This is wrong; it produces false dupes.

The WELL_INVALID case demonstrates the failure mode. A naive key — just `sorted(invalid_wells)` — would group two violations with the same offending wells into one Gap. But "the same offending wells" doesn't imply "the same fix" if the labware geometries differ. A user clipping `["A20"]` to "A1-D6" for a 24-tube rack is a wrong answer for a 96-plate (whose valid range is "A1-H12"). False-dupe means the orchestrator writes the rack-clip answer onto a plate's wells list. The fix is a real correctness bug masquerading as a UX nicety.

The right discipline is *symmetric*: include in the key every axis the answer depends on, and nothing else. That makes "same key → same answer" a guarantee, not an approximation.

This is also why "other types" key on `id(violation)`. We haven't reasoned about dedupe for MODULE_LABWARE_MISMATCH, SLOT_CONFLICT, etc. The conservative default is no-dedupe; we add a real key when we have evidence the type benefits AND we've identified its full fix-axis set. Defaulting to dedupe-when-uncertain would risk silent false dupes for types that look superficially similar but require different answers.

### Apply propagation, not handler propagation

When the user accepts an answer to a deduped Gap, the answer needs to reach every affected step. There were two places this could live:

- **Apply path**: the orchestrator's `default_apply_resolution` reads metadata and writes to every path. One change at the resolution layer; suggesters and detector emit metadata.
- **Suggester path**: the suggester returns one Suggestion that knows about all affected paths, and `Suggestion.apply()` (a method that doesn't exist today) walks them.

Picked apply-path because it keeps Suggestions stateless and equivalent to today's shape. The change is localized: `default_apply_resolution` gains one branch (`if metadata.affected_paths`); everything else stays the same. Suggesters don't need to know about dedupe; they receive a Gap that *happens* to represent multiple steps, but the Suggestion they return is about the one logical answer. The fan-out is the orchestrator's mechanical job.

## Alternatives considered

**(A) Dedupe at the suggester layer.** Suggesters could be aware of "this Gap represents N steps" and produce one Suggestion that applies to all. Rejected because (a) it forces every existing suggester to be modified to handle a list-of-paths shape it doesn't currently know about, and (b) the dedupe is a property of the violation, not of the suggestion — putting it at the suggester layer is a category error.

**(B) Dedupe at the user-prompt layer.** As described above; rejected for stateful-handler + auto-accept-bypass + stable-id reasons.

**(C) Don't dedupe; let the user power through.** Rejected because the smoke-test pain was real and predictable. Five identical prompts is a design failure, not a UX inconvenience.

**(D) Always dedupe by `(violation_type, hash of full values dict)`.** A blanket key would dedupe more aggressively, including types we haven't reasoned about. Rejected because it risks false dupes for types whose `values` happen to overlap. The explicit per-type keying makes the dedupe contract auditable and conservative.

## Tradeoffs and known limitations

**Limitation 1: types not in the explicit list don't dedupe.** If MODULE_LABWARE_MISMATCH starts firing repeatedly in real runs, the user will see N prompts. Recovery: add an explicit key for that type with a tested case.

**Limitation 2: dedupe is per-type only.** If a single fix would resolve violations of two different types (e.g., a config edit that fixes both LABWARE_NOT_FOUND on step 3 and WELL_INVALID on step 5), they remain separate Gaps. The orchestrator surfaces them separately. This is rare enough not to justify cross-type dedupe; the user resolves each, the iteration loop re-detects, and any downstream resolution that's now redundant disappears.

**Limitation 3: per-iteration grouping only.** A Gap deduped in iteration 1 doesn't carry forward to iteration 2 if the affected_steps shifts (e.g., user fixed two of five, three remain). The detector regroups in iteration 2 over the remaining violations. This is the right behavior — same key in iteration 2 hashes to the same id, so the orchestrator's "same gap as last iteration" matching still works for the unresolved subset. But it does mean the metadata's `affected_steps` list shrinks across iterations as the user picks off subsets.

## What this is not

This ADR doesn't address:

- **InitialContentsVolumeDetector grouping**, which has a similar UX problem (per-well prompts instead of per-bucket). That's a separate dedupe surface; the right fix there is a `BatchedInitialContentsVolumeDetector` that groups by `(labware, substance)` at the well-tracking layer, not at the constraint layer. Documented as deferred in ADR-0008's PR3b appendix.
- **MissingFieldsDetector grouping**, which doesn't apply (each missing field is genuinely a separate problem with a separate answer).
- **ProvenanceWarningDetector grouping**, same.

## References

- ADR-0008 — unified gap-resolution architecture (PR3b appendix has the legacy → new mapping that exposes this dedupe gap).
- `nl2protocol/gap_resolution/detectors.py` — `ConstraintViolationDetector` + `_dedupe_key`.
- `nl2protocol/gap_resolution/orchestrator.py` — `default_apply_resolution` + `_apply_at_path`.
- `tests/test_gap_resolution_detectors.py` — `TestConstraintViolationDetector` includes the dedupe and false-dupe tests this ADR specifies.
