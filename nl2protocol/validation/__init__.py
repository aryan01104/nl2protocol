"""
validation — Deterministic constraint checking and protocol validation.

constraints.py      — Hardware constraint checker + well state tracking
validator.py        — LLM-based protocol validation (cross-checks intent vs output)
validate_config.py  — Config JSON schema validation
input_validator.py  — Classify user input (protocol/question/invalid)
"""

from nl2protocol.validation.constraints import (
    Severity,
    ViolationType,
    ConstraintViolation,
    ConstraintCheckResult,
    ConstraintChecker,
    WellState,
    WellStateTracker,
    PIPETTE_SPECS,
    get_pipette_range,
    get_pipette_for_volume,
    get_all_pipette_ranges,
)

from nl2protocol.validation.validator import (
    ValidationResult,
    validate_protocol,
)
