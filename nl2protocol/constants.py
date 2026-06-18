"""
constants.py — project-wide constants.

Kept dependency-free (a leaf module) so any module can import it without
risking an import cycle.
"""

# Default Claude model for all LLM calls (input classification, semantic
# extraction, labware resolution). Centralized here so a model bump — or a
# model retirement, which previously left the same dead id hardcoded in four
# places — is a one-line change. Override per call by passing model_name=...
# to ConfigLoader / SemanticExtractor / InputValidator / LabwareMatcher.
DEFAULT_MODEL = "claude-sonnet-4-6"


# Reserved internal label marking a destination as the OT-2 fixed trash (always
# present in slot 12, never declared in lab_config.json). A discard step lowers
# to "aspirate the waste into the tip, then drop the tip in the trash", so this
# label never becomes a loaded labware. Used by the resolver (to route discard
# descriptions here), schema_builder (to lower them), and constraints (to treat
# them as resolved).
TRASH_LABEL = "trash"

# User-language tokens that name discarded liquid rather than a real container.
# A destination description matching one of these resolves to TRASH_LABEL.
DISCARD_DESCRIPTIONS = frozenset({
    "waste", "trash", "discard", "liquid waste",
    "waste container", "waste reservoir", "to waste", "to trash",
})


def is_discard_description(text: str) -> bool:
    """Return True when a labware description names the trash sink, not a container.

    Pre:    `text` is a labware description string, or None.
    Post:   Returns True iff `text` (case-insensitive, stripped) contains any
            token in `DISCARD_DESCRIPTIONS`. None or empty → False. Pure; no
            side effects.
    """
    if not text:
        return False
    t = text.strip().lower()
    return any(token in t for token in DISCARD_DESCRIPTIONS)
