"""Coverage audit: list every PN ordered in the source Sales BoM vs every
PN that appears on the COMPARE output. Flag drops.

Source Sales BoM:  parts with non-zero qty anywhere (per-site OR spares
                   OR Network/Maintenance/License-points columns).
COMPARE output:    PNs on the Shortfall tab + PNs visible on the Sales
                   BoM reference copy.
"""
from __future__ import annotations
import re
from pathlib import Path
from collections import defaultdict
from openpyxl import load_workbook

SRC = Path(r"C:/Users/ZackerySimino/OneDrive - LightRiver Technologies Inc/Desktop/ATLAS Build Material/TO4-Services-Prices-with-BoM-V9.1-03-05-26_BPA.xlsx")
CMP = Path(r"C:/Users/ZackerySimino/AppData/Local/Temp/audit_compare.xlsx")


def norm_pn(s):
    """Strip newlines, vendor 1P/P prefix, uppercase."""
    if s is None:
        return ""
    pn = str(s).split("\n", 1)[0].strip().upper()
    return re.sub(r"^(?:1P|P)", "", pn)


# ---------- Source: scan TO4 BoM v8 & CBR_bpa version for parts with ANY qty ----------
print("=" * 72)
print(f"SOURCE: {SRC.name}")
print("=" * 72)
src_wb = load_workbook(SRC, data_only=True)

source_parts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
source_descs: dict[str, str] = {}

for sheet_name in ("TO4 BoM v8", "CBR v1", "CBR_bpa version"):
    if sheet_name not in src_wb.sheetnames:
        continue
    ws = src_wb[sheet_name]
    print(f"\n--- {sheet_name} ({ws.max_row}x{ws.max_column}) ---")
    # Sheet-specific column layouts
    if sheet_name == "TO4 BoM v8":
        pn_col, desc_col, hdr_row = 2, 3, 11
        # All qty cols + special cols (Spares=65, Maintenance=66, Network=67,
        # License points=72) — include EVERYTHING in case any non-zero qty
        # would be a real order
        qty_cols = list(range(5, 65)) + [65, 66, 67, 72]
    elif sheet_name == "CBR v1":
        pn_col, desc_col, hdr_row = 2, 3, 2
        qty_cols = list(range(5, 36))  # per-site cols
    else:  # CBR_bpa version
        pn_col, desc_col, hdr_row = 1, 2, 2
        qty_cols = list(range(4, 27))  # cols D..Z

    for r in range(hdr_row + 1, ws.max_row + 1):
        raw_pn = ws.cell(r, pn_col).value
        if not raw_pn:
            continue
        pn = norm_pn(raw_pn)
        if not pn or pn in ("PART NUMBER", "PART NO", "PART NO.", "PART NO #"):
            continue
        # Skip rows that look like section banners (uppercase headings)
        if " " in str(raw_pn) and not any(c.isdigit() for c in str(raw_pn)):
            continue
        desc = ws.cell(r, desc_col).value
        if desc and pn not in source_descs:
            source_descs[pn] = str(desc).split("\n", 1)[0].strip()
        for c in qty_cols:
            qv = ws.cell(r, c).value
            if qv is None or qv == "":
                continue
            # Tolerate quoted strings
            if isinstance(qv, str):
                cleaned = qv.strip().strip('"').strip("'").strip()
                try:
                    q = int(float(cleaned)) if cleaned else 0
                except (TypeError, ValueError):
                    q = 0
            else:
                try:
                    q = int(float(qv))
                except (TypeError, ValueError):
                    q = 0
            if q > 0:
                source_parts[pn][f"{sheet_name}:col{c}"] += q

print(f"\nSource: {len(source_parts)} unique PNs with non-zero qty somewhere")

# ---------- COMPARE: every PN on the Shortfall tab ----------
print("\n" + "=" * 72)
print(f"COMPARE: {CMP.name}")
print("=" * 72)
cmp_wb = load_workbook(CMP, data_only=True)
ws_short = cmp_wb["Shortfall"]
short_pns: set[str] = set()
short_total_ordered: dict[str, int] = {}
short_live: dict[str, int] = {}
for r in range(3, ws_short.max_row + 1):
    pn = norm_pn(ws_short.cell(r, 2).value)
    if not pn or pn in ("PART NO", "PART NO."):
        continue
    short_pns.add(pn)
    to = ws_short.cell(r, 4).value
    li = ws_short.cell(r, 5).value
    short_total_ordered[pn] = to or 0
    short_live[pn] = li or 0

print(f"COMPARE Shortfall: {len(short_pns)} unique PNs")

# ---------- Coverage report ----------
print("\n" + "=" * 72)
print("COVERAGE: source PNs NOT on COMPARE Shortfall")
print("=" * 72)
missing = sorted(p for p in source_parts if p not in short_pns)
if not missing:
    print("  (all source PNs present)")
else:
    for pn in missing:
        desc = source_descs.get(pn, "")
        breakdown = dict(source_parts[pn])
        total = sum(breakdown.values())
        print(f"  {pn:<22}  qty_total={total:<5}  desc={desc[:50]!r}")
        # Limit detail to avoid noise
        for src_col, q in list(breakdown.items())[:3]:
            print(f"      {src_col}: {q}")

print("\n" + "=" * 72)
print("COVERAGE: COMPARE Shortfall PNs NOT in source (only Factory has)")
print("=" * 72)
extra = sorted(p for p in short_pns if p not in source_parts)
if not extra:
    print("  (all Shortfall PNs are in source)")
else:
    for pn in extra:
        to = short_total_ordered.get(pn, 0)
        li = short_live.get(pn, 0)
        print(f"  {pn:<22}  Total Ordered={to:<5}  Live={li}")

print("\n" + "=" * 72)
print("SUMMARY")
print("=" * 72)
print(f"  Source PNs:              {len(source_parts)}")
print(f"  COMPARE Shortfall PNs:   {len(short_pns)}")
print(f"  Common PNs:              {len(set(source_parts) & short_pns)}")
print(f"  In source but not COMPARE: {len(missing)}")
print(f"  In COMPARE but not source: {len(extra)}")
