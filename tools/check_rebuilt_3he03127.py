from openpyxl import load_workbook
from pathlib import Path
import tempfile
P = Path(tempfile.gettempdir()) / "rebuilt_sales_bom.xlsx"
wb = load_workbook(P, data_only=True)
print(f"Sheets: {len(wb.sheetnames)}  ({wb.sheetnames[:5]} ... {wb.sheetnames[-5:]})")
# Look for 3HE03127AA across every sheet
import re
TARGET = "3HE03127AA"
for ws in wb.worksheets:
    max_r = ws.max_row or 0
    max_c = ws.max_column or 0
    if max_c > 100:  # skip BOM-tab-style ultrawide for clarity
        for r in range(1, min(max_r, 100)+1):
            v = ws.cell(r, 2).value
            if v and TARGET in str(v):
                # show desc + first 6 non-zero data cells
                cells = [(c, ws.cell(r, c).value) for c in range(1, max_c+1)]
                hits = [(c, v) for c, v in cells if v not in (None, "", 0)]
                print(f"  [{ws.title}] r{r}: {hits[:18]}")
        continue
    for r in range(1, max_r+1):
        for c in (2, 4):
            v = ws.cell(r, c).value
            if v and TARGET in str(v):
                row_data = [(cc, ws.cell(r, cc).value) for cc in range(1, max_c+1)]
                row_data = [(cc, v) for cc, v in row_data if v not in (None, "", 0)]
                print(f"  [{ws.title}] r{r}: {row_data}")
                break
