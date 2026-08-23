"""End-to-end smoke test: run BomCompareFrame._compare against the real
Testing/ files and dump the Shortfall tab to verify the recalculation."""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")

from openpyxl import load_workbook
from gui.bom_compare_frame import BomCompareFrame
from gui.workbook_builder import WorkbookBuilder

SALES = r"C:/Users/ZackerySimino/AppData/Local/Temp/pipe_sanit.xlsx"
LIVE  = r"C:/Users/ZackerySimino/AppData/Local/Temp/pipe_live.xlsx"

builder = WorkbookBuilder(
    db_cache=None,
    template_path=str(ROOT / "data" / "Device_Report_Template.xlsx"),
    packing_slip_template=str(ROOT / "data" / "ATLAS_Packing_Slip.xlsx"),
)

frame = BomCompareFrame.__new__(BomCompareFrame)
frame.gui = type("g", (), {"workbook_builder": builder})()
frame._append_log = lambda msg: print(f"[LOG] {msg}")
frame._set_status = lambda msg: None
frame.after = lambda *a, **k: None

out = frame._compare(LIVE, SALES)
print(f"\n>>> Wrote: {out}\n")
wb = load_workbook(out, data_only=True)
ws = wb["Shortfall"]
print(f"Shortfall: {ws.max_row} rows")
for r in range(3, min(ws.max_row, 80)+1):
    cells = []
    for c in range(1, 7):
        v = ws.cell(r, c).value
        cells.append("" if v is None else str(v))
    if any(cells):
        print(f"  r{r:>3}: " + " | ".join(f"{x:<35}" if c==2 else f"{x:<8}" for c,x in enumerate(cells, start=1)))
