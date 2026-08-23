"""Rebuild the Sales BoM end-to-end against the original source so we can
inspect whether the output Spares tab now carries descriptions."""
import sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")

from openpyxl import load_workbook
from gui.workbook_builder import WorkbookBuilder

SRC = r"C:/Users/ZackerySimino/OneDrive - LightRiver Technologies Inc/Desktop/ATLAS Build Material/TO4-Services-Prices-with-BoM-V9.1-03-05-26_BPA.xlsx"
OUT = str(Path(tempfile.gettempdir()) / "rebuilt_sales_bom.xlsx")

builder = WorkbookBuilder(
    db_cache=None,
    template_path=str(ROOT / "data" / "Device_Report_Template.xlsx"),
    packing_slip_template=str(ROOT / "data" / "ATLAS_Packing_Slip.xlsx"),
)

# Try a likely sheet name
for sheet in ("TO4 BoM v8", "CBR_bpa version", "CBR v1"):
    try:
        print(f"\n========== Trying sheet: {sheet} ==========")
        out_path = builder.build_sales_bom_packing_slip_workbook(
            source_path=SRC,
            selected_sheets=[sheet],
            output_file=OUT,
            customer="BPA",
            project="TO4",
        )
        wb = load_workbook(out_path, data_only=True)
        if "Spares" not in wb.sheetnames:
            print(f"  No Spares tab; skipping")
            continue
        ws = wb["Spares"]
        print(f"  Spares tab dims {ws.max_row}x{ws.max_column}")
        # Show all rows including ones with previously-blank desc
        TARGETS = ("3HE11279AA", "3HE11286AA", "3HE11287AA", "3HE11288AA", "3HE03127AA")
        for r in range(14, ws.max_row+1):
            pn = ws.cell(r, 2).value
            desc = ws.cell(r, 3).value
            qty = ws.cell(r, 4).value
            if pn and any(t in str(pn) for t in TARGETS):
                print(f"  Spares r{r}: PN={pn!r}  desc={desc!r}  qty={qty}")
        # Also check BOM aggregate for 3HE03127AA
        ws_b = wb["BOM"]
        for r in range(10, ws_b.max_row+1):
            pn = ws_b.cell(r, 2).value
            if pn == "3HE03127AA":
                # show non-zero cells across the row
                row_data = [(c, ws_b.cell(r, c).value) for c in range(1, ws_b.max_column+1)]
                row_data = [(c, v) for c, v in row_data if v not in (None, "", 0)]
                print(f"  BOM r{r} 3HE03127AA: {row_data}")
                break
        break  # found one that worked
    except Exception as exc:
        print(f"  failed: {exc}")
