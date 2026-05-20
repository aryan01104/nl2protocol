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


# Well-shaped value: row letter A-P, column digits. Used by `cite_covers_well`
# to detect when a value is a candidate well name before consulting range
# semantics on the cite.
_WELL_RE = re.compile(r"^([A-P])(\d+)$", re.IGNORECASE)

# Alphanumeric range like "B1-B4" or "A1-D4". Plain hyphen, en-dash, em-dash
# all accepted as the delimiter (LLM output occasionally uses dashes).
_ALPHANUMERIC_RANGE_RE = re.compile(
    r"([A-P])(\d+)\s*[-\u2013\u2014]\s*([A-P])(\d+)",
    re.IGNORECASE,
)

# "column N" / "columns N-M" — singular and plural; bare and ranged.
_COLUMN_EXPR_RE = re.compile(
    r"\bcolumns?\s+(\d+)(?:\s*[-\u2013\u2014]\s*(\d+))?\b",
    re.IGNORECASE,
)

# "row X" / "rows X-Y" — singular and plural; bare and ranged.
_ROW_EXPR_RE = re.compile(
    r"\brows?\s+([A-P])(?:\s*[-\u2013\u2014]\s*([A-P]))?\b",
    re.IGNORECASE,
)


def cite_covers_well(cite: str, well: str) -> bool:
    """Return True when `cite` contains a range expression covering `well`.

    Pre:    `well` is a candidate well name (e.g. 'A1', 'B12').
            `cite` is the cited_text string from a Provenance, raw.

    Post:   Returns True when at least one of the following holds:
              - `cite` contains an alphanumeric range like 'B1-B4'
                (single row), 'A1-D1' (single column), or 'A1-D4'
                (rectangle) and `well`'s (row, col) lies in the
                rectangle. Endpoint order is irrelevant ('D4-A1' ==
                'A1-D4'). Hyphen, en-dash, em-dash all accepted.
              - `cite` contains a column expression — 'column N' or
                'columns N-M' — and `well`'s column matches (or is in
                the inclusive range).
              - `cite` contains a row expression — 'row X' or
                'rows X-Y' — and `well`'s row matches (or is in the
                inclusive range).
            Returns False when `well` doesn't match [A-P]\\d+, when
            `cite` contains no range expression, or when every range
            expression in `cite` excludes `well`.
            Plate-dimension-agnostic: only the well's own row+column
            are consulted; no assumption about plate size.

    Side effects: None.

    Note:   A compound cite like 'row A column 5' meant to designate
            a single cell is interpreted disjunctively (any well in
            row A or any well in column 5 returns True). The LLM
            normally cites single cells as literals ('A5'), so this
            edge case is rare; the relaxation cost is small.
    """
    if not cite:
        return False
    m = _WELL_RE.match(well or "")
    if not m:
        return False
    well_row = m.group(1).upper()
    well_col = int(m.group(2))

    for rm in _ALPHANUMERIC_RANGE_RE.finditer(cite):
        r1, c1 = rm.group(1).upper(), int(rm.group(2))
        r2, c2 = rm.group(3).upper(), int(rm.group(4))
        row_lo, row_hi = sorted([r1, r2])
        col_lo, col_hi = sorted([c1, c2])
        if row_lo <= well_row <= row_hi and col_lo <= well_col <= col_hi:
            return True

    for cm in _COLUMN_EXPR_RE.finditer(cite):
        c1 = int(cm.group(1))
        c2 = int(cm.group(2)) if cm.group(2) else c1
        col_lo, col_hi = sorted([c1, c2])
        if col_lo <= well_col <= col_hi:
            return True

    for rm in _ROW_EXPR_RE.finditer(cite):
        r1 = rm.group(1).upper()
        r2 = rm.group(2).upper() if rm.group(2) else r1
        row_lo, row_hi = sorted([r1, r2])
        if row_lo <= well_row <= row_hi:
            return True

    return False
