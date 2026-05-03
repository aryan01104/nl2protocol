"""
validation — Deterministic constraint checking.

constraints.py      — Hardware constraint checker + well state tracking
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
