"""
spec_analysis.py — Pure-function queries over a `ProtocolSpec`.

These functions read a spec, optionally cross-reference its initial_contents
or config-resolved labels, and return derived facts (lists, sets, dicts).
They do not mutate the spec, do not talk to LLMs, and do not emit events.

Distinct from `models/spec.py` (which holds the type definitions) and
from `validation/constraints.py` (which checks the spec against config-
specific physical constraints and emits `ConstraintViolation`s). Spec
analysis sits between the two — same shape as constraint checking but
returns plain data instead of typed violations, for use cases that need
the answer without the violation envelope.

See ADR-0017 for the broader "no analysis in models/" framing.
"""

from typing import List, Tuple, Optional


def infer_source_containers(spec) -> List[Tuple[str, str, Optional[str]]]:
    """Identify labware wells that are only aspirated from, never dispensed into.

    These are source containers (reservoirs, tube racks) the scientist
    fills before running the protocol. Used by the pipeline's pre-
    orchestrator source-container confirmation modal so the user can
    acknowledge the inferred set or abort.

    Pre:    `spec` is a `ProtocolSpec` with `.steps` (each step exposes
            `.action`, `.source`, `.destination`, `.substance`) and
            `.initial_contents` (each entry exposes `.labware`, `.well`).
    Post:   Returns a list of `(labware, well, substance)` tuples — one
            per (description, well) that is referenced as a source in a
            liquid action AND is never referenced as a destination for
            the same description AND is not already in `initial_contents`.
            `substance` is the first-seen step's substance value (or None
            when the step didn't carry one). Order is the order wells
            were first encountered as sources.
    Side effects: None.
    """
    source_wells: dict = {}   # (labware, well) -> substance
    dest_wells: set = set()   # (labware, well)

    liquid_actions = {"transfer", "distribute", "consolidate",
                      "serial_dilution", "aspirate"}

    for step in spec.steps:
        if step.action not in liquid_actions:
            continue

        if step.source:
            desc = step.source.description
            wells: list = []
            if step.source.well:
                wells = [step.source.well]
            elif step.source.wells:
                wells = step.source.wells
            for w in wells:
                key = (desc, w)
                if key not in source_wells:
                    source_wells[key] = (
                        step.substance.value if step.substance else None
                    )

        if step.destination:
            desc = step.destination.description
            wells = []
            if step.destination.well:
                wells = [step.destination.well]
            elif step.destination.wells:
                wells = step.destination.wells
            if step.destination.well_range:
                dest_wells.add((desc, "__range__"))
            for w in wells:
                dest_wells.add((desc, w))

    dest_labware = {lw for lw, _ in dest_wells}
    already_tracked = {(ic.labware, ic.well) for ic in spec.initial_contents}

    results: List[Tuple[str, str, Optional[str]]] = []
    seen = set()
    for (labware, well), substance in source_wells.items():
        if labware in dest_labware:
            continue
        if (labware, well) in already_tracked:
            continue
        if (labware, well) in seen:
            continue
        seen.add((labware, well))
        results.append((labware, well, substance))
    return results
