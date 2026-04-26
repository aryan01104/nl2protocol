"""
labware.py — Labware domain knowledge.

Maps Opentrons load_names to well layouts. Used by constraint checking,
well state tracking, and config enrichment.
"""

from typing import Dict, Any


def get_well_info(load_name: str) -> Dict[str, Any]:
    """
    Get well layout for a labware load_name.

    Primary: uses Opentrons' own labware definitions (authoritative source).
    Fallback: heuristic pattern matching on the load_name string.

    Returns: dict with rows, cols, row_range, col_range, well_count, valid_wells.
    """
    # Try the Opentrons labware library first — this is the real data
    try:
        from opentrons.protocols.labware import get_labware_definition
        defn = get_labware_definition(load_name)
        wells = sorted(defn["wells"].keys(), key=lambda w: (int(w[1:]), w[0]))

        # Derive grid dimensions from actual wells
        row_set = sorted(set(w[0] for w in wells))
        col_set = sorted(set(int(w[1:]) for w in wells))
        rows = len(row_set)
        cols = len(col_set)

        return {
            "rows": rows,
            "cols": cols,
            "row_range": f"{row_set[0]}-{row_set[-1]}" if rows > 1 else row_set[0],
            "col_range": f"{col_set[0]}-{col_set[-1]}",
            "well_count": len(wells),
            "valid_wells": wells
        }
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            f"Could not load Opentrons definition for '{load_name}' — using heuristic well layout"
        )

    # Fallback: heuristic pattern matching for unknown/custom labware
    ln = load_name.lower()
    if "_384_" in ln:
        rows, cols = 16, 24
    elif "_96_" in ln:
        rows, cols = 8, 12
    elif "_48_" in ln:
        rows, cols = 6, 8
    elif "_24_" in ln:
        rows, cols = 4, 6
    elif "_12_reservoir" in ln or "_12_well_reservoir" in ln:
        rows, cols = 1, 12
    elif "_12_" in ln:
        rows, cols = 3, 4
    elif "_6_" in ln:
        rows, cols = 2, 3
    elif "_1_reservoir" in ln or "_1_well" in ln:
        rows, cols = 1, 1
    else:
        rows, cols = 8, 12

    row_letters = [chr(ord('A') + i) for i in range(rows)]
    well_names = [f"{r}{c}" for r in row_letters for c in range(1, cols + 1)]

    return {
        "rows": rows,
        "cols": cols,
        "row_range": f"A-{row_letters[-1]}" if rows > 1 else "A",
        "col_range": f"1-{cols}",
        "well_count": len(well_names),
        "valid_wells": well_names
    }
