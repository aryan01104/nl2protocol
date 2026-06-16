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
