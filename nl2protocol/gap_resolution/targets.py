"""
Typed spec-address targets for the gap-resolution layer.

Replaces the stringly-typed `Gap.field_path` / `ReviewResult.field_path` with
a Union of Pydantic models, one per address shape that detectors actually
produce. The apply layer and the spotlight/anchor layer dispatch by variant
via `isinstance` instead of regex-parsing the path string.

Phase 1 scope (this file):
  - Define the variants.
  - Provide `path_to_target` / `target_to_path` as a bidirectional bridge
    so producers and consumers can migrate independently across phases.
  - Be lenient: every current path string round-trips, including sentinels
    (`.constraint`, `labware.namespace_split:*`, `"unknown"`, test
    placeholders like `"x"`/`"a"`/`"b"`). Anything we don't recognize lands
    as `UnknownTarget(raw=...)` rather than raising.

Future phases:
  - Phase 2: `Gap.targets: List[GapTarget]` added alongside `field_path`.
  - Phase 3: apply layer migrates to consume targets.
  - Phase 4: detectors produce typed targets natively; `field` / `subfield`
    can then tighten to Literal types of real ExtractedStep / LocationRef
    field names.
"""

from __future__ import annotations

import re
from typing import Union

from pydantic import BaseModel


# ============================================================================
# Variants — one per real spec-address shape
# ============================================================================

class StepField(BaseModel):
    """Address: `spec.steps[step_idx].<field>`.

    A top-level field on an ExtractedStep (volume, substance, source, etc.).
    `field` is the literal attribute name as declared on the Pydantic model.
    Kept as `str` in Phase 1 to admit any string emitted by current detectors;
    tightened to a Literal in Phase 4.
    """
    step_idx: int
    field: str


class StepSubfield(BaseModel):
    """Address: `spec.steps[step_idx].<field>.<subfield>`.

    A nested slot under a LocationRef-bearing step field (source/destination).
    Today `field` is always one of {"source", "destination"} and `subfield`
    is one of LocationRef's data slots (well, wells, well_range, description,
    resolved_label).
    """
    step_idx: int
    field: str
    subfield: str


class StepProvenance(BaseModel):
    """Address: `spec.steps[step_idx].<field>.<slot>` where `slot` ends in
    `provenance`.

    Covers three current cases:
      - atomic Provenanced* fields:    `steps[N].volume.provenance`
      - LocationRef description prov:  `steps[N].source.description_provenance`
      - LocationRef wells prov:        `steps[N].destination.wells_provenance`
    """
    step_idx: int
    field: str
    slot: str


class InitialVolume(BaseModel):
    """Address: `spec.initial_contents[well_idx].volume_ul`."""
    well_idx: int


class InitialWell(BaseModel):
    """Address: `spec.initial_contents[well_idx].well`."""
    well_idx: int


# ============================================================================
# Sentinel variants — Phase 1 preserves these for round-trip coverage.
# Phase 4 will likely reclassify them into a separate `Warning` type or
# give them real semantics.
# ============================================================================

class ConstraintPlaceholder(BaseModel):
    """`steps[N].constraint` — emitted by ConstraintViolationDetector for
    violations whose ViolationType has no field-attributable target
    (TIP_INSUFFICIENT, SLOT_CONFLICT, MODULE_*, VOLUME_EXCEEDS_WELL,
    LABWARE_NOT_FOUND without a role). The step index is informational; the
    user resolves these by editing config offline, not by writing into the
    spec.
    """
    step_idx: int


class NamespaceSplit(BaseModel):
    """`labware.namespace_split:<description>` — synthetic coordination key
    emitted by NamespaceSplitDetector. Not a spec slot; identifies a
    cross-step labware description that the user can rename or split.
    """
    description: str


class UnknownTarget(BaseModel):
    """Catch-all for paths that don't match any known shape — preserves the
    raw string so the round-trip identity holds. Covers the `"unknown"`
    fallback in MissingFieldsDetector and any test-only placeholders
    (`"x"`, `"constraints.pipette_capacity"`, etc.).
    """
    raw: str


# ============================================================================
# Union + bridge
# ============================================================================

GapTarget = Union[
    StepField,
    StepSubfield,
    StepProvenance,
    InitialVolume,
    InitialWell,
    ConstraintPlaceholder,
    NamespaceSplit,
    UnknownTarget,
]


# Match order matters: more-specific patterns first.
# `steps[N].<field>.<slot>provenance$` must come before the generic
# `steps[N].<field>.<sub>$` pattern because `description_provenance`
# would otherwise be claimed as a subfield.
_STEP_PROV_RE = re.compile(r"^steps\[(\d+)\]\.(\w+)\.(\w*provenance)$")
_STEP_SUBFIELD_RE = re.compile(r"^steps\[(\d+)\]\.(\w+)\.(\w+)$")
_STEP_CONSTRAINT_RE = re.compile(r"^steps\[(\d+)\]\.constraint$")
_STEP_FIELD_RE = re.compile(r"^steps\[(\d+)\]\.(\w+)$")
_INITIAL_VOL_RE = re.compile(r"^initial_contents\[(\d+)\]\.volume_ul$")
_INITIAL_WELL_RE = re.compile(r"^initial_contents\[(\d+)\]\.well$")
_NAMESPACE_RE = re.compile(r"^labware\.namespace_split:(.+)$")


def path_to_target(path: str) -> GapTarget:
    """Parse a dotted spec-address string into a typed GapTarget variant.

    Pre:    `path` is any string currently emitted by any detector, suggester,
            or upstream warning producer in the gap_resolution pipeline.
            Includes well-formed spec addresses, sentinel placeholders, and
            arbitrary test fixtures.

    Post:   Returns exactly one GapTarget variant, selected deterministically
            by regex match order:
              1. `steps[N].<field>.<slot>provenance$`              → StepProvenance
              2. `steps[N].constraint$`                            → ConstraintPlaceholder
              3. `steps[N].<field>.<subfield>$`                    → StepSubfield
              4. `steps[N].<field>$`                               → StepField
              5. `initial_contents[N].volume_ul$`                  → InitialVolume
              6. `initial_contents[N].well$`                       → InitialWell
              7. `labware.namespace_split:<description>`           → NamespaceSplit
              8. anything else                                     → UnknownTarget(raw=path)
            Pattern 2 fires before pattern 3 so the `.constraint` sentinel is
            never misclassified as a subfield.

    Side effects: None.
    Raises: Never. Malformed input falls through to UnknownTarget.
    """
    m = _STEP_PROV_RE.match(path)
    if m:
        return StepProvenance(
            step_idx=int(m.group(1)),
            field=m.group(2),
            slot=m.group(3),
        )

    m = _STEP_CONSTRAINT_RE.match(path)
    if m:
        return ConstraintPlaceholder(step_idx=int(m.group(1)))

    m = _STEP_SUBFIELD_RE.match(path)
    if m:
        return StepSubfield(
            step_idx=int(m.group(1)),
            field=m.group(2),
            subfield=m.group(3),
        )

    m = _STEP_FIELD_RE.match(path)
    if m:
        return StepField(
            step_idx=int(m.group(1)),
            field=m.group(2),
        )

    m = _INITIAL_VOL_RE.match(path)
    if m:
        return InitialVolume(well_idx=int(m.group(1)))

    m = _INITIAL_WELL_RE.match(path)
    if m:
        return InitialWell(well_idx=int(m.group(1)))

    m = _NAMESPACE_RE.match(path)
    if m:
        return NamespaceSplit(description=m.group(1))

    return UnknownTarget(raw=path)


def target_to_path(t: GapTarget) -> str:
    """Serialize a typed GapTarget back to its dotted-string form.

    Pre:    `t` is any GapTarget variant.

    Post:   Returns a string `s` such that `path_to_target(s) == t` for every
            variant. For UnknownTarget, returns `t.raw` verbatim — preserving
            the original string so the round-trip identity holds for inputs
            we didn't recognize on the way in.

    Side effects: None.
    Raises: Never (all variant branches are total).
    """
    if isinstance(t, StepProvenance):
        return f"steps[{t.step_idx}].{t.field}.{t.slot}"
    if isinstance(t, ConstraintPlaceholder):
        return f"steps[{t.step_idx}].constraint"
    if isinstance(t, StepSubfield):
        return f"steps[{t.step_idx}].{t.field}.{t.subfield}"
    if isinstance(t, StepField):
        return f"steps[{t.step_idx}].{t.field}"
    if isinstance(t, InitialVolume):
        return f"initial_contents[{t.well_idx}].volume_ul"
    if isinstance(t, InitialWell):
        return f"initial_contents[{t.well_idx}].well"
    if isinstance(t, NamespaceSplit):
        return f"labware.namespace_split:{t.description}"
    if isinstance(t, UnknownTarget):
        return t.raw
    raise TypeError(f"Unknown GapTarget variant: {type(t).__name__}")
