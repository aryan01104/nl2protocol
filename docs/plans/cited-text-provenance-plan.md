# Implementation plan: cited_text provenance verification

**Status:** Deferred. ADR-0003 captures the decision; this file captures the concrete edit plan for when implementation is greenlit.
**Prerequisite:** Build an eval set of 20–50 hand-labeled instructions before starting, so before/after verifier behavior can be measured. Without it, this change is made on faith.

---

## TL;DR

- One schema change: add `cited_text: Optional[str]` to `Provenance` + a validator requiring it when `source == "instruction"`.
- One prompt rewrite: instruct the LLM to emit the verbatim quote; remove the now-misleading "explicit_volumes auto-populated" line.
- One verifier rewrite: `_verify_claimed_instruction_provenance` becomes ~30 lines of substring checks.
- Five regex helpers deleted. One spec field (`explicit_volumes`) deleted. One pipeline log line updated.
- Two test files touched (`test_constraints.py`, `test_failure_modes.py`).
- All four field-level `ProvenancedX` types covered automatically (they share the `Provenance` class). `CompositionProvenance` intentionally out of scope.

---

## 1. Schema change (the only structural edit)

In `nl2protocol/models/spec.py`, the `Provenance` class:

- Add `cited_text: Optional[str] = Field(None, description=...)` after `confidence`.
- Add a `model_validator(mode='after')` that raises if `source == "instruction"` and `cited_text` is `None` or empty.
- For non-instruction sources:
  - `source="config"` — `cited_text` could optionally hold a config key path (e.g., `"labware.tube_rack"`). Same idea, different ground truth. Defer to a follow-up if config crosschecking is reintroduced.
  - `source="domain_default"` and `source="inferred"` — `cited_text` stays `None`. `reason` carries the human-readable justification.

Coverage check (which provenanced objects this affects):

| Object | Has `Provenance`? | Covered? |
|---|---|---|
| `ProvenancedVolume` | Yes | Yes |
| `ProvenancedDuration` | Yes | Yes |
| `ProvenancedTemperature` | Yes | Yes |
| `ProvenancedString` | Yes | Yes |
| `LocationRef.provenance` (labware + wells) | Yes | Yes |
| `PostAction.volume` (uses `ProvenancedVolume`) | Yes | Yes (transitively) |
| `CompositionProvenance` (step-level) | No — its own class | No, by design. Multi-source synthesis can't be one quote. Composition keeps `justification` (prose) + `grounding` (sources) + `confidence`. |

---

## 2. Prompt changes (`nl2protocol/extraction/prompts.py`)

| Line | Current text | Change |
|---|---|---|
| 33 | `- Leave "explicit_volumes" empty — it will be populated automatically` | Delete. The field goes away. |
| 35–48 | The PROVENANCE block | Add a `cited_text` paragraph between `reason` and `confidence` instructions: *"For source='instruction', also include `cited_text` — the verbatim substring from the instruction (no paraphrasing, no added words). The verifier checks that this exact substring appears in the instruction and that the value is contained within it. For other sources, leave `cited_text` null."* |
| 50–60 | Calibration examples | Update each example to include `cited_text`: `provenance: {{source: "instruction", reason: "...", confidence: 1.0, cited_text: "100uL"}}`. |
| 61–67 | Anti-patterns | Add: *"Cited text must be a verbatim substring. WRONG: cited_text='one hundred microliters' when text says '100 uL'. RIGHT: cited_text='100 uL'."* |
| 69–82 | COMPOSITION PROVENANCE | Leave alone. Add one clarifying sentence: *"Composition provenance does NOT have a cited_text field — multi-source synthesis can't be one quote."* |

---

## 3. Functions to delete (`nl2protocol/extraction/extractor.py`)

Every helper exists purely to support the regex-based verifier. Each is replaceable by a substring check on `cited_text`.

| Function | Lines (current) | Action |
|---|---|---|
| `_extract_volumes_from_text` | 237–241 | Delete |
| `_extract_wells_from_text` | 327–386 | Delete |
| `_extract_durations_from_text` | 388–407 | Delete |
| `_extract_non_volume_numbers` | 409–420 | Delete |
| `_expand_well_range` | helper used only by `_extract_wells_from_text` | Delete |
| `_verify_claimed_instruction_provenance` | 434–550 | Rewrite to ~30 lines (see §4) |

Two more sites that populate `explicit_volumes` via regex:

- Line 162–163 (in `extract`): delete
- Line 290 (in `refine`): delete

The cross-check block (lines 532–548) inside `_verify_claimed_instruction_provenance` also goes away — it depended on `explicit_volumes`.

---

## 4. Rewritten verifier (replaces lines 434–550)

```python
def _verify_claimed_instruction_provenance(self, spec: ProtocolSpec, instruction: str) -> List[dict]:
    """Verify all source='instruction' claims via cited_text substring check."""
    warnings = []
    text_lower = instruction.lower()

    def check(step_order, field_name, value, prov):
        if not prov or prov.source != "instruction":
            return
        quote = prov.cited_text
        if not quote:
            warnings.append(self._warn(step_order, field_name, str(value),
                "instruction", "fabrication",
                f"Step {step_order} {field_name}: source='instruction' but cited_text missing"))
            return
        if quote.lower() not in text_lower:
            warnings.append(self._warn(step_order, field_name, str(value),
                "instruction", "fabrication",
                f"Step {step_order} {field_name}: cited_text {quote!r} not found in instruction"))
            return
        if not _value_in_quote(value, quote):
            warnings.append(self._warn(step_order, field_name, str(value),
                "instruction", "fabrication",
                f"Step {step_order} {field_name}: value {value} not present in cited_text {quote!r}"))

    for step in spec.steps:
        if step.volume:      check(step.order, "volume",      step.volume.value,      step.volume.provenance)
        if step.substance:   check(step.order, "substance",   step.substance.value,   step.substance.provenance)
        if step.duration:    check(step.order, "duration",    step.duration.value,    step.duration.provenance)
        if step.temperature: check(step.order, "temperature", step.temperature.value, step.temperature.provenance)
        for ref, role in [(step.source, "source"), (step.destination, "destination")]:
            if ref and ref.provenance:
                check(step.order, f"{role} labware", ref.description, ref.provenance)
                for w in (ref.wells or ([ref.well] if ref.well else [])):
                    check(step.order, f"{role} well", w, ref.provenance)
        if step.post_actions:
            for pa in step.post_actions:
                if pa.volume:
                    check(step.order, f"{pa.action} volume", pa.volume.value, pa.volume.provenance)

    return warnings
```

Plus a tiny helper `_value_in_quote(value, quote)` — handles numeric formatting (`100` vs `100.0`) and case for strings.

That replaces ~115 lines with ~30. Same coverage. Stronger guarantee (positional grounding, not just "value appears somewhere in text").

---

## 5. Other files touched

| File | Change | Why |
|---|---|---|
| `nl2protocol/models/spec.py` line 269 | Delete `explicit_volumes` field on `ProtocolSpec`. | Only existed for the cross-check and pipeline log. |
| `nl2protocol/pipeline.py` line 981–982 | Delete the `Locked volumes` log block. | Field no longer exists. |
| `nl2protocol/extraction/extractor.py` line 919–920 | Delete the `Locked volumes` line in the formatter. | Same reason. |
| `tests/test_failure_modes.py` lines 416–418 | Delete the `explicit_volumes` assertions. | Field is gone. |
| `tests/test_constraints.py` lines 79, 351–403, 521, 544, 569, 592 | Remove `explicit_volumes=[...]` from the mock constructions, delete `test_explicit_volumes_must_appear_in_schema`. | The "explicit_volumes must appear in schema" test was checking dead behavior — the constraint checker doesn't reference this field in production. |
| `tests/test_tip_strategy.py` line 51, `tests/test_failure_modes.py` line 53 | Remove `"explicit_volumes": []` from mock dicts. | Field is gone. |
| All test mock factories (`_prov`, `_volume`, etc.) | Add `cited_text` to `_prov()` for any mock that uses `source="instruction"`, otherwise the validator will reject. | Required by new schema validator. |

---

## 6. Migration order

1. Land schema change (`cited_text` on `Provenance` + validator). All existing tests will fail until mocks are updated — that's the canary that you've found every call site.
2. Update test mocks to include `cited_text` for instruction-sourced provenance.
3. Update prompt with new examples.
4. Rewrite `_verify_claimed_instruction_provenance` to use `cited_text`.
5. Delete the regex helpers and `explicit_volumes` population sites.
6. Delete `explicit_volumes` field from `ProtocolSpec` and clean up the two log/format references.
7. Smoke-test end-to-end with a real instruction or two.
8. Run the eval set (built as a prerequisite) before and after; confirm verifier precision/recall improved.

Each step is independently revertable. Step 1 is the only one that breaks the build until step 2 lands — ideally land 1+2 in the same commit.

---

## 7. Risks and what to watch for

- **LLM omits `cited_text`.** Validator rejects the spec with a `ValidationError`, surfacing as an extraction failure. Desired behavior — loud, not silent. Worth adding a retry-with-feedback loop later (similar to `refine`); for now, surface and fail.
- **LLM paraphrases instead of quoting.** Verifier flags as fabrication. Right outcome, but expect initial flake on edge phrasing ("hundred microliters" vs "100 uL"). The prompt anti-pattern example reduces this. Eval set required to confirm rate is acceptable.
- **Whitespace differences.** `"100 uL"` vs `"100uL"`. Verifier uses `quote.lower() in instruction.lower()` which is character-sensitive. Normalize both sides (collapse runs of whitespace) in the substring check — one extra line, prevents false fabrications on innocuous spacing.
- **Numeric format differences.** `"100"` vs `"100.0"`. Handled in `_value_in_quote`.
- **Duplicate values in instruction.** "Mix 100uL with 100uL" — both refer to the same number. The substring check passes for both; this is fine (we're not disambiguating occurrences).
- **Prompt regression.** Adding `cited_text` instructions could subtly degrade other extraction behavior. Eval set required to confirm.

---

## 8. Why this is deferred

ADR-0003 is "Waiting" because the change cannot be safely validated by smoke testing alone. The LLM's quote-emission reliability on this project's instruction style is an empirical question, and without an eval set, "did this make things better?" cannot be answered. The eval set is the gating prerequisite.

The change is a positive expected-value play (~3:2 reward:risk), but not urgent — the current verifier works adequately for current scale, and the bugs it has (regex coverage gaps, substring collisions) are rare in the project's typical instruction style. Pick this back up after the eval-set work.
