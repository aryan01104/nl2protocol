# ADR-0017: Completeness gaps are emitted structured, not parsed from prose

**Status:** Implemented (commit `9490287`)
**Date:** 2026-06-27

**Completes ADR-0008 / ADR-0014:** the unified gap loop already moved the
`ProvenanceWarningDetector` to read an explicit `field_path` the verifier
emits (`detectors.py:_warning_to_field_path` — "the verifier now emits
`field_path` directly because it already knows the spec address"). The
`MissingFieldsDetector` was never given the same treatment; this ADR finishes
that migration.

## Context

Missing-field detection round-tripped a field address through an English
sentence:

1. `CompleteProtocolSpec.validate_completeness` (`models/spec.py`) walks the
   steps. At each deficiency it *knows the field* — it is literally inside the
   `if step.action in single_location_actions: if step.source is None and
   step.destination is None:` branch — yet it appends a prose string
   (`"Step 13 (mix): missing location"`) and raises them joined through a
   Pydantic `ValueError`.
2. `verify_no_missing_field` (`extraction/sufficient.py`) catches the
   `ValidationError` and **re-splits the prose** back into a `List[str]`.
3. `MissingFieldsDetector` (`gap_resolution/detectors.py`) regex-matches each
   string, then keyword-maps the description back to a field via
   `_missing_field_to_path` — `"missing volume"` → `.volume`,
   `"no source for"` → `.source`, … — falling back to `.unknown` for any
   phrasing the table didn't anticipate.

This is textbook **information leakage** (APoSD Ch. 5): the single design
decision *"which field does a missing-X correspond to"* lived in **two
places** — the validator's `errors.append` branches and the detector's
keyword table — and the two must be kept in agreement by hand. They drifted.
The validator gained a `single_location_actions` "missing location" check; the
keyword table never learned it. So a `mix` step with no source/destination
produced `field_path = steps[N].unknown`, and the apply layer
(`orchestrator.py:_apply_at_path`) did `setattr(step, "unknown", …)`, which
Pydantic rejects:

```
ValueError: "ExtractedStep" object has no field "unknown"
```

— an **uncaught** exception that killed a real Bradford run mid-gap-resolution
(`output/pipeline_state_20260627_113304.json`, `failed_at: uncaught_exception`).

The seam between the validator and the detector was a **shallow interface**
(APoSD Ch. 4): it carried *less* information than the producer had (prose
instead of an address), forcing the consumer to reconstruct what was already
known. A `ValidationError` of human sentences is the wrong abstraction to hand
a machine consumer — different layer, different abstraction.

## Decision

Make the producer — the walk that already knows the field — the single source
of truth for the address. The human string becomes a *rendering* of the
structured record, never an input to be parsed.

- **`models/spec.py`** — extract the per-step walk out of
  `validate_completeness` into a pure module function
  `missing_fields(spec) -> list[MissingField]`. `MissingField` carries
  `field_path` as a first-class field plus a `detail` phrase and a
  `message()` renderer. `validate_completeness` becomes a thin wrapper that
  raises the **byte-identical** `"Spec is incomplete (N issue(s)): …"` message
  (the construction-must-be-valid contract is preserved). Single-location
  actions (`mix`, `aspirate`, `dispense`, `touch_tip`) route their location to
  `.destination` by convention — a concrete field, not a placeholder.
- **`gap_resolution/detectors.py`** — `MissingFieldsDetector` reads
  `mf.field_path` directly. **Deleted** `_MISSING_FIELD_PATTERN`,
  `_missing_field_to_path`, `_missing_field_kind_and_severity`, the `.unknown`
  fallback, and the `unparseable` branch (~100 lines).
- **`extraction/sufficient.py`** — `verify_no_missing_field` becomes a thin
  string view: `[mf.message() for mf in missing_fields(spec)]`.
- **`gap_resolution/orchestrator.py`** — `_apply_at_path` no-ops when the
  parsed `fname` is not a field on the step model, instead of crashing. The
  structured walk means no path should reach here unmapped; the guard is
  **defense in depth** so a future unmapped path degrades safely.

The `.unknown` special case — and the crash it caused — is **defined out of
existence** (APoSD Ch. 10), not caught: the producer always states a real
address, so there is no "can't map" case left to handle. The previously-fatal
Bradford gap now resolves to `steps[12].destination` and is filled through the
normal suggester / modal machinery.

## Alternatives rejected

- **Add one more keyword rule (`"missing location"` → `.destination`).** The
  band-aid. It treats the symptom while leaving the leak — the two sources of
  truth still drift, and the next unanticipated validator phrasing reintroduces
  `.unknown`. Rejected: fixes the instance, not the class.
- **Make the detector tolerant of `.unknown` (skip such gaps).** Stops the
  crash but silently drops a *real* deficiency (the mix step genuinely has no
  location). Trades a loud failure for a quiet one.
- **Emit structured records from inside the Pydantic validator.** Pydantic's
  error channel carries strings; threading structured objects through
  `ValidationError` is awkward and couples the gap layer to Pydantic
  internals. Extracting a plain function that *both* the validator (rendered to
  a string) and the detector (structured) consume keeps each layer at its own
  abstraction.

## Consequences

- One source of truth for the field address; the keyword table and its two
  string round-trips are gone (~100 fewer lines in the detector).
- A new completeness rule cannot silently fall to an unmappable path — adding a
  branch to `missing_fields` necessarily states its `field_path`.
- `verify_no_missing_field` now reports *only* missing-field errors (it no
  longer surfaces unrelated validator errors that happened to ride along the
  old promote-then-catch path). Its sole caller was the detector, now migrated;
  it is kept as a string convenience.
- The apply-layer guard is a standing backstop: any field_path that doesn't
  address a real step field is a no-op, so this class of crash cannot recur.
- Regression tests pin both halves: a `mix` with no location routes to
  `destination` (not `.unknown`), and an unmappable field_path no-ops instead
  of raising.
