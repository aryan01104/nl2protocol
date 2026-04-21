"""
colors.py

Zero-dependency ANSI color helpers for terminal output.
Uses subtle, muted colors — no bright/garish tones.

Respects NO_COLOR (https://no-color.org/) and non-TTY environments.
"""

import os
import sys


def _colors_enabled(stream=None) -> bool:
    """Check if color output should be used."""
    if os.environ.get("NO_COLOR"):
        return False
    s = stream or sys.stderr
    return hasattr(s, 'isatty') and s.isatty()


# ANSI escape codes — using standard (not bright) variants for subtlety
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_CYAN = "\033[36m"
_WHITE = "\033[37m"


def _wrap_color(text: str, code: str) -> str:
    if not _colors_enabled():
        return text
    return f"{code}{text}{_RESET}"


# --- Structural hierarchy ---

def header(text: str) -> str:
    """Stage headers like [Stage 1/8]. Bold, subtle blue."""
    return _wrap_color(text, _BOLD + _BLUE)

def label(text: str) -> str:
    """Left-side labels (Protocol type:, Steps:). Just bold, no color."""
    return _wrap_color(text, _BOLD)

def dim(text: str) -> str:
    """De-emphasized text (debug paths, file names)."""
    return _wrap_color(text, _DIM)

# --- Semantic meaning ---

def success(text: str) -> str:
    """Success messages. Muted green."""
    return _wrap_color(text, _GREEN)

def error(text: str) -> str:
    """Error messages. Red but not bold — firm, not screaming."""
    return _wrap_color(text, _RED)

def warning(text: str) -> str:
    """Warnings and inferred values. Muted yellow."""
    return _wrap_color(text, _YELLOW)

def info(text: str) -> str:
    """Informational — reasoning, suggestions. Dim white."""
    return _wrap_color(text, _DIM + _WHITE)
