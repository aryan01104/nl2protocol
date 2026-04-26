# Task 6: Threshold-Based User Confirmation UX

## Problem

Before Task 6, the confirmation step dumped the entire ProtocolSpec and asked a single yes/no: "Proceed with this specification?" Every parameter got the same visual weight — a volume the user literally typed ("100uL") looked the same as a volume the LLM guessed ("probably 50uL for Bradford"). The user had to read every field to find the ones that actually needed attention.

This is a known UX failure mode: when you ask someone to review 20 items and 16 are obviously correct, they learn to skim and rubber-stamp. The 4 uncertain items get the same cursory glance as the 16 certain ones.

## What Task 6 does

Task 6 uses the provenance metadata from Task 4 and the verification results from Task 5 to route parameters into three buckets:

1. **Auto-accepted** (shown as a count, not individually):
   - `source="instruction"` or `source="config"` with `confidence >= threshold`
   - These passed Task 5's verification — the LLM's claim about where the value came from was confirmed programmatically

2. **Confirmation queue** (shown individually, interactive per-item):
   - `source="domain_default"` with `confidence < threshold`
   - `source="inferred"` (any confidence)
   - These are values the LLM admitted it wasn't fully sure about

3. **Fabrication errors** (block the pipeline):
   - Task 5 warnings with `severity="fabrication"`
   - The LLM claimed a value came from the instruction or config, but it didn't
   - These can't be trusted or confirmed — the pipeline halts

The default threshold is 0.7. A `--full-confirmation` flag bypasses routing and shows everything with full provenance detail.

## Code changes

### `cli.py`

Two new argparse flags:
- `--full-confirmation` (boolean): shows all parameters with provenance tags, bypasses auto-accept
- `--confirmation-threshold` (float, default 0.7): cutoff for auto-accepting instruction/config values

Both are threaded through to `run_pipeline()`.

### `app.py`

**`run_pipeline` signature** — added `full_confirmation: bool` and `confirmation_threshold: float` parameters.

**Confirmation call site** (after Stage 4, before Stage 5) — replaces the old single yes/no with:
1. Display `format_for_confirmation(spec, provenance_warnings, threshold, full)`
2. If any `fabrication` warnings exist, halt immediately with error
3. If `confirmable` items exist and not in full mode, run `_confirm_provenance_items()`
4. In full mode or no confirmable items, fall back to simple yes/no

**`_confirm_provenance_items(spec, confirmable)`** — interactive per-item flow. For each uncertain item, shows:
- The step number, field name, and current value
- The provenance source and severity
- The LLM's reason string (looked up via `_find_provenance_reason`)

User options per item:
- Enter/y: accept as-is
- e: type a replacement value
- s: accept all remaining items
- q: abort

**`_apply_provenance_edits(spec, edits)`** — applies user edits from the confirmation flow. When the user provides a new value:
- Updates the field's `.value`
- Sets provenance to `source="instruction"`, `confidence=1.0` (the user explicitly stated it)
- Sets reason to `"User-edited during confirmation"`

This means user-edited values are treated as ground truth by downstream stages (constraint checking, simulator retry in Task 8 won't override them).

### `extractor.py`

**`_find_provenance_reason(step, field_name)`** — module-level helper that looks up the provenance `reason` string for a given field on a step. Handles direct fields (volume, substance, duration), composition provenance, location refs (source/destination), and post-action volumes via string prefix matching on the field name.

**`_format_step_line(step)`** — extracted from the old `format_for_confirmation`. Builds the one-line step summary ("1. TRANSFER 100uL of sample from source_plate well A1 to dest_plate well B1").

**`format_for_confirmation(spec, provenance_warnings, threshold, full)`** — rewritten. Two modes:

*Full mode* (`--full-confirmation`): shows every step with every field's provenance source, confidence, exactness, and the composition justification. This is for building trust in the system or debugging extraction.

*Threshold mode* (default): shows a step overview for context, a count of auto-accepted parameters, then the numbered confirmation queue with reasons.

## How it improves outcomes

The core leverage: user attention is finite and the confirmation step is where it's spent. By filtering to only the uncertain fields, the user's attention is concentrated on the items where it has the most impact. A detailed instruction like "transfer 100uL from A1-A8 to B1-B8" might produce zero confirmation items. A vague instruction like "do a Bradford assay" might produce 10+ — but those genuinely need review.

The fabrication blocking is the safety-critical part. If the LLM says "this volume came from your instruction" but the volume doesn't appear in the instruction text, that's a hallucination with a cover story. Letting the user "confirm" it normalizes the lie. Halting forces a re-run, which is the correct response.

## Known deficiencies and open questions

### 1. Threshold is not calibrated

The 0.7 default is a guess. LLM confidence scores are rank-usable but not absolute-calibrated (Tian et al., EMNLP 2023). A value at 0.71 auto-accepts silently; a value at 0.69 goes to the queue. There's no empirical basis for 0.7 being the right cutoff. This needs tuning against the 13 test cases — run them, collect the confidence distributions, see where the errors cluster.

The `--confirmation-threshold` flag is the escape valve, and `--full-confirmation` is the nuclear option. But most users won't tune a threshold. If the default is wrong, the UX is wrong for everyone.

### 2. Fabrication halts are too aggressive

When a fabrication is detected, the pipeline halts with "Re-run with a more explicit instruction." There's no recovery path. The user can't say "I know the volume isn't in my text, I'm OK with the LLM's guess." This is a deliberate design choice (fabricated provenance = broken trust), but it means a single false positive from the regex extractors in Task 5 kills the entire run.

For example: the user writes "add about a hundred microliters." Task 5's `_extract_volumes_from_text` regex won't match "a hundred" (it looks for digits). The LLM correctly interprets it as 100uL and tags it `source="instruction"`. The verification fails because the regex can't find "100" in the text. The pipeline halts. The user did nothing wrong.

Possible fix: allow the user to override fabrication errors interactively, but flag them prominently. Or improve the regex to handle spelled-out numbers. Not implemented yet.

### 3. Edit flow is type-unsafe

`_apply_provenance_edits` does `float(new_val)` with a bare try/except ValueError. If the user types "fifty" instead of "50", it silently does nothing — no error message, no retry. The edit appears to succeed from the user's perspective because the flow moves on.

More broadly, edits are unvalidated — the user could type a volume of 99999uL that exceeds pipette capacity. The constraint checker in Stage 4 would catch this, but we don't re-run Stage 4 after edits. The plan says "on reject or edit, update the spec and re-run from Stage 4 onward," but this isn't implemented yet. Edits are applied and the pipeline proceeds to Stage 5.

### 4. No "back" navigation

`_confirm_labware_assignments` has a `(b)ack` option. `_confirm_provenance_items` doesn't. If the user accepts item 3 and then realizes at item 5 that they should have edited item 3, they have to quit and re-run. Not hard to add, but currently missing.

### 5. Composition provenance in the confirmation queue is not actionable

When `_flag_uncertain_claims` flags a step's composition provenance (the step itself might be fabricated), the confirmation queue shows it. But the user's only options are accept or quit — there's no way to delete a step. Editing composition isn't meaningful either (you can't change why a step exists by typing a string). The correct UX would be "remove this step? [y/n]" but that requires spec mutation logic (reorder remaining steps, update cross-references).

### 6. auto-accepted count and confirmation queue can disagree

`format_for_confirmation` counts auto-accepted parameters by walking the spec. `_flag_uncertain_claims` (Task 5) generates the confirmable list by walking the spec independently. They use similar but not identical logic. A parameter could be counted as "auto-accepted" in the display but also appear in the confirmable list if the counting logic and the flagging logic have slightly different conditions. This would confuse the user: "it says 14 auto-accepted but it's also asking me to confirm one of them."

### 7. Non-interactive mode silently accepts everything

If `sys.stdin.isatty()` is False (CI, piped input, scripts), no confirmation happens — not even for `low_confidence` items. The confirmable items are just skipped. This is probably fine for CI, but there should at least be a log line saying "N items would need confirmation in interactive mode."
