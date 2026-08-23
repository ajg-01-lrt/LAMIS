"""Verify _parse_per_site_bom now captures description for spares-only PNs."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import openpyxl
from gui.bom_compare_frame import BomCompareFrame

# Synthetic source mirroring the real-world layout:
# col A=Item, B=Part Number, C=Equipment Description, D-N=per-site, O=Spares, P=Total
wb = openpyxl.Workbook()
ws = wb.active
# Header row 7 with site cols, spares col, and total col
ws.cell(7, 1, "Item")
ws.cell(7, 2, "Part No.")
ws.cell(7, 3, "Equipment Description")
ws.cell(7, 4, "ALBA")
ws.cell(7, 5, "ALVY")
ws.cell(7, 6, "BAND")
ws.cell(7, 7, "Spares")
ws.cell(7, 8, "Total")
ws.cell(8, 4, "Qty")
ws.cell(8, 5, "Qty")
ws.cell(8, 6, "Qty")
ws.cell(8, 7, "Qty")
ws.cell(8, 8, "Qty")

# Data row 1: normal part with per-site + spares
ws.cell(10, 1, 1)
ws.cell(10, 2, "3HE12546AA")
ws.cell(10, 3, "SFP - C37.94 LC 2KM SR")
ws.cell(10, 4, 2)  # ALBA=2
ws.cell(10, 5, 3)  # ALVY=3
ws.cell(10, 6, 0)
ws.cell(10, 7, 5)  # Spares=5
ws.cell(10, 8, 10)

# Data row 2: SPARES-ONLY part with description (the bug case)
ws.cell(11, 1, 2)
ws.cell(11, 2, "3HE11286AA")
ws.cell(11, 3, "7250 IXR-R6 fan filter, 5-pack")
ws.cell(11, 4, 0)  # all per-site = 0
ws.cell(11, 5, 0)
ws.cell(11, 6, 0)
ws.cell(11, 7, 5)  # Spares only = 5
ws.cell(11, 8, 5)

# Data row 3: spares-only WITHOUT description (already-broken upstream data)
ws.cell(12, 1, 3)
ws.cell(12, 2, "3HE11279AA")
ws.cell(12, 3, "")  # genuinely blank
ws.cell(12, 4, 0)
ws.cell(12, 5, 0)
ws.cell(12, 6, 0)
ws.cell(12, 7, 5)
ws.cell(12, 8, 5)

sites, parts, spares, _ = BomCompareFrame._parse_per_site_bom(ws)
print("Sites:", sites)
print()
for pn in sorted(set(list(parts.keys()) + list(spares.keys()))):
    pinfo = parts.get(pn, {})
    print(f"  {pn}:")
    print(f"    desc={pinfo.get('desc')!r}")
    print(f"    site_qty={pinfo.get('site_qty', {})}")
    print(f"    spares={spares.get(pn, 0)}")

# Assertions
assert parts["3HE12546AA"]["desc"] == "SFP - C37.94 LC 2KM SR", "normal part desc"
assert spares["3HE12546AA"] == 5, "normal part spares qty"
assert parts.get("3HE11286AA", {}).get("desc") == "7250 IXR-R6 fan filter, 5-pack", (
    "spares-only PN must surface its description"
)
assert spares["3HE11286AA"] == 5, "spares-only PN spares qty"
# Blank-source PN: still in spares; desc stays empty (source data limit).
assert spares.get("3HE11279AA") == 5, "blank-desc spares-only must still register qty"
print()
print("All assertions passed.")
