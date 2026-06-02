# ADR-0015: Prune action-irrelevant fields on ExtractedStep after parse

**Status:** Accepted
**Date:** 2026-06-02

## Context

`ExtractedStep` (in `nl2protocol/models/spec.py`) is a single Pydantic class used for every action the LLM extracts — `transfer`, `mix`, `set_temperature`, `delay`, `comment`, every module action, etc. All action-specific fields are typed `Optional` so the LLM has one consistent JSON shape to fill:

```python
class ExtractedStep(BaseModel):
    action: ActionType
    substance: Optional[ProvenancedString] = None
    volume:    Optional[ProvenancedVolume] = None
    temperature: Optional[ProvenancedTemperature] = None
    duration:  Optional[ProvenancedDuration] = None
    source:      Optional[LocationRef] = None
    destination: Optional[LocationRef] = None
    note:        Optional[str] = None
    post_actions: Optional[List[PostAction]] = None
    replicates:  Optional[int] = None
    ...
```

The final command layer (`nl2protocol/models/schema.py`) is the opposite — one tight class per action, fields mirror the Opentrons API surface. `SetTemperature(module, celsius)`. `Delay(seconds, minutes)`. `Comment(message)`. No source, no destination, no leftover slots.

The mismatch surfaces as silent data passing through the pipeline. Concrete instance from the Western Blot smoke run (2026-06-02):

- LLM extracts `step 5 = ExtractedStep(action="set_temperature", temperature=ProvenancedTemperature(value=95.0, ...), destination=LocationRef(description="temperature module", description_provenance={cited_text: "temperature module", ...}))`.
- Pydantic accepts it. `destination` is optional, a LocationRef with no wells is a legal shape per the existing validator (`require_wells_provenance_when_wells_present` only fires when wells are set).
- `LabwareMatcher.suggest()` walks `step.source` and `step.destination` for every step regardless of `action` (`extraction/resolver.py:440-445`) and treats `"temperature module"` as a labware description that needs a config match.
- The matcher returns `suggested_label=None` with all four `config.labware` keys as candidates — `temp_mod` lives in `config.modules` so it's never offered.
- The user sees a row in the assignments modal for `"temperature module"` that has no good answer. They pick something arbitrary (in the smoke run: `dest_block`, which happens to sit on the temp module via `on_module`).
- `schema_builder.py:782-805` then builds `SetTemperature(module="temp_mod", celsius=95.0)` by reading `config["modules"]` directly. The user's pick from the modal has zero effect on the generated script.

The destination on `set_temperature` is dead weight that the LLM produced, that the type system accepted, and that the labware matcher treated as real. Same pattern is latent on every action whose final command consumes a subset of `ExtractedStep`'s fields.

## Decision

Keep `ExtractedStep` as a permissive one-class model and add a single `@model_validator(mode='after')` that nulls fields outside the action's documented argument set. The validator is the single source of truth for the per-action keep-set; downstream consumers continue to walk `step.source` / `step.destination` polymorphically and see `None` for actions where the field is irrelevant.

The scrubbed values are recorded in a new `pruned_fields: List[PrunedFieldRecord]` field on the same step. This preserves the LLM's mistake for later analysis (and `state_log` serialization) without surfacing it as a runtime warning.

## Keep matrix

Derived from the command classes in `models/schema.py` plus the fields `extraction/schema_builder.py` actually reads off an `ExtractedStep` when building each command.

| Action | Kept fields |
|---|---|
| `transfer` | volume, substance, source, destination, post_actions, replicates |
| `distribute` | volume, substance, source, destination, post_actions, replicates |
| `consolidate` | volume, substance, source, destination, post_actions, replicates |
| `serial_dilution` | volume, substance, source, destination, post_actions, replicates |
| `mix` | volume, substance, destination |
| `aspirate` | volume, substance, source |
| `dispense` | volume, substance, destination |
| `blow_out` | destination |
| `touch_tip` | destination |
| `delay` | duration, note |
| `pause` | duration, note, substance |
| `comment` | note, substance |
| `set_temperature` | temperature |
| `wait_for_temperature` | temperature |
| `engage_magnets` | ∅ |
| `disengage_magnets` | ∅ |
| `deactivate` | ∅ |

Prunable set: `{substance, volume, temperature, duration, source, destination, post_actions, replicates, note}`. For each action, every prunable field outside the keep-set gets nulled.

### Principle: matrix = "fields any codebase consumer reads for this action," not "fields the final command takes"

The schema-layer command classes (`models/schema.py`) are the starting point — the keep-set must include every field the final command consumes, else the pipeline breaks. But several other consumers read `ExtractedStep` fields per-action that the schema-layer command doesn't take:

- **Completeness validator** (`spec.py:969-996`) uses `step.substance.value` as a UX hint in error messages across the `liquid_actions` set (`transfer`, `distribute`, `consolidate`, `aspirate`, `dispense`, `mix`, `serial_dilution`). Pruning substance on those breaks error-message quality.
- **schema_builder describe-step** (`schema_builder.py:55-58`) reads `volume`, `temperature`, `duration`, `substance` to produce human-readable step descriptions for any action; each access is guarded with `if step.X` so pruning is safe — falls through to empty string.
- **schema_builder pause/comment** (`schema_builder.py:730,736`) uses `step.substance.value` as a fallback for the command message when `step.note` is null.

The matrix below reflects these: a field appears in the keep-set if any codebase path uses it for that action — including UX-hint paths, not just the final command's parameter list.

Notes on a few non-obvious entries:

- `mix` keeps `substance` because the completeness validator's `liquid_actions` set includes mix and uses `step.substance.value` for "missing volume for 'buffer'" hints. The schema-layer `Mix(labware, well, volume, repetitions)` doesn't take substance, but pruning it would silently regress error messages.
- `mix` drops `source`: the schema-layer command takes one well only, and the extraction convention treats `step.destination` as the mix target. `source` on `mix` is unused everywhere.
- `pause` / `comment` keep `substance` because `schema_builder` falls back to `step.substance.value` as the message when `step.note` is null (`schema_builder.py:730,736`). Not ideal — a future cleanup would route the message through a dedicated field — but pruning it today would regress existing pause-from-substance behavior.
- Module actions (`engage_magnets`, `disengage_magnets`, `deactivate`) keep nothing from `ExtractedStep`. The module label is resolved at compile time from `config["modules"]`; no LLM-extracted field survives to the final command.

## Signal preservation

New types in `nl2protocol/models/spec.py`:

```python
class PrunedFieldRecord(BaseModel):
    field_name: str   # e.g. "destination"
    value: Any         # the original sub-model the LLM filled (LocationRef, ProvenancedVolume, etc.)

class ExtractedStep(BaseModel):
    ...
    pruned_fields: List[PrunedFieldRecord] = Field(default_factory=list, description=(
        "Append-only record of LLM-filled fields the action doesn't consume, "
        "scrubbed by the prune_irrelevant_fields_by_action validator. Preserved "
        "for later analysis of LLM extraction errors. Not surfaced to the user."
    ))
```

The validator walks `_PRUNABLE_FIELDS`, finds anything non-None outside the action's keep-set, appends a `PrunedFieldRecord` to `self.pruned_fields`, and sets the original attr to `None`. The result lands in `state_log["stage_2_extraction"]` via the existing `spec.model_dump()` call in `pipeline.py`, so the LLM's mistake is recoverable from any run's state log.

## Apply path / consumer impact

No downstream changes. Every existing reader of `step.source` / `step.destination` already guards on `if ref is None` (see `extraction/resolver.py:441`, `pipeline.py:1152`, the orchestrator's gap detectors). The pruner only narrows what those readers see — it never widens — so the only behavioral change is "fields that the action doesn't consume are now reliably None instead of sometimes containing LLM-imagined values."

Specifically:
- **Labware matcher** stops seeing `"temperature module"` because `step.destination` on a `set_temperature` step is now `None`. The `_collect_unique_descriptions` walk skips it via the existing `if ref is None or ref.description in seen` guard.
- **Gap detectors** (`MissingFieldsDetector`, `ProvenanceWarningDetector`) operate on the post-pruner spec. A `set_temperature` step with `temperature=None` still raises "missing temperature target" via the existing `validate_step_required_fields`. A `set_temperature` step with `destination=None` raises nothing — that's the desired state.
- **HTML report** renders whatever `ExtractedStep` carries; pruned fields disappear from the render. The `pruned_fields` list is available to renderers that want to surface it (none today).

The pruner runs at parse time (`mode='after'`), which means any code path that uses `ExtractedStep.model_validate(...)` gets the cleanup automatically. Code that constructs steps via `ExtractedStep.model_construct(...)` (skips validation) bypasses the pruner — that's the existing escape hatch for tests that need to assert on raw shapes.

## Companion change: extraction prompt

`nl2protocol/extraction/prompts.py:280` TEMPERATURE STEPS section gets a one-line addition forbidding source/destination on temperature steps. Reduces the rate at which the pruner needs to fire by closing the loophole upstream. Defense-in-depth: the pruner remains the guarantee.

## Alternatives considered

**(A) Discriminated union — one Pydantic class per action.** The type-correct version. `SetTemperatureStep` literally has no `destination` field; `pydantic.ValidationError` on the LLM's output if it tries to fill one. Rejected for now because:
- The bug surface today is one known instance (Issue B from the labware-resolver handover). One example justifies a ~15 LOC validator; it doesn't justify restructuring `ExtractedStep` plus every consumer that reads `step.source` / `step.destination` polymorphically (~30 callsites across the orchestrator, resolver, schema_builder, validators, reporters).
- The LLM-side cost is real: the JSON schema handed to Claude becomes `oneOf[15+ shapes]` instead of one. Anthropic's structured output supports it, but prompt complexity goes up and the extraction prompt's per-action examples need to be re-targeted at the discriminated members.
- Migration cost: existing `pipeline_state_*.json` snapshots wouldn't deserialize against the new shape. We'd need a one-shot upgrader or a tolerant-reader compat layer.

The right call if we see this pattern recur (LLM filling more nonsense fields on other actions) is to revisit. "Three strikes and refactor" — one bug instance is a fluke; three is a pattern.

**(B) Per-consumer filtering — every downstream reader checks the action before walking the field.** E.g., the labware matcher gets a "skip non-pipetting actions" guard. Rejected because:
- The keep-set rule lives in N+ places instead of one. Adding a new module action means updating every consumer.
- The implicit type contract ("step.destination is meaningful for these actions") never gets written down.
- It's the "every caller knows the rule" smell — load-bearing convention without a single source.

**(C) Status quo — leave it.** Rejected because the labware matcher would keep producing phantom modal rows for any LLM-imagined module description, every smoke test would surface the same kind of bug, and the deferred Issue B never gets closed.

**(D) Validate-and-raise instead of prune-and-record.** Have the validator raise `ValidationError` when the LLM fills a field outside the keep-set. Rejected because LLM extraction is the primary input; rejecting valid-shaped output for cosmetic reasons forces a re-run of the LLM call. The pruner's "scrub and proceed" is the right ratio of "tolerate the producer's mistakes, don't propagate them downstream."

## Tradeoffs

**Type signature stays loose.** `source: Optional[LocationRef]` is still the type. A reader doing `step.source.description` outside an `if step.source is not None` guard is still a runtime error waiting to happen. Static analysis doesn't help us here. The discriminated-union refactor (A) is the only fix for that — accepted cost of this ADR.

**Pruner is a load-bearing runtime invariant.** Every consumer that assumes "set_temperature steps have destination=None" trusts the pruner ran. Code paths that construct steps via `model_construct` bypass it (existing tests do this). New downstream code that depends on the post-prune shape needs to either be confident it's seeing post-validation steps, or explicitly call a `prune(step)` helper.

**Adds one new schema field (`pruned_fields`) to every step in serialized output.** Most steps will have `pruned_fields: []` — empty list. State log size grows trivially; HTML report ignores the field unless we wire it in.

**`pause` / `comment` keep `substance`.** Documented above as a hack. The real fix is "schema_builder routes the message through a dedicated field." That's out of scope for this ADR and will be a follow-up if anyone trips on it.

**Doesn't catch shape errors INSIDE a kept field.** The pruner is whole-field granularity. If the LLM puts a `LocationRef` in `step.destination` for a `transfer` step but the LocationRef itself is malformed (description points at a module, say), the pruner keeps it as-is and the labware matcher still has to handle it. The pruner solves "wrong field," not "wrong value in a right field."

## References

- ADR-0007 — schema enforcement layers (the layered-validation model this fits into)
- ADR-0008 — unified gap resolution (downstream consumers that walk `step.source`/`step.destination`)
- `nl2protocol/models/spec.py` — `ExtractedStep`, new `PrunedFieldRecord` + `_ACTION_KEEPS` + validator
- `nl2protocol/models/schema.py` — final command classes that determine the keep matrix
- `nl2protocol/extraction/schema_builder.py` — `ExtractedStep` → final command translation; what's actually read per action
- `nl2protocol/extraction/prompts.py:280` — companion prompt change
- `nl2protocol/extraction/resolver.py:440-445` — the labware-matcher walk that surfaced the bug
- `output/pipeline_state_20260602_111912.json` — Western Blot smoke run that exposed the issue
