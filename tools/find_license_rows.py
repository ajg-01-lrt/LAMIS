"""Locate license rows in a Factory per-site detail tab so we know how
to filter them."""
from openpyxl import load_workbook
from pathlib import Path
P = Path(r"C:/Users/ZackerySimino/OneDrive - LightRiver Technologies Inc/Desktop/Inventory and Packing slips/Testing/Master_BPA_TO4_Factoy_Inventory.BOM.xlsx")
wb = load_workbook(P, data_only=True)
ws = wb["MALN"]
print(f"MALN: {ws.max_row}x{ws.max_column}")
# Print rows where col D contains a license PN OR look for section banners
LICENSE_PNS = ("3HE15662EA", "3HE16839AA", "3HE02784UA", "3HE07354AC", "3HE08607EA", "3HE09259EA")
for r in range(1, ws.max_row+1):
    vals = []
    for c in range(1, min(ws.max_column or 0, 8)+1):
        v = ws.cell(r, c).value
        vals.append("" if v is None else str(v))
    joined = " ".join(vals).lower()
    is_license_row = any(pn in " ".join(vals) for pn in LICENSE_PNS)
    is_banner = any(w in joined for w in ("license", "software", "maintenance", "rtu ", "os ", "service"))
    if is_license_row or is_banner or "license" in joined or "node" in joined:
        cells = [f"[{c}]{v!r}" for c, v in enumerate(vals, start=1) if v]
        if cells:
            print(f"  r{r:>3}: " + "  ".join(cells)[:240])
