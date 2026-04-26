"""
extraction — Protocol extraction pipeline.

extractor.py      — SemanticExtractor (LLM extraction, provenance, gap fill, formatting)
prompts.py        — LLM prompt templates
resolver.py       — LabwareResolver (labware description → config label)
schema_builder.py — spec_to_schema (deterministic spec → hardware commands)
"""

from nl2protocol.extraction.schema_builder import (
    spec_to_schema,
    validate_schema_against_spec,
    _format_step_line,
)
from nl2protocol.extraction.prompts import (
    REASONING_SYSTEM_PROMPT,
    REASONING_USER_PROMPT,
)
from nl2protocol.extraction.resolver import LabwareResolver
from nl2protocol.extraction.extractor import (
    SemanticExtractor,
    _find_provenance_reason,
)
# Re-export spec models so `from nl2protocol.extraction import ProtocolSpec` works
from nl2protocol.models.spec import (
    ProtocolSpec,
    CompleteProtocolSpec,
    ExtractedStep,
    WellContents,
    LabwarePrefill,
    Provenance,
    CompositionProvenance,
    ProvenancedVolume,
    ProvenancedDuration,
    ProvenancedTemperature,
    ProvenancedString,
    LocationRef,
    PostAction,
)
