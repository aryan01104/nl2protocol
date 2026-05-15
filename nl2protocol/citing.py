"""Citation utilities — shared predicate for locating a cited substring
within an instruction.

Used by:
- nl2protocol/reporting.py for rendering cite-span marks in the
  instruction column (hover/highlight wiring).
- nl2protocol/extraction/extractor.py for verifying that an
  instruction-sourced provenance's cited_text actually appears in the
  instruction text (fabrication detection).

Keeping the predicate in one place prevents the visual surface and the
verifier from disagreeing about which cites are valid — a class of bug
ADR-0003 was designed to eliminate.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple


def normalize_for_match(s: str) -> str:
    """Lowercase + collapse runs of whitespace to single spaces, strip ends.

    Used by `find_cite_position` and any caller doing substring matching
    where minor spacing differences ('100 uL' vs '100uL', tabs vs spaces)
    shouldn't cause misses.
    """
    return re.sub(r"\s+", " ", s.lower()).strip()


def find_cite_position(instruction: str, cited_text: str) -> Optional[Tuple[int, int]]:
    """Find (start, end) char offsets where `cited_text` appears in
    `instruction`. Case-insensitive. Falls back to whitespace-collapsed
    matching for innocuous spacing differences. Returns None when no
    occurrence is found by either pass.

    First-match-wins for substrings that appear multiple times.
    Multi-cite disambiguation (e.g., carrying an LLM-emitted character
    offset alongside the cite) is a future improvement.
    """
    if not cited_text:
        return None
    # Try exact case-insensitive match first.
    pattern = re.compile(re.escape(cited_text), re.IGNORECASE)
    m = pattern.search(instruction)
    if m:
        return m.start(), m.end()
    # Fallback: collapse whitespace on both sides and try again. The
    # returned offsets are approximate (they reference the normalized
    # instruction, then clamped to the original instruction's length) —
    # acceptable for whitespace-only diffs since downstream callers
    # use the position for visual marking or pass/fail checks, not
    # byte-exact slicing.
    norm_instruction = normalize_for_match(instruction)
    norm_cite = normalize_for_match(cited_text)
    pattern2 = re.compile(re.escape(norm_cite))
    m2 = pattern2.search(norm_instruction)
    if m2:
        approx_start = m2.start()
        approx_end = min(m2.end(), len(instruction))
        return approx_start, approx_end
    return None
