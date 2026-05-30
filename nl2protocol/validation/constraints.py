"""
constraints.py

Deterministic constraint checker for protocol specifications.

This is the neuro-symbolic bridge: the LLM extracts what the scientist wants,
and this module checks whether the hardware can physically do it. Conflicts
are surfaced to the scientist with explanations — never silently resolved.

Design principles (from ConstraintLLM, EMNLP 2025):
  - Constraint checking is DETERMINISTIC — no LLM, no randomness
  - Violations are STRUCTURED — type, severity, what/why/suggestion
  - Nothing is silently clamped or dropped
  - The scientist decides how to resolve conflicts

Checks performed:
  1. Pipette capacity — can the selected pipette handle the requested volume?
  2. Well validity — do referenced wells exist on the labware?
  3. Labware resolution — can every LocationRef map to config labware?
  4. Module availability — does the config have the required modules?
  5. Tip sufficiency — are there enough tips for the protocol?
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Tuple

from nl2protocol.models.spec import ProtocolSpec, ExtractedStep, ProvenancedVolume


# ============================================================================
# CONSTRAINT VIOLATION TYPES
# ============================================================================

class Severity(Enum):
    """The outcome of one constraint check on one (step, check_type) cell.

    Tagged-union discriminator for `CheckResult` — see `notes/cs-concepts.md`
    ("Tagged union: one class + discriminator, not N parallel classes")
    and the sibling `Gap.kind` field in `gap_resolution/types.py` for the
    same pattern applied to the orchestrator's gap pipeline.
    """
    ERROR   = "error"      # Cannot proceed — physically impossible
    WARNING = "warning"    # Can proceed but result may be suboptimal
    INFO    = "info"       # FYI — scientist should be aware
    PASSED  = "passed"     # Check ran, the spec satisfied it


class ViolationType(Enum):
    """What kind of constraint was violated?"""
    PIPETTE_CAPACITY = "pipette_capacity"
    PIPETTE_MINIMUM = "pipette_minimum"
    WELL_INVALID = "well_invalid"
    LABWARE_NOT_FOUND = "labware_not_found"
    MODULE_NOT_FOUND = "module_not_found"
    MODULE_LABWARE_MISMATCH = "module_labware_mismatch"
    TIP_INSUFFICIENT = "tip_insufficient"
    VOLUME_EXCEEDS_WELL = "volume_exceeds_well"
    SLOT_CONFLICT = "slot_conflict"


@dataclass
class CheckResult:
    """The outcome of one constraint check on one (step, check_type) cell.

    Tagged-union shape: shared fields up front; outcome-specific fields
    are `Optional` and filled by the side of the union the record
    represents. Every (step, check_type) cell that the checker visits
    appears as exactly one `CheckResult` — never zero, never two.

    Failure variants (`severity` in {ERROR, WARNING, INFO}) carry
    user-actionable text: `what` / `why` / `suggestion` + machine-
    readable `values`. Pass variants (`severity == PASSED`) carry
    `detail_label` + `what` — the row in the spec view the inline tag
    anchors to, and the short positive phrase shown there.

    Naming: kept the historical name in the public API via the
    `ConstraintViolation` alias just below so existing callers don't
    have to migrate; new code should reach for `CheckResult` directly.
    The pattern is documented in `notes/cs-concepts.md`.
    """
    violation_type: ViolationType        # for passes, names the check that ran
    severity: Severity                   # discriminator
    step: int                            # 0 for protocol-level checks
    what: str                            # failure: what's wrong; pass: inline phrase
    why: str = ""                        # failure-only
    suggestion: str = ""                 # failure-only
    values: Dict[str, object] = field(default_factory=dict)
    # Pass-only: the detail row the inline tag attaches to. Logical
    # labels (the rendering layer maps these to displayed rows):
    #   "volume" / "source_labware" / "source_wells" /
    #   "destination_labware" / "destination_wells".
    detail_label: Optional[str] = None

    def __str__(self) -> str:
        icon = {
            "error":   "ERROR",
            "warning": "WARN",
            "info":    "INFO",
            "passed":  "PASS",
        }[self.severity.value]
        if self.severity == Severity.PASSED:
            anchor = f" ({self.detail_label})" if self.detail_label else ""
            return f"[{icon}] Step {self.step}{anchor}: {self.what}"
        return (f"[{icon}] Step {self.step}: {self.what}\n"
                f"       Why: {self.why}\n"
                f"       Suggestion: {self.suggestion}")


# Backward-compat alias. Pre-refactor name; kept so existing imports,
# `isinstance(..., ConstraintViolation)` checks, and test fixtures
# continue to work without churn. New code should use `CheckResult`.
ConstraintViolation = CheckResult


@dataclass
class PhysicalConstraintsCheckResult:
    """Result of running all constraint checks.

    Backing store is a single `_checks` list; the historical `violations`
    accessor is now a property that returns only the non-`PASSED` entries
    (preserving its meaning for every existing reader). Passes are
    accessed via the new `passes` property.
    """
    # Internal: every CheckResult emitted by the checker, including passes.
    # External callers read via the properties below.
    _checks: List[CheckResult] = field(default_factory=list)

    @property
    def violations(self) -> List[CheckResult]:
        """Failure-side records only. Preserves pre-refactor semantics —
        an existing caller reading `result.violations` sees the same
        list shape it always did (errors + warnings + infos, never
        passes)."""
        return [c for c in self._checks if c.severity != Severity.PASSED]

    @property
    def passes(self) -> List[CheckResult]:
        """PASSED records only — what the inline-check renderer reads."""
        return [c for c in self._checks if c.severity == Severity.PASSED]

    @property
    def checks(self) -> List[CheckResult]:
        """Every check the checker ran, regardless of outcome."""
        return list(self._checks)

    @property
    def has_errors(self) -> bool:
        return any(c.severity == Severity.ERROR for c in self._checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.severity == Severity.WARNING for c in self._checks)

    @property
    def errors(self) -> List[CheckResult]:
        return [c for c in self._checks if c.severity == Severity.ERROR]

    @property
    def warnings(self) -> List[CheckResult]:
        return [c for c in self._checks if c.severity == Severity.WARNING]

    def __bool__(self) -> bool:
        """True if there are no errors (warnings are OK to proceed)."""
        return not self.has_errors


# ============================================================================
# PIPETTE KNOWLEDGE
# ============================================================================

PIPETTE_SPECS: Dict[str, Tuple[float, float]] = {
    "p10": (1.0, 10.0),
    "p20": (1.0, 20.0),
    "p50": (5.0, 50.0),
    "p300": (20.0, 300.0),
    "p1000": (100.0, 1000.0),
}


def get_pipette_range(model: str) -> Optional[Tuple[float, float]]:
    """Map an Opentrons pipette model load-name to its (min_uL, max_uL) range.

    Pre:    `model` is a non-empty Opentrons pipette load-name string of the
            form "<spec>_<channels>_<generation>" — e.g. "p300_single_gen2",
            "p1000_multi_flex". Recognized specs are exactly the keys of
            PIPETTE_SPECS: {"p10", "p20", "p50", "p300", "p1000"}.

    Post:   Returns (min_uL, max_uL) — the pipette's accurate-volume range in
            microliters — iff `model.lower()` starts with "<spec>_" for some
            spec in PIPETTE_SPECS. Returns None otherwise. Volumes are always
            in microliters; this function is never responsible for unit
            conversion.

    Side effects: None. No I/O, no mutation, deterministic.

    Raises: AttributeError if `model` is not a string.
    """
    model_lower = model.lower()
    for key, (lo, hi) in PIPETTE_SPECS.items():
        if model_lower.startswith(key + "_"):
            return (lo, hi)
    return None


def get_pipette_for_volume(volume: float, config: dict) -> Optional[str]:
    """Find the first configured mount whose pipette can accurately handle `volume`.

    Pre:    `volume` is a real number in microliters. `config` is a dict that
            may contain "pipettes" → {mount_name: {"model": str, ...}}, where
            each "model" is an Opentrons load-name in the legible domain of
            `get_pipette_range` (e.g. "p300_single_gen2"). The caller is
            responsible for any unit conversion; this function never converts.

    Post:   Returns the mount name (e.g. "left", "right") of the first pipette
            in dict-insertion order over `config["pipettes"]` whose
            accurate-volume range [min_uL, max_uL] satisfies
            `min_uL <= volume <= max_uL`. Returns None iff one of:
              - `config` has no "pipettes" key,
              - `config["pipettes"]` is empty,
              - no configured pipette's range covers `volume`,
              - every configured pipette's model is unrecognized by
                `get_pipette_range`.

    Side effects: None. Does not mutate `config`. Deterministic.

    Raises: KeyError if any pipette entry is missing the "model" key.
            AttributeError if a "model" value is not a string.
    """
    if "pipettes" not in config:
        return None
    for mount, pip in config["pipettes"].items():
        rng = get_pipette_range(pip["model"])
        if rng and rng[0] <= volume <= rng[1]:
            return mount
    return None


def get_all_pipette_ranges(config: dict) -> Dict[str, Tuple[float, float]]:
    """Map every recognized configured mount to its pipette's (min_uL, max_uL).

    Pre:    `config` is a dict, optionally with "pipettes" → {mount_name (str):
            {"model": str, ...}}, where each "model" is an Opentrons load-name
            in the legible domain of `get_pipette_range`.

    Post:   Returns a new dict {mount: (min_uL, max_uL)} containing one entry
            for every mount in `config["pipettes"]` whose model is recognized
            by `get_pipette_range`. Mounts with unrecognized models are
            silently omitted (NOT included with a None or sentinel value).
            Returns {} iff one of:
              - `config` has no "pipettes" key,
              - `config["pipettes"]` is empty,
              - every configured pipette's model is unrecognized.
            Volumes are always in microliters.

    Side effects: None. Does not mutate `config`. Deterministic.

    Raises: KeyError if any pipette entry is missing the "model" key.
            AttributeError if a "model" value is not a string.
    """
    result = {}
    for mount, pip in config.get("pipettes", {}).items():
        rng = get_pipette_range(pip["model"])
        if rng:
            result[mount] = rng
    return result


# ============================================================================
# CONSTRAINT CHECKER
# ============================================================================

class PhysicalConstraintsChecker:
    """Deterministic constraint checker for ProtocolSpec against lab config.

    Run this AFTER resolution of extracted spec and BEFORE schema generation. Surfaces all
    hardware conflicts so the scientist can make informed decisions.
    """

    def __init__(self, config: dict):
        self.config = config
        self.pipette_ranges = get_all_pipette_ranges(config)

    def assert_physical_constraints(self, spec: ProtocolSpec) -> PhysicalConstraintsCheckResult:
        """Run every constraint check and collect violations.

        Pre:    `spec` is a Pydantic-validated `ProtocolSpec`. `self.config`
                was supplied at construction.
        Post:   Returns a `PhysicsalConstraintsCheckResult` containing zero or more
                `ConstraintViolation`s. Empty `violations` list iff every check
                passes. Each violation has all six structured fields populated
                (type, severity, step, what, why, suggestion). The same logical
                problem is not double-reported across check categories.
        Pure:   Does not mutate `spec` or `self.config`. No I/O, no LLM, no 
                randomness - same inputs always produce the same result.
        Raises: Never. Malformed inputs are the caller's responsibility.
        """
        result = PhysicalConstraintsCheckResult()

        for step in spec.steps:
            self._verify_step_volumes_fit_pipette(step, result)
            self._verify_labwares_exist_in_config(step, result)
            self._verify_referenced_wells_exist_in_config(step, result)

        self._verify_modules_exist_in_config(spec, result)
        self._check_labware_on_module(spec, result)
        self._check_tip_sufficiency(spec, result)

        return result

    # ========================================================================
    # INDIVIDUAL CHECKS
    # ========================================================================

    def _pass_phrase_for_volume(self, volume: float) -> Optional[str]:
        """Build the short positive phrase emitted on a passing pipette-
        capacity check, used as the inline tag next to a volume value.

        Pre:    `volume` is a real number in microliters that has already
                been verified by the caller as fitting at least one
                configured pipette's range. (If no pipette covers it,
                the caller will have emitted a violation instead and
                this method should not be called.)

        Post:   Returns a string of the form
                "within p20 range (1-20 uL)" - uses the smallest pipette
                spec key (p10/p20/p50/p300/p1000) that covers the volume,
                with the pipette's accurate range. Returns None when the
                configured pipette's model isn't recognized by
                `get_pipette_range` (defensive - caller should treat as
                "no pass tag to emit").

                CodeRabbit P2 fix: this picks the SMALLEST covering
                pipette (lowest upper bound), not the first-in-config-
                order. Without this, a 20 uL transfer in a config with
                both p20 and p300 mounted could be tagged "within p300
                range (20-300 uL)" if p300 happened to come first in
                the dict.
        """
        covering = []
        for mount, (lo, hi) in self.pipette_ranges.items():
            if lo <= volume <= hi:
                covering.append((hi, lo, mount))
        if not covering:
            return None
        covering.sort()  # smallest upper bound first
        hi, lo, mount = covering[0]
        model_full = self.config["pipettes"][mount]["model"]
        spec_key = next(
            (k for k in PIPETTE_SPECS if model_full.lower().startswith(k + "_")),
            model_full,
        )
        return f"within {spec_key} range ({lo:g}-{hi:g} \u00b5L)"

    def _verify_step_volumes_fit_pipette(self, step: ExtractedStep, result: PhysicalConstraintsCheckResult):
        """Check that every volume in this step can be handled by available pipettes."""

        # Check step volume
        if step.volume:
            vol = step.volume.value
            if step.volume.unit == "mL":
                vol *= 1000
            self._check_volume_against_pipettes(step, vol, "transfer", result)
            # Pass branch: if some pipette covers the volume, emit a
            # PASSED CheckResult so the inline-check renderer can stamp
            # a green tag next to the volume row. The phrase names the
            # smallest covering pipette + its range, matching the value
            # the row displays.
            can_handle = any(lo <= vol <= hi
                             for lo, hi in self.pipette_ranges.values())
            if can_handle:
                phrase = self._pass_phrase_for_volume(vol)
                if phrase:
                    result._checks.append(CheckResult(
                        violation_type=ViolationType.PIPETTE_CAPACITY,
                        severity=Severity.PASSED,
                        step=step.order,
                        what=phrase,
                        detail_label="volume",
                        values={"volume": vol},
                    ))

        # Check post-action volumes (e.g., mix volumes)
        if step.post_actions:
            for pa in step.post_actions:
                if pa.volume:
                    pa_vol = pa.volume.value
                    if pa.volume.unit == "mL":
                        pa_vol *= 1000

                    # The mix volume needs to be handled by the SAME pipette
                    # that's doing the transfer (you can't switch mid-operation)
                    transfer_vol = None
                    if step.volume:
                        transfer_vol = step.volume.value
                        if step.volume.unit == "mL":
                            transfer_vol *= 1000

                    # Which pipette would handle the transfer?
                    transfer_mount = None
                    if step.pipette_hint:
                        for mount, pip in self.config.get("pipettes", {}).items():
                            if step.pipette_hint.lower() in pip["model"].lower():
                                transfer_mount = mount
                                break
                    if not transfer_mount and transfer_vol:
                        transfer_mount = get_pipette_for_volume(transfer_vol, self.config)

                    if transfer_mount:
                        rng = self.pipette_ranges.get(transfer_mount)
                        if rng and pa_vol > rng[1]:
                            # CONFLICT: mix volume exceeds the transfer pipette's capacity
                            pip_model = self.config["pipettes"][transfer_mount]["model"]
                            # Find if another pipette could do it
                            alt_mount = get_pipette_for_volume(pa_vol, self.config)

                            # Check if alt pipette can ALSO handle the transfer volume
                            alt_covers_transfer = False
                            if alt_mount and alt_mount != transfer_mount:
                                alt_rng = self.pipette_ranges.get(alt_mount)
                                if alt_rng and transfer_vol and alt_rng[0] <= transfer_vol <= alt_rng[1]:
                                    alt_covers_transfer = True

                            if alt_mount and alt_mount != transfer_mount and alt_covers_transfer:
                                alt_model = self.config["pipettes"][alt_mount]["model"]
                                suggestion = (
                                    f"Use {alt_model} ({alt_mount}) for this step — "
                                    f"it handles both the transfer ({transfer_vol}uL) and "
                                    f"{pa.action} ({pa_vol}uL)."
                                )
                            elif alt_mount and alt_mount != transfer_mount:
                                alt_model = self.config["pipettes"][alt_mount]["model"]
                                alt_rng = self.pipette_ranges[alt_mount]
                                suggestion = (
                                    f"No single pipette covers both {transfer_vol}uL (transfer) "
                                    f"and {pa_vol}uL ({pa.action}). Options:\n"
                                    f"         1. Reduce {pa.action} volume to {rng[1]}uL "
                                    f"({pip_model} max) — less thorough mixing\n"
                                    f"         2. Use {alt_model} for entire step — "
                                    f"handles {pa_vol}uL {pa.action} but {transfer_vol}uL is "
                                    f"below its accurate range ({alt_rng[0]}uL min)\n"
                                    f"         3. Split into two operations: transfer with "
                                    f"{pip_model}, then {pa.action} with {alt_model}"
                                )
                            else:
                                suggestion = (
                                    f"Reduce {pa.action} volume to {rng[1]}uL (pipette max), "
                                    f"or add a larger pipette to your config."
                                )
                            result._checks.append(CheckResult(
                                violation_type=ViolationType.PIPETTE_CAPACITY,
                                severity=Severity.ERROR,
                                step=step.order,
                                what=(
                                    f"{pa.action} volume ({pa_vol}uL) exceeds "
                                    f"{pip_model} capacity ({rng[1]}uL)"
                                ),
                                why=(
                                    f"The {pip_model} is selected for this step because the "
                                    f"transfer volume is {transfer_vol}uL. But the post-transfer "
                                    f"{pa.action} requires {pa_vol}uL, which this pipette cannot "
                                    f"physically aspirate."
                                ),
                                suggestion=suggestion,
                                values={
                                    "requested_volume": pa_vol,
                                    "pipette_max": rng[1],
                                    "transfer_volume": transfer_vol,
                                    "transfer_mount": transfer_mount,
                                    "action": pa.action,
                                }
                            ))

    def _check_volume_against_pipettes(self, step: ExtractedStep, volume: float,
                                        context: str, result: PhysicalConstraintsCheckResult):
        """Check if any configured pipette can handle this volume."""
        can_handle = any(lo <= volume <= hi for lo, hi in self.pipette_ranges.values())
        if not can_handle:
            available = ", ".join(
                f"{self.config['pipettes'][m]['model']} ({lo}-{hi}uL)"
                for m, (lo, hi) in self.pipette_ranges.items()
            )
            result._checks.append(CheckResult(
                violation_type=ViolationType.PIPETTE_CAPACITY,
                severity=Severity.ERROR,
                step=step.order,
                what=f"No pipette can handle {volume}uL",
                why=f"Available pipettes: {available}. None covers {volume}uL.",
                suggestion="Add an appropriate pipette to your config, or adjust the volume.",
                values={"requested_volume": volume, "available_ranges": dict(self.pipette_ranges)}
            ))

    def _verify_labwares_exist_in_config(self, step: ExtractedStep, result: PhysicalConstraintsCheckResult):
        """Check that source/destination have been resolved to config labware.

        Augmented with a capability fallback: when the literal label lookup
        misses but the ref's referenced wells uniquely fit ONE config
        labware, rescue the lookup and emit a PASSED record naming the
        capability match. This stops the constraint check from being a
        pure string-equality gate while keeping it strict (ambiguous
        multi-fit still fails, no fit still fails — the fallback only
        rescues unique fits).
        """
        from nl2protocol.extraction.resolver import _capability_filter
        config_labels = set(self.config.get("labware", {}).keys())
        if not config_labels:
            return

        for ref, role in [(step.source, "source"), (step.destination, "destination")]:
            if not ref:
                continue
            # Check resolved_label, fall back to description for legacy specs
            label = ref.resolved_label or ref.description
            if label not in config_labels:
                # Capability fallback: maybe the description is non-canonical
                # but the wells uniquely fit a known labware.
                ref_wells = self._wells_of_ref(ref)
                if ref_wells:
                    survivors = _capability_filter(ref_wells, self.config)
                    if len(survivors) == 1:
                        rescued = survivors[0]
                        slot = self.config["labware"][rescued].get("slot")
                        phrase = (
                            f"rescued via capability fit: '{ref.description}' "
                            f"→ '{rescued}'"
                            + (f" at slot {slot}" if slot else "")
                        )
                        result._checks.append(CheckResult(
                            violation_type=ViolationType.LABWARE_NOT_FOUND,
                            severity=Severity.PASSED,
                            step=step.order,
                            what=phrase,
                            detail_label=f"{role}_labware",
                            values={"role": role, "label": rescued,
                                     "slot": slot,
                                     "rescued_from": ref.description},
                        ))
                        continue
                # Original failure path. Mention the capability-fallback
                # outcome in `why` so the user can tell whether the miss
                # is "no labware fits these wells" vs "multiple fit
                # ambiguously".
                survivors = (_capability_filter(ref_wells, self.config)
                             if ref_wells else [])
                if ref_wells and not survivors:
                    why = (
                        f"Wells {sorted(ref_wells)} do not fit any "
                        f"configured labware. Available labware: "
                        f"{sorted(config_labels)}."
                    )
                elif ref_wells and len(survivors) > 1:
                    why = (
                        f"Wells fit multiple labware ambiguously: "
                        f"{sorted(survivors)}. Resolver couldn't pick."
                    )
                else:
                    why = (
                        f"Available labware: {sorted(config_labels)}. "
                        f"No match found."
                    )
                result._checks.append(CheckResult(
                    violation_type=ViolationType.LABWARE_NOT_FOUND,
                    severity=Severity.ERROR,
                    step=step.order,
                    what=f"Cannot resolve {role} '{ref.description}' to any configured labware",
                    why=why,
                    suggestion=f"Check your config has labware matching '{ref.description}', or rename it.",
                    values={"description": ref.description, "role": role, "available": sorted(config_labels)}
                ))
            else:
                # Pass branch: the labware resolved to a config entry.
                # Emit the inline phrase ("loaded at slot N") for the
                # source/destination row in the spec view. Slot may be
                # absent on legacy configs — fall back to a generic
                # phrase so the green tag still appears (the resolution
                # itself is the verified claim).
                slot = self.config.get("labware", {}).get(label, {}).get("slot")
                phrase = (f"loaded at slot {slot}" if slot
                          else f"resolves to config labware '{label}'")
                result._checks.append(CheckResult(
                    violation_type=ViolationType.LABWARE_NOT_FOUND,
                    severity=Severity.PASSED,
                    step=step.order,
                    what=phrase,
                    detail_label=f"{role}_labware",
                    values={"role": role, "label": label, "slot": slot},
                ))

    def _verify_referenced_wells_exist_in_config(self, step: ExtractedStep, result: PhysicalConstraintsCheckResult):
        """Check that referenced wells exist on the target labware."""
        from nl2protocol.models.labware import get_well_info

        for ref, role in [(step.source, "source"), (step.destination, "destination")]:
            if not ref:
                continue

            # Resolve labware to get well info
            label = self._resolve_ref_to_label(ref)
            if not label:
                continue  # Already caught by labware resolution check

            lw_config = self.config.get("labware", {}).get(label, {})
            load_name = lw_config.get("load_name", "")
            if not load_name:
                # Labware entry exists but has no load_name. Config validation
                # is responsible for catching this; constraint checking skips.
                continue
            try:
                well_info = get_well_info(load_name)
            except ValueError:
                # Unknown load_name. The config validator's
                # `_validate_config_labware_against_api` is responsible for
                # flagging this; we don't double-report from here.
                continue

            valid_wells = set(well_info.get("valid_wells", []))
            if not valid_wells:
                continue

            rows = well_info.get("rows", "?")
            cols = well_info.get("cols", "?")
            # Track whether any well-validity issue was raised on this
            # (step, role). If none, a single PASSED record covers the
            # entire wells row in the spec view ("wells: A1, A2, A3, A4
            # ✓ exist on reagent_rack (4x6)").
            wells_passed = True

            # Check single well
            if ref.well and ref.well not in valid_wells:
                wells_passed = False
                result._checks.append(CheckResult(
                    violation_type=ViolationType.WELL_INVALID,
                    severity=Severity.ERROR,
                    step=step.order,
                    what=f"Well {ref.well} does not exist on '{label}' ({load_name})",
                    why=f"This labware has {well_info.get('rows', '?')} rows x {well_info.get('cols', '?')} columns.",
                    suggestion=f"Valid wells: row range A-{chr(64 + well_info.get('rows', 8))}, columns 1-{well_info.get('cols', 12)}.",
                    values={"well": ref.well, "labware": label, "load_name": load_name}
                ))

            # Check well list
            if ref.wells:
                invalid = [w for w in ref.wells if w not in valid_wells]
                if invalid:
                    wells_passed = False
                    result._checks.append(CheckResult(
                        violation_type=ViolationType.WELL_INVALID,
                        severity=Severity.ERROR,
                        step=step.order,
                        what=f"Wells {invalid[:5]} do not exist on '{label}' ({load_name})",
                        why=f"This labware has {well_info.get('rows', '?')} rows x {well_info.get('cols', '?')} columns.",
                        suggestion=f"Valid well range: A1-{chr(64 + well_info.get('rows', 8))}{well_info.get('cols', 12)}.",
                        values={"invalid_wells": invalid, "labware": label}
                    ))

            # Pass branch: every referenced well exists on the labware
            # AND the ref actually referenced at least one well (an
            # empty ref doesn't earn a pass tag). The phrase ("exist
            # on reagent_rack (4x6)" / "exists on ...") matches the
            # row's plurality.
            ref_has_well = bool(ref.well or ref.wells)
            if ref_has_well and wells_passed:
                verb = "exists on" if (ref.well and not ref.wells) else "exist on"
                result._checks.append(CheckResult(
                    violation_type=ViolationType.WELL_INVALID,
                    severity=Severity.PASSED,
                    step=step.order,
                    what=f"{verb} {label} ({rows}\u00d7{cols})",
                    detail_label=f"{role}_wells",
                    values={"role": role, "labware": label,
                             "rows": rows, "cols": cols},
                ))

    def _verify_modules_exist_in_config(self, spec: ProtocolSpec, result: PhysicalConstraintsCheckResult):
        """Check that module commands reference modules that exist in config."""
        module_actions = {
            "set_temperature": "temperature",
            "wait_for_temperature": "temperature",
            "engage_magnets": "magnetic",
            "disengage_magnets": "magnetic",
            "deactivate": None,  # any module
        }

        config_modules = self.config.get("modules", {})
        config_module_types = {mod["module_type"] for mod in config_modules.values()}

        for step in spec.steps:
            if step.action in module_actions:
                required_type = module_actions[step.action]
                if required_type and required_type not in config_module_types:
                    result._checks.append(CheckResult(
                        violation_type=ViolationType.MODULE_NOT_FOUND,
                        severity=Severity.ERROR,
                        step=step.order,
                        what=f"Action '{step.action}' requires a {required_type} module, but none configured",
                        why=f"Your config has modules: {list(config_modules.keys()) or 'none'}.",
                        suggestion=f"Add a {required_type} module to your config JSON.",
                        values={"required_type": required_type, "action": step.action}
                    ))

    def _check_labware_on_module(self, spec: ProtocolSpec, result: PhysicalConstraintsCheckResult):
        """Warn if protocol uses temperature commands but no labware sits on the module."""
        temp_actions = {"set_temperature", "wait_for_temperature"}
        uses_temp = any(step.action in temp_actions for step in spec.steps)
        if not uses_temp:
            return

        config_modules = self.config.get("modules", {})
        temp_modules = {label for label, mod in config_modules.items()
                        if mod.get("module_type") == "temperature"}
        if not temp_modules:
            return  # _check_module_availability already handles missing modules

        # Check if any labware has on_module pointing to a temperature module
        config_labware = self.config.get("labware", {})
        labware_on_temp = any(
            lw.get("on_module") in temp_modules
            for lw in config_labware.values()
        )

        if not labware_on_temp:
            result._checks.append(CheckResult(
                violation_type=ViolationType.MODULE_LABWARE_MISMATCH,
                severity=Severity.WARNING,
                step=0,
                what="Protocol uses temperature control but no labware is on the temperature module",
                why=(
                    "Temperature commands will heat/cool the module, but labware on a "
                    "separate slot won't be affected. If your samples need temperature "
                    "control, the labware must sit on the module."
                ),
                suggestion=(
                    'Add "on_module": "<module_label>" to the labware entry in your '
                    "config.json, and set its slot to match the module's slot."
                ),
                values={"temp_modules": list(temp_modules)}
            ))

    def _check_tip_sufficiency(self, spec: ProtocolSpec, result: PhysicalConstraintsCheckResult):
        """Estimate tip usage per pipette and warn if any exceeds available tips."""
        liquid_actions = {"transfer", "distribute", "consolidate", "mix",
                          "serial_dilution", "aspirate", "dispense"}

        # Count tips per mount
        tip_usage = {mount: 0 for mount in self.pipette_ranges}

        for step in spec.steps:
            if step.action not in liquid_actions:
                continue

            # Determine which mount handles this step's volume
            mount = self._mount_for_volume(step.volume.value if step.volume else 0)
            if not mount:
                continue

            if step.tip_strategy == "same_tip":
                tip_usage[mount] += 1
            else:
                wells = self._estimate_well_count(step)
                tip_usage[mount] += wells

        # Count available tips per mount
        tips_available = {mount: 0 for mount in self.pipette_ranges}
        for mount, pip in self.config.get("pipettes", {}).items():
            for tr_label in pip.get("tipracks", []):
                tr = self.config.get("labware", {}).get(tr_label, {})
                load = tr.get("load_name", "")
                tips_available[mount] += 384 if "384" in load else 96

        # Check each pipette independently
        for mount in self.pipette_ranges:
            used = tip_usage.get(mount, 0)
            available = tips_available.get(mount, 0)
            if used > available > 0:
                model = self.config["pipettes"][mount].get("model", mount)
                result._checks.append(CheckResult(
                    violation_type=ViolationType.TIP_INSUFFICIENT,
                    severity=Severity.ERROR,
                    step=0,
                    what=f"{model} ({mount}) may need ~{used} tips but only {available} available",
                    why="If tips run out mid-protocol, the robot will halt and require manual intervention.",
                    suggestion=f"Add more tipracks for {model}, or use 'same_tip' strategy where cross-contamination isn't a concern.",
                    values={"mount": mount, "estimated_tips": used, "available_tips": available}
                ))

    def _mount_for_volume(self, volume: float) -> Optional[str]:
        """Determine which pipette mount handles a given volume."""
        if not volume:
            return None
        # Prefer the smallest pipette that can handle the volume
        best = None
        best_max = float('inf')
        for mount, (lo, hi) in self.pipette_ranges.items():
            if lo <= volume <= hi and hi < best_max:
                best = mount
                best_max = hi
        # Fallback: if no pipette covers it exactly, pick the closest
        if not best and self.pipette_ranges:
            best = min(self.pipette_ranges, key=lambda m: abs(volume - sum(self.pipette_ranges[m]) / 2))
        return best

    @staticmethod
    def _estimate_well_count(step) -> int:
        """Estimate number of wells a step touches (for tip counting)."""
        wells = 1
        if step.destination:
            if step.destination.wells:
                wells = len(step.destination.wells)
            elif step.destination.well_range:
                wells = max(wells, 8)
        if step.source and step.source.wells:
            wells = max(wells, len(step.source.wells))
        if step.replicates:
            wells *= step.replicates
        return wells

    # ========================================================================
    # HELPERS
    # ========================================================================

    def _resolve_ref_to_label(self, ref) -> Optional[str]:
        """Read pre-resolved config label from a LocationRef."""
        if not ref or "labware" not in self.config:
            return None
        # Use resolved_label if available, fall back to description for legacy specs
        label = ref.resolved_label or ref.description
        if label in self.config["labware"]:
            return label
        return None

    def _wells_of_ref(self, ref) -> set:
        """Aggregate every well referenced by a LocationRef into a set.

        Pre:    `ref` is a LocationRef (any of `well`, `wells`,
                `well_range` may be None).
        Post:   Returns the union of `{ref.well}` (if set), `ref.wells`
                (if set), and `expand_well_range(ref.well_range)` (if
                set). Empty set when none of those carry well data —
                callers should treat that as "no wells referenced".
        Side effects: None. Imports `expand_well_range` lazily to avoid
                      a cycle between validation and extraction modules.
        """
        from nl2protocol.extraction.schema_builder import expand_well_range
        out: set = set()
        if ref is None:
            return out
        if ref.well:
            out.add(ref.well)
        if ref.wells:
            out.update(ref.wells)
        if ref.well_range:
            out.update(expand_well_range(ref.well_range))
        return out


# ============================================================================
# WELL STATE TRACKER
# ============================================================================

@dataclass
class WellState:
    """State of a single well at a point in time."""
    volume_ul: float = 0.0
    substances: List[str] = field(default_factory=list)

    def add(self, volume: float, substance: str = "unknown"):
        """Add liquid (and optionally record a substance label) to this well.

        Pre:    `volume` is a non-negative real number in microliters.
                `substance` is a string (may be empty). The caller is
                responsible for any unit conversion. Behavior on negative
                `volume` is undefined.

        Post:   `self.volume_ul` is incremented by exactly `volume` (no
                clamping; no maximum check — well-capacity overflow detection
                is the caller's responsibility, e.g. via
                `WellStateTracker.dispense`).
                `self.substances` has `substance` appended at the end iff
                `substance` is non-empty AND `substance` is not already
                present (exact string equality, case-sensitive). Otherwise
                `self.substances` is unchanged.

        Side effects: Mutates `self.volume_ul`. Possibly mutates
                      `self.substances` (appends one element).

        Raises: Never.
        """
        self.volume_ul += volume
        if substance and substance not in self.substances:
            self.substances.append(substance)

    def remove(self, volume: float) -> bool:
        """Try to remove `volume` from this well. Returns False if insufficient.

        Pre:    `volume` is a non-negative real number in microliters.
                Behavior on negative `volume` is undefined.

        Post:   Returns True iff `volume <= self.volume_ul + 0.01` (a fixed
                0.01uL tolerance for float drift).
                On True:  `self.volume_ul` is set to
                          `max(0.0, self.volume_ul - volume)` — clamped at
                          0, so consuming a within-tolerance overdraw leaves
                          the well at exactly 0.
                          `self.substances` is unchanged.
                On False: `self.volume_ul` and `self.substances` are both
                          unchanged.

        Side effects: Mutates `self.volume_ul` only when returning True.
                      Never mutates `self.substances`.

        Raises: Never.
        """
        if volume > self.volume_ul + 0.01:  # small tolerance
            return False
        self.volume_ul = max(0, self.volume_ul - volume)
        return True


class WellStateTracker:
    """Tracks what's in every well throughout protocol execution.

    Used during spec_to_schema to:
      - Detect aspirating from empty wells (wrong source labware)
      - Detect well overflow (volume > well capacity)
      - Verify serial dilution steps don't re-add diluent
      - Provide context for debugging

    This is NOT simulation — it's a lightweight bookkeeping pass that
    catches logical errors the Opentrons simulator would miss (the simulator
    doesn't know what *substance* is in a well, only volume).
    """

    def __init__(self, config: dict, spec: 'ProtocolSpec'):
        self.config = config
        # {labware_label: {well: WellState}}
        self.state: Dict[str, Dict[str, WellState]] = {}
        self.warnings: List[str] = []
        self._warned_wells: set = set()  # Deduplicate: one warning per well
        self._default_volume_wells: set = set()  # Wells that got a fallback volume

        # Initialize from prefilled_labware (uniform fills like "100uL media per well")
        from nl2protocol.models.labware import get_well_info
        for pf in spec.prefilled_labware:
            lw_config = config.get("labware", {}).get(pf.labware, {})
            load_name = lw_config.get("load_name", "")
            if load_name:
                well_info = get_well_info(load_name)
                # Mirror the initial_contents fallback (line 701-708): when
                # the LLM didn't state a volume, use the labware's well
                # capacity as an upper bound so the well-state tracker
                # still knows the substance is present without firing
                # false overflow warnings.
                if pf.volume_ul is not None:
                    vol = pf.volume_ul
                    is_fallback = False
                else:
                    vol = self._get_well_capacity(pf.labware) or 15000.0
                    is_fallback = True
                for well in well_info["valid_wells"]:
                    self._ensure_well(pf.labware, well)
                    self.state[pf.labware][well].add(vol, pf.substance)
                    if is_fallback:
                        self._default_volume_wells.add((pf.labware, well))

        # Initialize from spec's initial_contents (per-well specifics)
        for ic in spec.initial_contents:
            self._ensure_well(ic.labware, ic.well)
            if ic.volume_ul:
                self.state[ic.labware][ic.well].add(ic.volume_ul, ic.substance)
            else:
                # Volume not stated — use well capacity as a reasonable upper bound.
                # This prevents false overflow warnings while still tracking the substance.
                fallback = self._get_well_capacity(ic.labware) or 15000.0
                self.state[ic.labware][ic.well].add(fallback, ic.substance)
                self._default_volume_wells.add((ic.labware, ic.well))
                self.warnings.append(
                    f"'{ic.labware}' well {ic.well} has '{ic.substance}' "
                    f"but no volume stated — assuming {fallback:.0f}uL (well capacity)"
                )

    def _ensure_well(self, labware: str, well: str):
        """Ensure the labware/well exists in state."""
        if labware not in self.state:
            self.state[labware] = {}
        if well not in self.state[labware]:
            self.state[labware][well] = WellState()

    def dispense(self, labware: str, well: str, volume: float, substance: str = "unknown"):
        """Record a dispense (volume + substance) into a tracked well; warn on overflow.

        Pre:    `labware` is a non-empty string — the labware label as used
                in `self.config["labware"]`. (Pydantic guarantees this for
                schema-driven callers; passing None or "" is a contract
                violation and will crash.)
                `well` is a well-name string (e.g. "A1").
                `volume` is a non-negative real number in microliters.
                Behavior on negative `volume` is undefined.
                `substance` is a string (defaults to "unknown"); follows
                `WellState.add` semantics for empty/duplicate handling.

        Post:   Ensures (labware, well) exists in `self.state` (creating an
                empty `WellState` if absent), then records the dispense via
                `WellState.add(volume, substance)`. If the well's cumulative
                tracked volume now exceeds the labware's estimated capacity
                (per `_get_well_capacity`), exactly one overflow warning
                string is appended to `self.warnings` for this call; for
                wells whose initial volume was assumed (i.e. in
                `self._default_volume_wells`), the warning includes an
                additional NOTE about the assumption.

        Side effects: Mutates `self.state[labware][well]` (creating entries
                      if needed) and possibly appends to `self.warnings`.

        Raises: TypeError if `labware` is None (out-of-contract input).
        """
        self._ensure_well(labware, well)
        self.state[labware][well].add(volume, substance)

        # Check for well overflow
        well_capacity = self._get_well_capacity(labware)
        if well_capacity and self.state[labware][well].volume_ul > well_capacity:
            msg = (f"Well {well} in '{labware}' would contain "
                   f"{self.state[labware][well].volume_ul:.1f}uL, "
                   f"exceeding estimated capacity ({well_capacity}uL)")
            if (labware, well) in self._default_volume_wells:
                msg += (" — NOTE: this well's initial volume was assumed "
                        "(no volume in instruction), which may cause this overflow")
            self.warnings.append(msg)

    def aspirate(self, labware: str, well: str, volume: float) -> bool:
        """Record an aspiration from a well. Returns False if it would underflow.

        Pre:    `labware` is a non-empty string — the labware label as used
                in `self.config["labware"]`. (Pydantic guarantees this for
                schema-driven callers; passing None or "" is a contract
                violation and will crash.)
                `well` is a well-name string (e.g. "A1").
                `volume` is a non-negative real number in microliters.
                Behavior on negative `volume` is undefined.

        Post:   Ensures (labware, well) exists in `self.state`. Then:
                  * If the well's tracked volume is < 0.01uL (treated as
                    "empty") and `volume > 0`: returns False; appends one
                    "appears to be empty" warning to `self.warnings` (or
                    suppresses it if a warning for this (labware, well)
                    has already been emitted — see `self._warned_wells`).
                    `self.state` is unchanged.
                  * Otherwise if `volume > tracked_volume + 0.01`: returns
                    False; appends one deduplicated "only has ~X uL" warning.
                    `self.state` is unchanged.
                  * Otherwise: returns True; decrements the well's volume by
                    `volume` (via `WellState.remove`); `self.warnings` is
                    unchanged.

        Side effects: Mutates `self.state[labware][well]` (always at least
                      via `_ensure_well`; volume only on True return).
                      May append to `self.warnings` and `self._warned_wells`.

        Raises: TypeError if `labware` is None (out-of-contract input).
        """
        self._ensure_well(labware, well)
        key = (labware, well)
        current = self.state[labware][well].volume_ul
        if current < 0.01 and volume > 0:
            if key not in self._warned_wells:
                self._warned_wells.add(key)
                self.warnings.append(
                    f"Aspirating from '{labware}' well {well} "
                    f"which appears to be empty (no prior dispense tracked)"
                )
            return False
        if volume > current + 0.01:
            if key not in self._warned_wells:
                self._warned_wells.add(key)
                self.warnings.append(
                    f"Aspirating {volume}uL from '{labware}' well {well} "
                    f"which only has ~{current:.1f}uL"
                )
            return False
        self.state[labware][well].remove(volume)
        return True

    def get_volume(self, labware: str, well: str) -> float:
        """Read the currently tracked volume (uL) for a well.

        Pre:    `labware` and `well` are strings.

        Post:   Returns the tracker's current `volume_ul` for the
                (labware, well) lookup. Returns 0.0 if the lookup is unseen
                (labware not in `self.state`, or well not under that labware).
                NOTE: this conflates "well never tracked" with "well tracked
                at exactly 0uL". Callers needing to distinguish must inspect
                `self.state` directly. This inspector is intended for tests
                and external observers; internal logic should not branch on
                its return value.

        Side effects: None. Pure read.

        Raises: Never.
        """
        if labware in self.state and well in self.state[labware]:
            return self.state[labware][well].volume_ul
        return 0.0

    def get_substances(self, labware: str, well: str) -> List[str]:
        """Read the list of substance labels recorded in a well.

        Pre:    `labware` and `well` are strings.

        Post:   Returns a fresh list (shallow copy) of the substance labels
                for the (labware, well) lookup. Returns [] if the lookup is
                unseen. Mutating the returned list does NOT affect the
                tracker's state.

        Side effects: None. Pure read.

        Raises: Never.
        """
        if labware in self.state and well in self.state[labware]:
            return list(self.state[labware][well].substances)
        return []

    def well_has_liquid(self, labware: str, well: str) -> bool:
        """Return True iff the well has more than 0.01uL tracked.

        Pre:    `labware` and `well` are strings.

        Post:   Returns True iff `self.get_volume(labware, well) > 0.01`.
                Equivalently: True iff the (labware, well) lookup has been
                seen by the tracker AND its tracked volume strictly exceeds
                the 0.01uL float-tolerance threshold. False for unseen
                lookups or for wells tracked at 0.0-0.01uL.

        Side effects: None. Pure read.

        Raises: Never.
        """
        return self.get_volume(labware, well) > 0.01

    @staticmethod
    def _get_well_capacity_static(labware: str, config: dict) -> Optional[float]:
        """Estimate well capacity from labware load_name (class-level, no instance needed)."""
        lw_config = config.get("labware", {}).get(labware, {})
        load_name = lw_config.get("load_name", "").lower()
        return WellStateTracker._capacity_from_load_name(load_name)

    def _get_well_capacity(self, labware: str) -> Optional[float]:
        """Estimate well capacity from labware load_name."""
        lw_config = self.config.get("labware", {}).get(labware, {})
        load_name = lw_config.get("load_name", "").lower()
        return self._capacity_from_load_name(load_name)

    @staticmethod
    def _capacity_from_load_name(load_name: str) -> Optional[float]:
        """Parse capacity from a load_name string."""

        # Common capacity patterns from load names
        if "1.5ml" in load_name or "1500ul" in load_name:
            return 1500.0
        if "2ml" in load_name or "2000ul" in load_name:
            return 2000.0
        if "100ul" in load_name:
            return 100.0
        if "200ul" in load_name:
            return 200.0
        if "360ul" in load_name:
            return 360.0
        if "50ml" in load_name:
            return 50000.0
        if "15ml" in load_name:
            return 15000.0
        if "reservoir" in load_name:
            return 22000.0  # typical 12-well reservoir per channel
        return None
