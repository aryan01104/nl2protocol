from typing import List

from pydantic import ValidationError

from nl2protocol.models.spec import ProtocolSpec, CompleteProtocolSpec


# ========================================================================
# SUFFICIENCY CHECK
# ========================================================================

def verify_no_missing_field(spec: ProtocolSpec) -> List[str]:
    """Check if the spec has enough information to generate a protocol.

    Attempts promotion to CompleteProtocolSpec. If validation fails,
    returns the error messages as a list. Empty list = sufficient.
    """
    try:
        CompleteProtocolSpec.model_validate(spec.model_dump())
        return []
    except ValidationError as e:
        gaps = []
        for err in e.errors():
            msg = err["msg"].removeprefix("Value error, ")
            # The validator joins multiple issues with "; " — split them
            prefix_end = msg.find(": ")
            if prefix_end != -1:
                body = msg[prefix_end + 2:]
                gaps.extend(issue.strip() for issue in body.split("; "))
            else:
                gaps.append(msg)
        return gaps
