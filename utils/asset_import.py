"""Asset Import — match an ASN-style asset workbook onto an inventory file.

Operator workflow:

1. Upload a live-inventory workbook (the ATLAS-generated kind with one
   sheet per device, chassis row at 15, serial in column E, asset-tag
   column at G).
2. Upload an "asset doc" (Advanced Ship Notification format — see
   ``parse_asset_doc`` for the header-detection rules).
3. For every device-tab row whose Serial Number (column E) matches a
   row in the asset doc, write the corresponding Asset value to G
   (overwriting whatever was there, including the live XLOOKUP-from-
   Summary formula — automated bulk fill wins over manual entry).
4. Per device tab: if the matching rows all share one PO, stamp that
   PO into the tab's ``Customer PO`` cell (C7). If the matches yield
   conflicting POs on the same tab, leave C7 alone and flag in the
   result.

Pure logic, no Tk. ``apply_asset_data_to_inventory`` is the merge
entry point; the GUI just opens the inventory workbook, calls this,
then saves.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Column-header recognition ──────────────────────────────────────────

# Each entry is a list of *normalized* header forms that count as a
# match for the column. Normalization (see ``_norm``) collapses
# whitespace and punctuation so ``"Serial #"``, ``"serial #"``, ``"Serial Number"``,
# and ``"serial-number"`` all converge on the same key.
_PO_HEADERS = {"po", "ponumber", "purchaseorder"}
_SERIAL_HEADERS = {"serial", "serialnumber"}
_ASSET_HEADERS = {"asset", "assetnumber", "assettag"}


def _norm(value: Any) -> str:
    """Normalize a cell value for header matching.

    Lowercase, strip whitespace + punctuation. ``"Serial #"`` → ``"serial"``;
    ``"PO Number"`` → ``"ponumber"``; numbers and None → ``""``.
    """
    if value is None:
        return ""
    s = str(value).strip().lower()
    # Drop everything except alphanumerics.
    return re.sub(r"[^a-z0-9]+", "", s)


def _norm_serial(value: Any) -> str:
    """Normalize a serial number for matching across files.

    Strip whitespace, uppercase. Empty / None / ``"nan"`` → ``""``."""
    if value is None:
        return ""
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return ""
    return s.upper()


# ── Parser ─────────────────────────────────────────────────────────────


class AssetRecord(NamedTuple):
    """One row from the asset doc that mentions a serial."""
    po: str
    asset: str


def parse_asset_doc(path: str) -> Dict[str, AssetRecord]:
    """Read an ASN-style workbook into ``{serial: AssetRecord(po, asset)}``.

    Header detection walks every cell on every sheet looking for a
    row that contains all three of: a PO header, a Serial header, an
    Asset header. The first such row anchors the column positions;
    data starts on the row below.

    Duplicate serials with identical (PO, asset) are silently
    collapsed. Duplicates with conflicting payloads keep the LAST
    occurrence (and log a debug line); use ``apply_asset_data_to_inventory``'s
    return value if you need to surface those for the operator.

    Raises ``ValueError`` if no header row could be located.
    """
    # Imported here so tests that don't have openpyxl on the path don't
    # blow up just for importing this module.
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)

    for ws in wb.worksheets:
        anchor = _find_header_row(ws)
        if anchor is None:
            continue
        po_col, serial_col, asset_col, header_row = anchor
        result: Dict[str, AssetRecord] = {}
        for row in ws.iter_rows(
            min_row=header_row + 1, values_only=True,
        ):
            # ``values_only=True`` gives tuples indexed from 0; the
            # column-positions returned by ``_find_header_row`` are 1-
            # based (openpyxl convention) so we subtract 1 here.
            po_val = _cell_at(row, po_col - 1)
            serial_val = _cell_at(row, serial_col - 1)
            asset_val = _cell_at(row, asset_col - 1)
            serial = _norm_serial(serial_val)
            if not serial:
                continue
            po = "" if po_val is None else str(po_val).strip()
            asset = "" if asset_val is None else str(asset_val).strip()
            if not po and not asset:
                # Useless row — no PO, no asset.
                continue
            existing = result.get(serial)
            if existing is not None and existing != AssetRecord(po, asset):
                logger.debug(
                    "asset doc: serial %r duplicated with conflicting "
                    "(po=%r,asset=%r) vs prior %r — last wins",
                    serial, po, asset, existing,
                )
            result[serial] = AssetRecord(po=po, asset=asset)
        wb.close()
        return result
    wb.close()
    raise ValueError(
        "Could not locate a header row containing PO / Serial # / Asset "
        "in any sheet of the asset doc."
    )


def _cell_at(row_tuple: tuple, idx: int) -> Any:
    """Tolerant tuple indexer — short rows return None instead of raising."""
    if idx < 0 or idx >= len(row_tuple):
        return None
    return row_tuple[idx]


def _find_header_row(ws: Any) -> Optional[Tuple[int, int, int, int]]:
    """Locate the header row + column positions.

    Returns ``(po_col, serial_col, asset_col, header_row)`` (all 1-
    based) or ``None`` if no row contains all three headers. The same
    row is required to carry every header — splitting them across
    multiple rows isn't supported and isn't what real ASN docs do.
    """
    max_scan = min(ws.max_row or 0, 200)  # ASN headers always appear within ~30 rows
    for r in range(1, max_scan + 1):
        po_col = serial_col = asset_col = None
        for cell in ws[r]:
            key = _norm(cell.value)
            if not key:
                continue
            if po_col is None and key in _PO_HEADERS:
                po_col = cell.column
            elif serial_col is None and key in _SERIAL_HEADERS:
                serial_col = cell.column
            elif asset_col is None and key in _ASSET_HEADERS:
                asset_col = cell.column
        if po_col and serial_col and asset_col:
            return po_col, serial_col, asset_col, r
    return None


# ── Merger ─────────────────────────────────────────────────────────────


# Column letters/numbers that match the ATLAS device-tab layout.
_INVENTORY_SERIAL_COL = 5   # E
_INVENTORY_ASSET_COL = 7    # G
_INVENTORY_CUSTOMER_PO_CELL = "C7"
_INVENTORY_DATA_START_ROW = 15

# Sheets that look like device tabs but aren't — exclude from the merge.
_NON_DEVICE_SHEET_NAMES = {
    "Summary", "BOM", "Inventory by Site",
    "Customer-project",
}


class AssetImportResult(NamedTuple):
    """Stats from one merge run, surfaced to the operator."""
    tabs_touched: int
    rows_matched: int
    tabs_with_no_matches: List[str]
    serials_not_in_asset_doc: int
    po_conflicts: List[Tuple[str, List[str]]]


def apply_asset_data_to_inventory(
    wb: Any,
    asset_map: Dict[str, AssetRecord],
    *,
    chassis_row: int = _INVENTORY_DATA_START_ROW,
) -> AssetImportResult:
    """Merge ``asset_map`` onto every device tab in *wb*.

    Mutates *wb* in place — caller saves. Returns an ``AssetImportResult``
    with merge stats.

    Per device tab:

    * Walks rows from ``chassis_row`` to ``ws.max_row``.
    * For each row, reads column E (serial), looks up in ``asset_map``.
      On a match: writes the asset to column G, accumulates the PO.
    * After the row walk: if all matched POs were identical, stamps
      that PO into C7. Conflicting POs are flagged in
      ``po_conflicts`` and C7 is left untouched.
    """
    tabs_touched = 0
    rows_matched = 0
    tabs_with_no_matches: List[str] = []
    po_conflicts: List[Tuple[str, List[str]]] = []
    serials_seen_not_in_map = 0

    for sname in wb.sheetnames:
        if sname in _NON_DEVICE_SHEET_NAMES:
            continue
        ws = wb[sname]
        tab_pos = set()  # all distinct (non-empty) POs we matched on this tab
        tab_matched_count = 0
        last_row = ws.max_row or chassis_row
        for r in range(chassis_row, last_row + 1):
            serial_cell = ws.cell(row=r, column=_INVENTORY_SERIAL_COL).value
            serial = _norm_serial(serial_cell)
            if not serial:
                continue
            record = asset_map.get(serial)
            if record is None:
                serials_seen_not_in_map += 1
                continue
            if record.asset:
                # Overwrite anything in G — including the
                # XLOOKUP-from-Summary formula that fresh inventory
                # builds plant at G15. Bulk import is the
                # authoritative source when the operator chose to
                # run it.
                ws.cell(
                    row=r, column=_INVENTORY_ASSET_COL,
                ).value = record.asset
                tab_matched_count += 1
            if record.po:
                tab_pos.add(record.po)
        if tab_matched_count == 0 and not tab_pos:
            tabs_with_no_matches.append(sname)
            continue
        tabs_touched += 1
        rows_matched += tab_matched_count
        if len(tab_pos) == 1:
            ws[_INVENTORY_CUSTOMER_PO_CELL] = next(iter(tab_pos))
        elif len(tab_pos) > 1:
            po_conflicts.append((sname, sorted(tab_pos)))
    return AssetImportResult(
        tabs_touched=tabs_touched,
        rows_matched=rows_matched,
        tabs_with_no_matches=tabs_with_no_matches,
        serials_not_in_asset_doc=serials_seen_not_in_map,
        po_conflicts=po_conflicts,
    )
