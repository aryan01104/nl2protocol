"""
Config validation module for nl2protocol.

Validates lab configuration JSON files before sending to LLM:
1. Schema validation (required fields, types)
2. Labware validation (load_names exist in Opentrons library)
3. Slot conflict detection (no duplicate slots)
4. Pipette mount conflict detection
5. Tiprack reference validation
"""

from typing import Dict, Any, List, Optional, Set
from dataclasses import dataclass, field


@dataclass
class ValidationError:
    """Represents a single validation error."""
    category: str  # 'schema', 'labware', 'slot', 'mount', 'tiprack'
    message: str
    path: Optional[str] = None  # JSON path to the error


@dataclass
class ValidationResult:
    """Result of config validation."""
    valid: bool
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)

    def __str__(self) -> str:
        """Format the result as a human-readable summary string.

        Pre:    `self.valid` and `self.errors` are populated.

        Post:   If `self.valid` is True: returns the single string
                "Config is valid".
                Otherwise: returns a multi-line string starting with
                "Config invalid (N errors):" (where N = `len(self.errors)`)
                followed by one indented line per error in
                "  - [{category}] {message}" format. Warnings are NOT
                included in this string (separate field).

        Side effects: None.

        Raises: Never.
        """
        if self.valid:
            return "Config is valid"
        error_msgs = [f"  - [{e.category}] {e.message}" for e in self.errors]
        return f"Config invalid ({len(self.errors)} errors):\n" + "\n".join(error_msgs)


class ConfigValidator:
    """Validates lab configuration files."""

    # Valid slots for OT-2 (1-11) and Flex (A1-D4)
    OT2_SLOTS = {str(i) for i in range(1, 12)}
    FLEX_SLOTS = {f"{r}{c}" for r in "ABCD" for c in "1234"}
    VALID_SLOTS = OT2_SLOTS | FLEX_SLOTS

    VALID_MOUNTS = {"left", "right"}

    def __init__(self):
        # Cache valid labware names from opentrons_shared_data
        self._valid_labware: Set[str] = set()
        self._shared_data_unavailable = False # is the state for whether we are able to fetch the data from opentrons api
        self._load_valid_labware()

    def _load_valid_labware(self) -> None:
        """Load all valid labware load_names from opentrons_shared_data."""
        try:
            from opentrons_shared_data.labware import list_definitions
            definitions = list(list_definitions())
            # definitions is iterable of (load_name, version) tuples
            self._valid_labware = {d[0] for d in definitions}
        except Exception:
            self._valid_labware = set()
            self._shared_data_unavailable = True

    def validate(self, config: Dict[str, Any]) -> ValidationResult:
        """Run every category of config validation; return a structured result.

        Pre:    `config` is a dict (typically loaded from a lab_config.json).
                `self._valid_labware` was populated (or marked unavailable)
                in `__init__`.

        Post:   Returns a `ValidationResult` with:
                  * `valid` = True iff `len(errors) == 0`.
                  * `errors` = combined list from up to five check categories
                    (in order): schema → labware-against-API → slot
                    conflicts → mount conflicts → tiprack refs.
                  * `warnings` = ["shared_data_unavailable" warning] iff
                    `self._shared_data_unavailable` is True; otherwise [].
                Each error has `category` ∈ {"schema", "labware", "slot",
                "mount", "tiprack"}, a human-readable `message`, and a
                JSON `path` string.
                SHORT-CIRCUIT: secondary checks (categories 2–5) are
                skipped iff `config["labware"]` or `config["pipettes"]`
                is missing OR not a dict. This subsumes both the
                "Missing required field" case AND the "wrong top-level
                type" case (preventing `.items()` crashes on strings).
                Other schema errors (a labware entry that itself isn't
                a dict, missing nested fields like "load_name") do NOT
                trigger the short-circuit — secondary checks tolerate
                those gracefully.

        Side effects: None. Read-only validation.

        Raises: Never.
        """
        errors: List[ValidationError] = []
        warnings: List[ValidationError] = []

        if self._shared_data_unavailable:
            warnings.append(ValidationError(
                category="labware",
                message="opentrons_shared_data unavailable — load_name validation skipped, well layouts will use heuristics",
                path="labware.*.load_name"
            ))

        # 1. Schema validation
        errors.extend(self._validate_schema(config))

        # Secondary checks (2-5) iterate `.items()` on labware/pipettes — only
        # safe when both are dicts. This guard catches both the
        # "Missing required field" case (config.get returns None, not a dict)
        # and the "wrong top-level type" case (config["labware"] is a string).
        labware_ok = isinstance(config.get("labware"), dict)
        pipettes_ok = isinstance(config.get("pipettes"), dict)
        if labware_ok and pipettes_ok:
            # 2. Labware validation
            errors.extend(self._validate_config_labware_against_api(config))

            # 3. Slot conflict detection
            errors.extend(self._check_slot_conflicts(config))

            # 4. Mount conflict detection
            errors.extend(self._check_mount_conflicts(config))

            # 5. Tiprack reference validation
            errors.extend(self._validate_tiprack_refs(config))

        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings
        )

    def _validate_schema(self, config: Dict[str, Any]) -> List[ValidationError]:
        """Validate required fields and types."""
        errors = []

        # Required top-level keys
        if "labware" not in config: # checks if configuration exists
            errors.append(ValidationError(
                category="schema",
                message="Missing required field 'labware'",
                path="labware"
            ))
        elif not isinstance(config["labware"], dict): # checks that configuration is correct type
            errors.append(ValidationError(
                category="schema",
                message="'labware' must be an object",
                path="labware"
            ))

        if "pipettes" not in config: # checks if pipettes exists in config
            errors.append(ValidationError(
                category="schema",
                message="Missing required field 'pipettes'",
                path="pipettes"
            ))
        elif not isinstance(config["pipettes"], dict): # checks if field is of the correct type
            errors.append(ValidationError(
                category="schema",
                message="'pipettes' must be an object",
                path="pipettes"
            ))

        # Validate each labware entry
        if isinstance(config.get("labware"), dict): # this loop checks that for each item in labware, the dict is of the correct format
            for name, lw in config["labware"].items():
                if not isinstance(lw, dict):
                    errors.append(ValidationError(
                        category="schema",
                        message=f"Labware '{name}' must be an object",
                        path=f"labware.{name}"
                    ))
                    continue
                if "load_name" not in lw:
                    errors.append(ValidationError(
                        category="schema",
                        message=f"Labware '{name}' missing 'load_name'",
                        path=f"labware.{name}.load_name"
                    ))
                if "slot" not in lw:
                    errors.append(ValidationError(
                        category="schema",
                        message=f"Labware '{name}' missing 'slot'",
                        path=f"labware.{name}.slot"
                    ))

        # Validate each pipette entry
        if isinstance(config.get("pipettes"), dict):  # this loop checks that for each item in pipettes, the dict is of the correct format
            for mount, pip in config["pipettes"].items():
                if not isinstance(pip, dict):
                    errors.append(ValidationError(
                        category="schema",
                        message=f"Pipette '{mount}' must be an object",
                        path=f"pipettes.{mount}"
                    ))
                    continue
                if "model" not in pip:
                    errors.append(ValidationError(
                        category="schema",
                        message=f"Pipette '{mount}' missing 'model'",
                        path=f"pipettes.{mount}.model"
                    ))

        return errors

    def _validate_config_labware_against_api(self, config: Dict[str, Any]) -> List[ValidationError]:
        """Checks for each labware dict in the given config if:
            - load_name actually exist per opentrons APi
            - slot actually exists for the opentrons robot
        """
        errors = []

        if not self._valid_labware:
            # Skip labware name validation if shared data unavailable
            return errors

        labware = config.get("labware", {})
        for name, lw in labware.items(): # key-thing unpacking of dict, lw is a single dict of a labware and its details
            if not isinstance(lw, dict):
                errors.append(ValidationError(
                    category="labware",
                    message=f"Invalid type '{lw}'. lw is not a dict",
                    path=f"labware.{lw}"
                ))

            load_name = lw.get("load_name", "") # checks if the load_name for our lw exists in the API
            if load_name not in self._valid_labware:
                errors.append(ValidationError(
                    category="labware",
                    message=f"Unknown labware load_name: '{load_name}'",
                    path=f"labware.{name}.load_name"
                ))

            # Validate slot
            slot = lw.get("slot", "") # checks if the slot for our lw exists for the opentrons robot
            if slot and slot not in self.VALID_SLOTS:
                errors.append(ValidationError(
                    category="labware",
                    message=f"Invalid slot '{slot}'. Valid OT-2 slots: 1-11, Flex slots: A1-D4",
                    path=f"labware.{name}.slot"
                ))

        return errors

    def _check_slot_conflicts(self, config: Dict[str, Any]) -> List[ValidationError]:
        """Check for duplicate slot assignments."""
        errors = []
        slot_usage: Dict[str, List[str]] = {}

        labware = config.get("labware", {})
        for name, lw in labware.items():
            if not isinstance(lw, dict):
                continue
            slot = lw.get("slot", "")
            if slot:
                if slot not in slot_usage:
                    slot_usage[slot] = []
                slot_usage[slot].append(name)

        for slot, names in slot_usage.items():
            if len(names) > 1:
                errors.append(ValidationError(
                    category="slot",
                    message=f"Slot '{slot}' assigned to multiple labware: {names}",
                    path=f"labware (slot {slot})"
                ))

        return errors

    def _check_mount_conflicts(self, config: Dict[str, Any]) -> List[ValidationError]:
        """Check for invalid mount assignments."""
        errors = []

        pipettes = config.get("pipettes", {})
        for mount in pipettes.keys():
            if mount not in self.VALID_MOUNTS:
                errors.append(ValidationError(
                    category="mount",
                    message=f"Invalid mount '{mount}'. Must be 'left' or 'right'",
                    path=f"pipettes.{mount}"
                ))

        return errors

    def _validate_tiprack_refs(self, config: Dict[str, Any]) -> List[ValidationError]:
        """Validate that tiprack references exist in labware."""
        errors = []

        labware_keys = set(config.get("labware", {}).keys())
        pipettes = config.get("pipettes", {})

        for mount, pip in pipettes.items():
            if not isinstance(pip, dict):
                continue
            tipracks = pip.get("tipracks", [])
            for tiprack_ref in tipracks:
                if tiprack_ref not in labware_keys:
                    errors.append(ValidationError(
                        category="tiprack",
                        message=f"Tiprack '{tiprack_ref}' not found in labware",
                        path=f"pipettes.{mount}.tipracks"
                    ))

        return errors


def validate_config(config: Dict[str, Any]) -> ValidationResult:
    """Convenience: construct a ConfigValidator and run validate(config).

    Pre:    `config` is a dict.
    Post:   Returns the same ValidationResult that
            `ConfigValidator().validate(config)` would return.
    Side effects: Constructs a fresh ConfigValidator (which lazy-loads the
                  Opentrons labware library on each call — non-trivial cost
                  per call; suitable for one-shot use, not loops).
    Raises: Never.
    """
    validator = ConfigValidator()
    return validator.validate(config)


def validate_config_file(path: str) -> ValidationResult:
    """Read a JSON config file and validate it.

    Pre:    `path` is a string (file path).

    Post:   On success: opens `path`, parses JSON, returns
            `validate_config(parsed_dict)`.
            On FileNotFoundError: returns ValidationResult(valid=False)
            with one error of category="file", message="Config file not
            found: {path}".
            On json.JSONDecodeError: returns ValidationResult(valid=False)
            with one error of category="file", message="Invalid JSON: {e}".

    Side effects: Reads the file at `path` from disk.

    Raises: Never (file-not-found and JSON errors are wrapped in a
            ValidationResult; other exceptions, e.g. PermissionError,
            propagate).
    """
    import json
    try:
        with open(path, 'r') as f:
            config = json.load(f)
        return validate_config(config)
    except FileNotFoundError:
        return ValidationResult(
            valid=False,
            errors=[ValidationError(
                category="file",
                message=f"Config file not found: {path}",
                path=path
            )]
        )
    except json.JSONDecodeError as e:
        return ValidationResult(
            valid=False,
            errors=[ValidationError(
                category="file",
                message=f"Invalid JSON: {e}",
                path=path
            )]
        )
