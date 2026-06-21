# CodeRabbit follow-ups (deferred, address post-merge)

Findings surfaced by CodeRabbit on merged PRs that we chose to merge first and
fix later. Full comment bodies + suggested diffs live permanently on the PRs
themselves: `gh api repos/aryan01104/nl2protocol/pulls/<N>/comments`.

## PR #20 — Stream Stage-2 reasoning (merged into main)

All three are in `nl2protocol/extraction/extractor.py`, in the streaming /
spec-repair logic. All rated 🟠 Major.

1. **Don't fully suppress `reasoning_delta` emission failures** (~`_emit_reasoning`, orig lines 242–243).
   `except Exception: pass` swallows all reporter errors silently — if the live
   reporter breaks, Stage-2 reasoning vanishes with no signal. Fix: keep it
   non-blocking but log once (e.g. a `_reasoning_emit_failed` latch + one
   `stderr` line). Ruff also flags the bare `try/except/pass`.

2. **Skip stale `steps[*].order` violations after renumbering** (`_repair_round`, ~line 275).
   `_repair_round()` fixes `order` first, then still consumes the pre-fix
   validation errors — causing unnecessary re-asks that can rewrite
   otherwise-valid entries. Fix: re-validate (or drop order-errors) after the
   order fix before deciding what to re-ask.

3. **Enforce JSON object shape before patching repaired entries** (`_reask_element`, ~line 312).
   `json.loads()` accepts arrays/scalars; writing those into
   `data[container][index]` can corrupt the structure and waste repair
   attempts. Fix: assert the parsed value is a dict (object) before patching.

## PR #22 — derived volumes / disposal / mix cycle-count (merged into main)

Fixed before merge: the critical `pipeline.py` import (folded restructure in)
and the Ruff `×`→`x`. The 6 below were deferred — all 🟠 Major except the
last cluster note. They harden edge cases on the new disposal/removal/mix
features (happy path works).

1. **Boundary-safe discard matching** (`constants.py:35`, ~line 49).
   `is_discard_description` uses substring `token in t`, so a description like
   "washington buffer" matches "wash" → false trash routing. Use word-boundary
   matching.

2. **Gate discard auto-routing by role** (`resolver.py:381`).
   Routes ANY discard-like description to `TRASH_LABEL` without checking it's a
   *destination*. A source labware named like a discard gets sent to trash.
   Gate to destination role.

3. **Over-broad "remove all" match** (`suggesters.py:488`, ~487).
   Generic tokens ("wash", "ethanol") can misclassify reagent *additions* as
   "remove all". Tighten the token set / require removal verbs.

4. **Guard `repetitions` against non-positive values** (`spec.py:782`).
   No positivity check, so `0`/negative bypass the `DEFAULT_MIX_REPS` fallback.
   Add `gt=0` or coercion (mirrors the `replicates` coercion pattern).

5. **Discard well-bypass should be true-fixed-trash only** (`spec.py:1099`).
   The well-requirement skip also fires on discard-*description* refs even when
   not resolved to the fixed trash. Limit to true `TRASH_LABEL` destinations.

6. **Don't auto-pass discard/trash labware checks on source refs** (`constraints.py:528`).
   The pass-path bypasses labware-not-found for BOTH roles; a bogus *source*
   labware named like a discard slips through. Restrict to destination.
