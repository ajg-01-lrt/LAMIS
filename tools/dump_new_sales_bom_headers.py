"""Dump first 12 rows of every sheet in the failing Sales BoM to see what
header keywords are present (or absent) on the BOM aggregate tab."""
from __future__ import annotations
from pathlib import Path
from openpyxl import load_workbook

PATH = Path(r"C:/Users/ZackerySimino/OneDrive - LightRiver Technologies Inc/Desktop/Inventory and Packing slips/Testing/Sales_BPA_NGT_Refresh_TO4_SalesBoM_Site_Packing_2026-05-23_00-33.xlsx")

wb = load_workbook(PATH, data_only=True, read_only=False)
print(f"Sheets: {wb.sheetnames[:10]} (showing first 10 of {len(wb.sheetnames)})")
for ws in wb.worksheets[:3]:
    print(f"\n=== {ws.title} ===  max_row={ws.max_row} max_col={ws.max_column}")
    for r in range(1, min(13, (ws.max_row or 0)+1)):
        cells = []
        for c in range(1, min(ws.max_column or 0, 30) + 1):
            v = ws.cell(r,c).value
            if v is not None:
                cells.append(f"[{c}]{v!r}")
        if cells:
            print(f"  r{r:>2}:  " + "  ".join(cells))
