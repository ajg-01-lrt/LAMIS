"""One-shot investigator: where does the 103-count for 3HE04824AA come from,
and is it ordered under any aliased form in the Sales BoM?"""
from __future__ import annotations

import re
from pathlib import Path
from openpyxl import load_workbook

TESTING = Path(r"c:/Users/ZackerySimino/OneDrive - LightRiver Technologies Inc/Desktop/Inventory and Packing slips/Testing")
LIVE_PATH    = TESTING / "Master_BPA_TO4_Factoy_Inventory.BOM.xlsx"
SALES_PATH   = TESTING / "TO4_BPA_NGT_Refresh_SalesBoM_Site_Packing_2026-05-22_22-05.xlsx"
COMPARE_PATH = TESTING / "Master_BPA_TO4_Factoy_Inventory.BOM.COMPARE.xlsx"

TARGET = "3HE04824AA"

def norm(s):
    return re.sub(r"\s+", "", str(s or "")).upper()

def scan_workbook(path: Path, title: str, target_norm: str):
    print(f"\n=== {title} :: {path.name} ===")
    if not path.exists():
        print("  (missing)")
        return
    wb = load_workbook(path, data_only=True, read_only=False)
    for ws in wb.worksheets:
        # find PN-bearing cells whose normalized PN contains the target
        hits = []
        max_r = ws.max_row or 0
        max_c = ws.max_column or 0
        if max_r > 5000 or max_c > 200:
            max_r = min(max_r, 5000)
            max_c = min(max_c, 200)
        for row in ws.iter_rows(min_row=1, max_row=max_r, max_col=max_c, values_only=False):
            for cell in row:
                if cell.value is None:
                    continue
                nv = norm(cell.value)
                if target_norm in nv:
                    hits.append((cell.row, cell.column, cell.coordinate, str(cell.value)))
        if hits:
            print(f"  [Sheet: {ws.title}]  {len(hits)} cells contain '{target_norm}'")
            for r, c, coord, val in hits[:30]:
                # print this row's data: PN cell + first ~12 neighbors after
                row_cells = list(ws.iter_rows(min_row=r, max_row=r, max_col=max_c, values_only=True))[0]
                nonblank = [(i+1, v) for i, v in enumerate(row_cells) if v not in (None, "")]
                print(f"    {coord!r:>8}  raw={val!r}")
                print(f"       row {r} nonblank cells (col,val): {nonblank[:25]}")

def find_headers(ws):
    """Find header row (looks like row 7 in these workbooks) and column map."""
    for r in range(1, min(15, (ws.max_row or 0) + 1)):
        vals = [str(ws.cell(row=r, column=c).value or "") for c in range(1, min(80, (ws.max_column or 0) + 1))]
        joined = " | ".join(vals).lower()
        if "part" in joined and ("description" in joined or "desc" in joined):
            return r, vals
    return None, []

def main():
    tnorm = norm(TARGET)
    for p, name in [(LIVE_PATH, "LIVE"), (SALES_PATH, "SALES"), (COMPARE_PATH, "COMPARE")]:
        scan_workbook(p, name, tnorm)

    # Targeted look at LIVE BoM sheet
    print("\n=== LIVE-side detail: per-column count for 3HE04824AA ===")
    wb = load_workbook(LIVE_PATH, data_only=True)
    # main aggregate sheet typically titled "BoM" or "Master" etc
    candidates = [ws for ws in wb.worksheets if any(k in ws.title.lower() for k in ("bom","master","aggregate","factory"))]
    if not candidates:
        candidates = wb.worksheets
    for ws in candidates:
        hdr_row, hdrs = find_headers(ws)
        if not hdr_row:
            continue
        # find PN col
        pn_col = None
        for i, h in enumerate(hdrs):
            if re.search(r"\bpart\b", str(h), re.I) and "number" in str(h).lower():
                pn_col = i + 1
                break
        if pn_col is None:
            for i, h in enumerate(hdrs):
                if "part" in str(h).lower() and "num" in str(h).lower():
                    pn_col = i + 1
                    break
        if pn_col is None:
            continue
        print(f"\n  Sheet {ws.title!r}  header row={hdr_row}  PN col={pn_col}")
        print(f"  Headers: {hdrs[:40]}")
        # find the row for target
        for r in range(hdr_row + 1, (ws.max_row or 0) + 1):
            v = norm(ws.cell(row=r, column=pn_col).value)
            if v == tnorm:
                row_vals = [(c, hdrs[c-1] if c-1 < len(hdrs) else f"col{c}", ws.cell(row=r, column=c).value)
                            for c in range(1, min(len(hdrs)+1, (ws.max_column or 0)+1))]
                nonblank = [(c, h, v) for (c, h, v) in row_vals if v not in (None, "")]
                print(f"  Row {r} non-blank cells:")
                for c, h, v in nonblank:
                    print(f"    col {c:>3} ({h!r}): {v!r}")
                break

if __name__ == "__main__":
    main()
