import re
from typing import List, Optional

from nl2protocol.citing import cite_covers_well
from nl2protocol.models.spec import ProtocolSpec
from nl2protocol.extraction.extractor import TERMINAL_REVIEW_STATUSES


# ========================================================================
# PROVENANCE VERIFICATION
# ========================================================================

# helpers — (provenance issue → dict) maker; atomic value-in-quote checker
# ========================================================================

def _warn(step_index: int, field: str, value: str,
          claimed_source: str, severity: str, message: str,
          field_path: Optional[str] = None) -> dict:
    """Build a structured provenance warning.

    `field_path` is the spec address the warning is about (e.g.
    `steps[2].source.wells_provenance`). The verifier already knows this
    when it walks the spec — emitting it here means downstream consumers
    (the orchestrator's Gap detector) don't have to translate the
    human-readable `field` label back into a path. `field` stays as the
    display label.
    """
    return {
        "step": step_index,
        "field": field,
        "field_path": field_path,
        "value": value,
        "claimed_source": claimed_source,
        "severity": severity,
        "message": message,
    }


def value_in_quote(value, quote: str) -> bool:
    """Return True if `value` is grounded by the cited `quote`.

    Numeric values: accept both '100' and '100.0' forms (integer-valued
    floats may appear in the cite either way). String values: case-
    insensitive substring match, plus a well-name range relaxation —
    a well like 'B2' is considered grounded by a cite like '(B1-B4)',
    'column 2', or 'rows A-D' because the cited range syntax IS
    verbatim from the instruction and the member well is a
    deterministic expansion of it. The relaxation only fires for
    well-name-shaped values ([A-P]\\d+); other strings keep the
    plain substring semantics.
    """
    if isinstance(value, (int, float)):
        if float(value).is_integer() and str(int(value)) in quote:
            return True
        return str(value) in quote
    s = str(value).strip()
    # Range path: a well like 'B2' is grounded by a cite that names
    # the range 'B1-B4' (or 'column 1', 'rows A-D'). cite_covers_well
    # only fires for cites containing range syntax; literal-well
    # cites fall through to the boundary/substring path below.
    if cite_covers_well(quote, s):
        return True
    # Well-name literals (`[A-P]\d+`) must match the cite with word
    # boundaries — otherwise "B2" would match a cite containing
    # "B20", silently hiding fabricated wells (CodeRabbit P1).
    if re.fullmatch(r"[A-Pa-p]\d+", s):
        return re.search(
            rf"(?<![A-Za-z0-9]){re.escape(s)}(?![A-Za-z0-9])",
            quote,
            re.IGNORECASE,
        ) is not None
    # Non-well-shaped strings (substance names, etc.): plain
    # case-insensitive substring match, as before.
    return s.lower() in quote.lower()


# checkers — instruction provenances, low-confidence non-instruction provenances
# ========================================================================

def verify_claimed_instruction_provenance(spec: ProtocolSpec, instruction: str) -> List[dict]:
    """Verify every source='instruction' provenance via cited_text.

    For each provenance with source='instruction', three checks must
    all pass:
        1. cited_text is non-empty.
        2. cited_text appears in the instruction (case-insensitive,
            whitespace-collapsed via citing.find_cite_position).
        3. the value is contained within the cited_text.

    Each failed check yields one warning with severity='fabrication'.
    Walks every field that can carry instruction-sourced provenance:
    atomic value fields (volume / substance / duration / temperature),
    post_action volumes, and LocationRef slots (description + wells
    — each has its own provenance after the 1a split).
    """
    from nl2protocol.citing import find_cite_position

    warnings = []

    def check(step_order: int, field_name: str, field_path: str,
              value, prov):
        if not prov or prov.source != "instruction":
            return
        # A user action (or reviewer agreement) on this Provenance is
        # terminal for the fabrication lifecycle: re-running the
        # cited_text checks would re-raise a gap the user already
        # resolved, looping the orchestrator until iteration cap.
        if prov.review_status in TERMINAL_REVIEW_STATUSES:
            return
        quotes = prov.cited_text  # List[str] (normalizer wraps str → [str])
        if not quotes:
            warnings.append(_warn(
                step_order, field_name, str(value), "instruction", "fabrication",
                f"Step {step_order} {field_name}: source='instruction' but cited_text missing",
                field_path=field_path,
            ))
            return
        # Every cite must appear in the instruction. A list of cites is
        # only honest if all of them are real verbatim quotes — one
        # bogus entry means the LLM confabulated.
        missing = next(
            (q for q in quotes if find_cite_position(instruction, q) is None),
            None,
        )
        if missing is not None:
            warnings.append(_warn(
                step_order, field_name, str(value), "instruction", "fabrication",
                f"Step {step_order} {field_name}: cited_text {missing!r} not found in instruction",
                field_path=field_path,
            ))
            return
        # At least one cite must contain THIS specific value. For a
        # wells list where each well has its own cite ("Plasmid A1 to
        # cells B1", "Plasmid A2 to cells B2", ...), the verifier is
        # called once per well; A1 should match its own cite, A2 its
        # own, etc. — at-least-one-contains is the right relaxation.
        if not any(value_in_quote(value, q) for q in quotes):
            warnings.append(_warn(
                step_order, field_name, str(value), "instruction", "fabrication",
                f"Step {step_order} {field_name}: value {value!r} not present in any cited_text {quotes!r}",
                field_path=field_path,
            ))

    for step_idx, step in enumerate(spec.steps):
        if step.volume:
            check(step.order, "volume",
                  f"steps[{step_idx}].volume.provenance",
                  step.volume.value, step.volume.provenance)
        if step.substance:
            check(step.order, "substance",
                  f"steps[{step_idx}].substance.provenance",
                  step.substance.value, step.substance.provenance)
        if step.duration:
            check(step.order, "duration",
                  f"steps[{step_idx}].duration.provenance",
                  step.duration.value, step.duration.provenance)
        if step.temperature:
            check(step.order, "temperature",
                  f"steps[{step_idx}].temperature.provenance",
                  step.temperature.value, step.temperature.provenance)
        for ref, role in [(step.source, "source"), (step.destination, "destination")]:
            if not ref:
                continue
            check(step.order, f"{role} labware",
                  f"steps[{step_idx}].{role}.description_provenance",
                  ref.description, ref.description_provenance)
            wells = list(ref.wells or ([ref.well] if ref.well else []))
            for w in wells:
                # Every well on a ref shares one wells_provenance, so all
                # well-fabrication warnings point at the SAME slot. Gap
                # dedup by id collapses them into one Gap.
                check(step.order, f"{role} well",
                      f"steps[{step_idx}].{role}.wells_provenance",
                      w, ref.wells_provenance)
        if step.post_actions:
            for pa_idx, pa in enumerate(step.post_actions):
                if pa.volume:
                    check(step.order, f"{pa.action} volume",
                          f"steps[{step_idx}].post_actions[{pa_idx}].volume.provenance",
                          pa.volume.value, pa.volume.provenance)

    return warnings


def flag_low_confidence_non_instr_provenances(spec: ProtocolSpec) -> List[dict]:
    """Flag domain_default and inferred claims for user confirmation.

    domain_default with confidence < 0.8 → severity 'unverified'.
    inferred (any confidence) → severity 'low_confidence'.
    These are routed to Task 6's threshold-based confirmation UX.
    """
    warnings = []

    for step in spec.steps:
        # Composition-level: inferred steps are always flagged
        if step.composition_provenance.confidence < 0.8:
            grounding = step.composition_provenance.grounding
            if "instruction" not in grounding:
                warnings.append(_warn(
                    step.order, "composition", step.action,
                    ",".join(grounding) if grounding else "inferred",
                    "low_confidence",
                    f"Step {step.order} ({step.action}): composition confidence "
                    f"{step.composition_provenance.confidence} — may need confirmation",
                ))

        # Walk provenanced fields. Atomic fields carry one provenance;
        # LocationRefs carry two (description + wells) — each gets its
        # own uncertainty check so a low-confidence wells claim is not
        # masked by a high-confidence description claim, or vice versa.
        fields = [
            ("volume", step.volume, step.volume.provenance if step.volume else None),
            ("temperature", step.temperature, step.temperature.provenance if step.temperature else None),
            ("substance", step.substance, step.substance.provenance if step.substance else None),
            ("duration", step.duration, step.duration.provenance if step.duration else None),
        ]
        for ref, role in [(step.source, "source"), (step.destination, "destination")]:
            if not ref:
                continue
            if ref.description_provenance:
                fields.append((f"{role} labware", ref, ref.description_provenance))
            if ref.wells_provenance:
                fields.append((f"{role} wells", ref, ref.wells_provenance))

        for field_name, field_val, prov in fields:
            if not prov:
                continue

            val_str = str(field_val.value) if hasattr(field_val, 'value') else str(field_val)
            if prov.source == "inferred":
                warnings.append(_warn(
                    step.order, field_name, val_str,
                    "inferred", "low_confidence",
                    f"Step {step.order} {field_name}: inferred value "
                    f"(confidence {prov.confidence}) — needs confirmation",
                ))
            elif prov.source == "domain_default" and prov.confidence < 0.8:
                warnings.append(_warn(
                    step.order, field_name, val_str,
                    "domain_default", "unverified",
                    f"Step {step.order} {field_name}: domain default with "
                    f"confidence {prov.confidence} — may need confirmation",
                ))

        # Post-action fields
        if step.post_actions:
            for pa in step.post_actions:
                if pa.volume and pa.volume.provenance:
                    prov = pa.volume.provenance
                    if prov.source == "inferred":
                        warnings.append(_warn(
                            step.order, f"{pa.action} volume",
                            f"{pa.volume.value}{pa.volume.unit}",
                            "inferred", "low_confidence",
                            f"Step {step.order} {pa.action}: inferred volume "
                            f"(confidence {prov.confidence}) — needs confirmation",
                        ))
                    elif prov.source == "domain_default" and prov.confidence < 0.8:
                        warnings.append(_warn(
                            step.order, f"{pa.action} volume",
                            f"{pa.volume.value}{pa.volume.unit}",
                            "domain_default", "unverified",
                            f"Step {step.order} {pa.action}: domain default volume "
                            f"with confidence {prov.confidence} — may need confirmation",
                        ))

    return warnings


# public entrypoint
# ========================================================================

def inspect_provenance_claims(spec: ProtocolSpec, instruction: str, config: dict) -> List[dict]:
    """Verify provenance claims across all source types.

    Dispatches on provenance.source for each field:
        - instruction: cited_text must appear in the instruction AND must
          contain the value → 'fabrication' if either check fails
        - domain_default: flag if confidence < 0.8 → 'unverified'
        - inferred: always flag → 'low_confidence'

    `config` is accepted for API symmetry but currently unused — the
    config-claims branch was dropped during the refactor.

    Returns list of warning dicts:
        {step, field, field_path, value, claimed_source, severity, message}

    Severity levels (for Task 6 routing):
        'fabrication'    — LLM lied about where a value came from (block/fix)
        'unverified'     — domain default with low confidence (prompt user)
        'low_confidence' — inferred value (always prompt user)
    """
    warnings = []
    warnings.extend(verify_claimed_instruction_provenance(spec, instruction))
    warnings.extend(flag_low_confidence_non_instr_provenances(spec))
    return warnings
