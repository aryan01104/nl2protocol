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

from .extractor import ProtocolSpec, ExtractedStep, VolumeSpec


# ============================================================================
# CONSTRAINT VIOLATION TYPES
# ============================================================================

class Severity(Enum):
    """How bad is this violation?"""
    ERROR = "error"        # Cannot proceed — physically impossible
    WARNING = "warning"    # Can proceed but result may be suboptimal
    INFO = "info"          # FYI — scientist should be aware


class ViolationType(Enum):
    """What kind of constraint was violated?"""
    PIPETTE_CAPACITY = "pipette_capacity"
    PIPETTE_MINIMUM = "pipette_minimum"
    WELL_INVALID = "well_invalid"
    LABWARE_NOT_FOUND = "labware_not_found"
    MODULE_NOT_FOUND = "module_not_found"
    TIP_INSUFFICIENT = "tip_insufficient"
    VOLUME_EXCEEDS_WELL = "volume_exceeds_well"
    SLOT_CONFLICT = "slot_conflict"


@dataclass
class ConstraintViolation:
    """A single constraint violation with full context for the scientist."""
    violation_type: ViolationType
    severity: Severity
    step: int                          # Which step has the problem
    what: str                          # What's wrong (factual, short)
    why: str                           # Why it matters (domain context)
    suggestion: str                    # How to fix it
    values: Dict[str, object] = field(default_factory=dict)  # Machine-readable details

    def __str__(self) -> str:
        icon = {"error": "ERROR", "warning": "WARN", "info": "INFO"}[self.severity.value]
        return f"[{icon}] Step {self.step}: {self.what}\n       Why: {self.why}\n       Suggestion: {self.suggestion}"


@dataclass
class ConstraintCheckResult:
    """Result of running all constraint checks."""
    violations: List[ConstraintViolation] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(v.severity == Severity.ERROR for v in self.violations)

    @property
    def has_warnings(self) -> bool:
        return any(v.severity == Severity.WARNING for v in self.violations)

    @property
    def errors(self) -> List[ConstraintViolation]:
        return [v for v in self.violations if v.severity == Severity.ERROR]

    @property
    def warnings(self) -> List[ConstraintViolation]:
        return [v for v in self.violations if v.severity == Severity.WARNING]

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
    """Get (min, max) volume range for a pipette model string."""
    model_lower = model.lower()
    for key, (lo, hi) in PIPETTE_SPECS.items():
        if key in model_lower:
            return (lo, hi)
    return None


def get_pipette_for_volume(volume: float, config: dict) -> Optional[str]:
    """Find which mount can handle this volume. Returns mount or None."""
    if "pipettes" not in config:
        return None
    for mount, pip in config["pipettes"].items():
        rng = get_pipette_range(pip["model"])
        if rng and rng[0] <= volume <= rng[1]:
            return mount
    return None


def get_all_pipette_ranges(config: dict) -> Dict[str, Tuple[float, float]]:
    """Get {mount: (min, max)} for all configured pipettes."""
    result = {}
    for mount, pip in config.get("pipettes", {}).items():
        rng = get_pipette_range(pip["model"])
        if rng:
            result[mount] = rng
    return result


# ============================================================================
# CONSTRAINT CHECKER
# ============================================================================

class ConstraintChecker:
    """Deterministic constraint checker for ProtocolSpec against lab config.

    Run this AFTER extraction, BEFORE schema generation. Surfaces all
    hardware conflicts so the scientist can make informed decisions.
    """

    def __init__(self, config: dict):
        self.config = config
        self.pipette_ranges = get_all_pipette_ranges(config)

    def check_all(self, spec: ProtocolSpec) -> ConstraintCheckResult:
        """Run all constraint checks. Returns structured result."""
        result = ConstraintCheckResult()

        # Check for labware the instruction references but config doesn't have
        self._check_missing_labware(spec, result)

        for step in spec.steps:
            self._check_pipette_capacity(step, result)
            self._check_labware_resolution(step, result)
            self._check_well_validity(step, result)

        self._check_module_availability(spec, result)
        self._check_tip_sufficiency(spec, result)

        return result

    # ========================================================================
    # INDIVIDUAL CHECKS
    # ========================================================================

    def _check_missing_labware(self, spec: ProtocolSpec, result: ConstraintCheckResult):
        """Check if the LLM flagged any labware as missing from config."""
        if not spec.missing_labware:
            return
        for lw_desc in spec.missing_labware:
            result.violations.append(ConstraintViolation(
                violation_type=ViolationType.LABWARE_NOT_FOUND,
                severity=Severity.ERROR,
                step=0,
                what=f"Instruction references '{lw_desc}' but it's not in your lab config",
                why=(
                    f"The protocol requires this labware but your config.json doesn't define it. "
                    f"Without it, the robot doesn't know which physical deck slot to use."
                ),
                suggestion=(
                    f"Add '{lw_desc}' to your config.json with the correct Opentrons load_name "
                    f"and deck slot. Example:\n"
                    f"         \"{lw_desc.replace(' ', '_')}\": {{"
                    f"\"load_name\": \"<opentrons_labware_name>\", \"slot\": \"<slot_number>\"}}"
                ),
                values={"missing": lw_desc}
            ))

    def _check_pipette_capacity(self, step: ExtractedStep, result: ConstraintCheckResult):
        """Check that every volume in this step can be handled by available pipettes."""

        # Check step volume
        if step.volume and not step.volume.inferred:
            vol = step.volume.value
            if step.volume.unit == "mL":
                vol *= 1000
            self._check_volume_against_pipettes(step, vol, "transfer", result)

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
                            result.violations.append(ConstraintViolation(
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
                                        context: str, result: ConstraintCheckResult):
        """Check if any configured pipette can handle this volume."""
        can_handle = any(lo <= volume <= hi for lo, hi in self.pipette_ranges.values())
        if not can_handle:
            available = ", ".join(
                f"{self.config['pipettes'][m]['model']} ({lo}-{hi}uL)"
                for m, (lo, hi) in self.pipette_ranges.items()
            )
            result.violations.append(ConstraintViolation(
                violation_type=ViolationType.PIPETTE_CAPACITY,
                severity=Severity.ERROR,
                step=step.order,
                what=f"No pipette can handle {volume}uL",
                why=f"Available pipettes: {available}. None covers {volume}uL.",
                suggestion="Add an appropriate pipette to your config, or adjust the volume.",
                values={"requested_volume": volume, "available_ranges": dict(self.pipette_ranges)}
            ))

    def _check_labware_resolution(self, step: ExtractedStep, result: ConstraintCheckResult):
        """Check that source/destination descriptions can map to config labware."""
        config_labels = set(self.config.get("labware", {}).keys())
        if not config_labels:
            return

        for ref, role in [(step.source, "source"), (step.destination, "destination")]:
            if not ref:
                continue
            desc = ref.description.lower().replace("(inferred from config)", "").strip()
            desc_words = set(re.split(r'[\s_\-]+', desc))

            # Try to match — same logic as resolve_labware but just checking existence
            matched = False
            for label in config_labels:
                label_lower = label.lower()
                label_words = set(re.split(r'[\s_\-]+', label_lower))
                # Exact match
                if label_lower == desc:
                    matched = True
                    break
                # Word overlap
                if desc_words & label_words:
                    matched = True
                    break
                # Substring
                if any(lw in desc or desc in lw for lw in [label_lower]):
                    matched = True
                    break

            if not matched:
                result.violations.append(ConstraintViolation(
                    violation_type=ViolationType.LABWARE_NOT_FOUND,
                    severity=Severity.ERROR,
                    step=step.order,
                    what=f"Cannot resolve {role} '{ref.description}' to any configured labware",
                    why=f"Available labware: {sorted(config_labels)}. No match found.",
                    suggestion=f"Check your config has labware matching '{ref.description}', or rename it.",
                    values={"description": ref.description, "role": role, "available": sorted(config_labels)}
                ))

    def _check_well_validity(self, step: ExtractedStep, result: ConstraintCheckResult):
        """Check that referenced wells exist on the target labware."""
        from .parser import get_well_info

        for ref, role in [(step.source, "source"), (step.destination, "destination")]:
            if not ref:
                continue

            # Resolve labware to get well info
            label = self._resolve_label(ref)
            if not label:
                continue  # Already caught by labware resolution check

            lw_config = self.config.get("labware", {}).get(label, {})
            load_name = lw_config.get("load_name", "")
            well_info = get_well_info(load_name)
            if not well_info:
                continue

            valid_wells = set(well_info.get("valid_wells", []))
            if not valid_wells:
                continue

            # Check single well
            if ref.well and ref.well not in valid_wells:
                result.violations.append(ConstraintViolation(
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
                    result.violations.append(ConstraintViolation(
                        violation_type=ViolationType.WELL_INVALID,
                        severity=Severity.ERROR,
                        step=step.order,
                        what=f"Wells {invalid[:5]} do not exist on '{label}' ({load_name})",
                        why=f"This labware has {well_info.get('rows', '?')} rows x {well_info.get('cols', '?')} columns.",
                        suggestion=f"Valid well range: A1-{chr(64 + well_info.get('rows', 8))}{well_info.get('cols', 12)}.",
                        values={"invalid_wells": invalid, "labware": label}
                    ))

    def _check_module_availability(self, spec: ProtocolSpec, result: ConstraintCheckResult):
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
                    result.violations.append(ConstraintViolation(
                        violation_type=ViolationType.MODULE_NOT_FOUND,
                        severity=Severity.ERROR,
                        step=step.order,
                        what=f"Action '{step.action}' requires a {required_type} module, but none configured",
                        why=f"Your config has modules: {list(config_modules.keys()) or 'none'}.",
                        suggestion=f"Add a {required_type} module to your config JSON.",
                        values={"required_type": required_type, "action": step.action}
                    ))

    def _check_tip_sufficiency(self, spec: ProtocolSpec, result: ConstraintCheckResult):
        """Estimate tip usage and warn if it might exceed available tips."""
        # Count approximate tip usage
        tip_count = 0
        for step in spec.steps:
            liquid_actions = {"transfer", "distribute", "consolidate", "mix",
                              "serial_dilution", "aspirate", "dispense"}
            if step.action not in liquid_actions:
                continue

            if step.tip_strategy == "same_tip":
                tip_count += 1
            else:
                # Estimate wells involved
                wells = 1
                if step.destination:
                    if step.destination.wells:
                        wells = len(step.destination.wells)
                    elif step.destination.well_range:
                        # Rough estimate from range
                        wells = max(wells, 8)  # conservative
                if step.source and step.source.wells:
                    wells = max(wells, len(step.source.wells))
                if step.replicates:
                    wells *= step.replicates
                tip_count += wells

        # Count available tips
        available_tips = 0
        if "pipettes" in self.config:
            for pip in self.config["pipettes"].values():
                for tr_label in pip.get("tipracks", []):
                    tr = self.config.get("labware", {}).get(tr_label, {})
                    load = tr.get("load_name", "")
                    if "384" in load:
                        available_tips += 384
                    else:
                        available_tips += 96

        if tip_count > available_tips and available_tips > 0:
            result.violations.append(ConstraintViolation(
                violation_type=ViolationType.TIP_INSUFFICIENT,
                severity=Severity.WARNING,
                step=0,
                what=f"Protocol may need ~{tip_count} tips but only ~{available_tips} available",
                why="If tips run out mid-protocol, the robot will halt and require manual intervention.",
                suggestion="Add more tiprack(s) to your config, or use 'same_tip' strategy where cross-contamination isn't a concern.",
                values={"estimated_tips": tip_count, "available_tips": available_tips}
            ))

    # ========================================================================
    # HELPERS
    # ========================================================================

    def _resolve_label(self, ref) -> Optional[str]:
        """Quick labware resolution for constraint checking."""
        if not ref or "labware" not in self.config:
            return None
        desc = ref.description.lower().replace("(inferred from config)", "").strip()
        desc_words = set(re.split(r'[\s_\-]+', desc))

        for label in self.config["labware"]:
            label_words = set(re.split(r'[\s_\-]+', label.lower()))
            if label.lower() == desc or (desc_words & label_words):
                return label
        return None


# ============================================================================
# WELL STATE TRACKER
# ============================================================================

@dataclass
class WellState:
    """State of a single well at a point in time."""
    volume_ul: float = 0.0
    substances: List[str] = field(default_factory=list)

    def add(self, volume: float, substance: str = "unknown"):
        self.volume_ul += volume
        if substance and substance not in self.substances:
            self.substances.append(substance)

    def remove(self, volume: float) -> bool:
        """Remove volume. Returns False if insufficient."""
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

        # Initialize from spec's initial_contents
        for ic in spec.initial_contents:
            self._ensure_well(ic.labware, ic.well)
            if ic.volume_ul:
                self.state[ic.labware][ic.well].add(ic.volume_ul, ic.substance)
            else:
                # Stock/reagent — has liquid but unknown volume. Treat as infinite.
                self.state[ic.labware][ic.well].add(100000.0, ic.substance)

    def _ensure_well(self, labware: str, well: str):
        """Ensure the labware/well exists in state."""
        if labware not in self.state:
            self.state[labware] = {}
        if well not in self.state[labware]:
            self.state[labware][well] = WellState()

    def dispense(self, labware: str, well: str, volume: float, substance: str = "unknown"):
        """Record a dispense into a well."""
        if not labware:
            return
        self._ensure_well(labware, well)
        self.state[labware][well].add(volume, substance)

        # Check for well overflow (approximate — use 2000uL as upper bound)
        well_capacity = self._get_well_capacity(labware)
        if well_capacity and self.state[labware][well].volume_ul > well_capacity:
            self.warnings.append(
                f"Well {well} in '{labware}' would contain "
                f"{self.state[labware][well].volume_ul:.1f}uL, "
                f"exceeding estimated capacity ({well_capacity}uL)"
            )

    def aspirate(self, labware: str, well: str, volume: float) -> bool:
        """Record an aspiration from a well. Returns False if well is empty/insufficient."""
        if not labware:
            return True  # Can't check without labware
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
        """Get current volume in a well."""
        if labware in self.state and well in self.state[labware]:
            return self.state[labware][well].volume_ul
        return 0.0

    def get_substances(self, labware: str, well: str) -> List[str]:
        """Get what's in a well."""
        if labware in self.state and well in self.state[labware]:
            return self.state[labware][well].substances
        return []

    def well_has_liquid(self, labware: str, well: str) -> bool:
        """Check if a well has any liquid in it."""
        return self.get_volume(labware, well) > 0.01

    def _get_well_capacity(self, labware: str) -> Optional[float]:
        """Estimate well capacity from labware load_name."""
        lw_config = self.config.get("labware", {}).get(labware, {})
        load_name = lw_config.get("load_name", "").lower()

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
