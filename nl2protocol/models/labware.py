"""
labware.py — Labware domain knowledge.

Maps Opentrons load_names to well layouts. Used by constraint checking,
well state tracking, and config enrichment.
"""

from typing import Dict, Any


def get_well_info(load_name: str) -> Dict[str, Any]:
    """Resolve a labware load_name to its well-layout dict (rows, cols, wells).

    Pre:    `load_name` is a string. The legible domain has two parts:
              - Opentrons-known load_names (e.g. "corning_96_wellplate_360ul_flat")
                — resolved authoritatively via Opentrons' own labware library.
              - Heuristic-recognized strings: any load_name whose lowercase
                form contains exactly one of these substring patterns
                (checked in priority order):
                    "_384_"                       → 16 rows × 24 cols
                    "_96_"                        →  8 rows × 12 cols
                    "_48_"                        →  6 rows ×  8 cols
                    "_24_"                        →  4 rows ×  6 cols
                    "_12_reservoir" / "_12_well_reservoir"
                                                  →  1 row  × 12 cols
                    "_12_"                        →  3 rows ×  4 cols
                    "_6_"                         →  2 rows ×  3 cols
                    "_1_reservoir" / "_1_well"    →  1 row  ×  1 col

    Post:   Returns a dict with exactly six keys:
              - rows (int): number of rows
              - cols (int): number of columns
              - row_range (str): "A-X" if rows > 1, else "A"
              - col_range (str): "1-N" (always range form, even when cols=1)
              - well_count (int): equals rows * cols (also equals
                len(valid_wells) for the heuristic path; for the Opentrons
                path equals len(defn["wells"]))
              - valid_wells (List[str]): well-name strings (e.g. "A1"), sorted
                column-major then row (Opentrons path) or row-major then col
                (heuristic path) — sort order differs between paths.
            For Opentrons-known load_names: dimensions match Opentrons'
            definition exactly.
            For heuristic-recognized load_names: dimensions match the
            substring dispatch table above.

    KNOWN WARTS (current behavior, pin-tested below):
      1. Unrecognized load_names that don't match any pattern silently fall
         through to the default 8 rows × 12 cols (96-well plate). A typo
         in a load_name is indistinguishable from a real 96-well plate.
      2. Empty string `""` falls through to the same default — a labware
         entry with a missing/empty `load_name` silently appears as a
         96-well plate to downstream callers (e.g. `_check_well_validity`).
      Both warts mean this function never returns a "could not resolve"
      signal. Callers cannot distinguish "valid 96-well plate" from
      "garbage in, default out". A future fix should raise on
      unrecognized input or return None.

    Side effects: Imports `opentrons.protocols.labware` lazily on the
                  Opentrons path. Logs a WARNING via the module-level
                  logger when falling back to the heuristic path.

    Raises: AttributeError if `load_name` is not a string (from `.lower()`).
            Does NOT raise on unrecognized input — see WARTS above.
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
