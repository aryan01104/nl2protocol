from typing import List

from nl2protocol.models.spec import ProtocolSpec, missing_fields


# ========================================================================
# SUFFICIENCY CHECK
# ========================================================================

def verify_no_missing_field(spec: ProtocolSpec) -> List[str]:
    """Return the completeness errors for `spec` (empty list = sufficient).

    Thin human-readable view over `missing_fields` — the structured walk that
    is the single source of truth for action-specific required fields.
    """
    return [mf.message() for mf in missing_fields(spec)]
