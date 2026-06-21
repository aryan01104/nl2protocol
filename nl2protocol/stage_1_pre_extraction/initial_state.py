"""Parse an uploaded initial-state spreadsheet into a per-well contents map.

The spreadsheet is the ground-truth initial state of the deck: for each labware,
two plate-map blocks — a *substance* map and a *volume* map — laid out as a grid
(row letters down the left, column numbers across the top). A block is anchored by
a header cell reading ``<config label> — substance`` or ``<config label> — volume``.
See docs/plans/initial-state-spreadsheet.html for the writing rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import openpyxl

Well = str
Label = str
Cell = Tuple[str, float]  # (substance, volume_ul)

_HEADER_RE = re.compile(r"[—–\-:]\s*(substance|volume)\b.*$", re.IGNORECASE)


@dataclass(frozen=True)
class InitialStateSheet:
    """Parsed initial-state map keyed on (config label, well).

    `cells` holds one entry per filled well: its substance and volume in µL.
    Empty wells are omitted. `errors` collects every parse problem found; the
    parser never raises on a bad cell, so a partial map plus its errors both
    surface to the caller.
    """

    cells: Dict[Tuple[Label, Well], Cell]
    errors: List[str]


def _parse_header(text: str) -> Optional[Tuple[str, str]]:
    """Return (label, attribute) if `text` is a block header, else None.

    Attribute is "substance" or "volume". The label is whatever precedes the
    dash/colon + keyword, verbatim (validated against the config by the caller).
    """
    m = _HEADER_RE.search(text.strip())
    if not m:
        return None
    label = text.strip()[: m.start()].strip()
    if not label:
        return None
    return label, m.group(1).lower()


def _read_grid(matrix, r0: int, c0: int) -> Dict[Well, object]:
    """Read one plate-map grid anchored at header (r0, c0).

    Row r0+1 holds column numbers starting at c0+1; column c0 from r0+2 down holds
    row letters. Returns {well: raw cell value} for non-blank body cells, stopping
    at the first blank row-letter cell (end of block).
    """
    col_numbers: List[Tuple[int, object]] = []  # (matrix column index, label)
    header_row = matrix[r0 + 1] if r0 + 1 < len(matrix) else []
    c = c0 + 1
    while c < len(header_row) and header_row[c] not in (None, ""):
        col_numbers.append((c, header_row[c]))
        c += 1

    out: Dict[Well, object] = {}
    rr = r0 + 2
    while rr < len(matrix):
        row = matrix[rr]
        letter = row[c0] if c0 < len(row) else None
        if letter in (None, ""):
            break
        letter = str(letter).strip().upper()
        for ci, colnum in col_numbers:
            val = row[ci] if ci < len(row) else None
            if val in (None, ""):
                continue
            well = f"{letter}{str(colnum).strip()}"
            out[well] = val
        rr += 1
    return out


def parse_initial_state(xlsx_bytes: bytes, config: dict) -> InitialStateSheet:
    """Parse an .xlsx initial-state map into an InitialStateSheet.

    Pre:  `xlsx_bytes` is the raw bytes of an .xlsx workbook. `config` is the lab
          config; `config["labware"]` maps config labels to entries.
    Post: Returns an InitialStateSheet whose `cells` has one (label, well) entry
          per well that is filled in BOTH the substance and volume maps, with the
          volume coerced to float. `errors` lists, without raising:
            - a header whose label is not a key in `config["labware"]`;
            - a duplicate block for the same (label, attribute);
            - a non-numeric volume cell;
            - a well present in one of a label's two maps but not the other.
          A workbook that cannot be opened yields empty cells and one error.
    """
    labware = config.get("labware", {}) or {}
    errors: List[str] = []

    try:
        wb = openpyxl.load_workbook(BytesIO(xlsx_bytes), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 — surface any open failure as a parse error
        return InitialStateSheet(cells={}, errors=[f"could not read spreadsheet: {e}"])

    ws = wb.active
    matrix = [list(row) for row in ws.iter_rows(values_only=True)]

    # label -> attribute -> {well: raw value}
    blocks: Dict[Label, Dict[str, Dict[Well, object]]] = {}
    for r, row in enumerate(matrix):
        for c, val in enumerate(row):
            if not isinstance(val, str):
                continue
            parsed = _parse_header(val)
            if parsed is None:
                continue
            label, attr = parsed
            if label not in labware:
                errors.append(f"header '{val}': '{label}' is not a labware in the config")
                continue
            if label in blocks and attr in blocks[label]:
                errors.append(f"duplicate {attr} map for '{label}'")
                continue
            blocks.setdefault(label, {})[attr] = _read_grid(matrix, r, c)

    cells: Dict[Tuple[Label, Well], Cell] = {}
    for label, attrs in blocks.items():
        substances = attrs.get("substance", {})
        volumes = attrs.get("volume", {})
        for well in set(substances) | set(volumes):
            if well not in substances:
                errors.append(f"{label} {well}: volume given but no substance")
                continue
            if well not in volumes:
                errors.append(f"{label} {well}: substance given but no volume")
                continue
            try:
                vol = float(volumes[well])
            except (TypeError, ValueError):
                errors.append(f"{label} {well}: volume '{volumes[well]}' is not a number")
                continue
            cells[(label, well)] = (str(substances[well]).strip(), vol)

    wb.close()
    return InitialStateSheet(cells=cells, errors=errors)
