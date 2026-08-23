from openpyxl import load_workbook
from pathlib import Path
SRC = Path(r"C:/Users/ZackerySimino/OneDrive - LightRiver Technologies Inc/Desktop/ATLAS Build Material/TO4-Services-Prices-with-BoM-V9.1-03-05-26_BPA.xlsx")
wb = load_workbook(SRC, data_only=True)
print(f"Sheets: {wb.sheetnames[:15]}")
# Find sheet with 3HE11286AA
target = "3HE11286AA"
for ws in wb.worksheets:
    max_r = min(ws.max_row or 0, 3000)
    max_c = min(ws.max_column or 0, 200)
    for r in range(1, max_r+1):
        v = ws.cell(r, 2).value
        if v and target in str(v).upper().replace(" ", ""):
            # Show the row's first 6 cols + spares-ish cell
            print(f"\n  [{ws.title}] r{r}: col B={v!r}")
            for c in range(1, min(max_c, 10)+1):
                cv = ws.cell(r, c).value
                if cv not in (None, ""):
                    print(f"     col {c} = {cv!r}")
            break
