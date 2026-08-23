"""End-to-end pipeline qty audit across all 4 stages:
   Raw Sales BoM -> Sanitized Sales BoM -> COMPARE (Sales side)
                                     Factory Live -> COMPARE (Factory side)

For every PN that surfaces anywhere in the chain, report:
   raw_qty / sanit_qty / live_qty / cmp_ordered / cmp_live  + delta flags.
"""
from __future__ import annotations
import re
from pathlib import Path
from collections import defaultdict
from openpyxl import load_workbook

RAW   = Path(r"C:/Users/ZackerySimino/AppData/Local/Temp/pipe_raw.xlsx")
SANIT = Path(r"C:/Users/ZackerySimino/AppData/Local/Temp/pipe_sanit.xlsx")
LIVE  = Path(r"C:/Users/ZackerySimino/AppData/Local/Temp/pipe_live.xlsx")
CMP   = Path(r"C:/Users/ZackerySimino/AppData/Local/Temp/pipe_compare.xlsx")

ALIASES = {
    "3HE13584AA":   "3HE11278AA",
    "3KC72037AA":   "3KC72037AAAA",
    "8DG59417AA":   "8DG59417AAXX",
    "8DG59427AA":   "8DG59427AAXX",
    "8DG60207AA":   "8DG60207AAAA",
    "1AD1519300":   "1AD151930001",
    "1AB2151200":   "1AB215120085",
}
INTANGIBLE_KW = ("subscription", "support", "maintenance", "warranty",
                 "training", "installation", "license", " fp", "feature pack")


def norm(s):
    if s is None: return ""
    pn = str(s).split("\n", 1)[0].strip().upper()
    return re.sub(r"^(?:1P|P)", "", pn)


def canon(pn):
    return ALIASES.get(pn, pn)


def qty(v):
    if v in (None, ""): return 0
    if isinstance(v, str):
        v = v.strip().strip('"').strip("'").strip()
    try: return int(float(v))
    except (TypeError, ValueError): return 0


def is_intangible(desc):
    if not desc: return False
    d = desc.lower()
    return any(kw in d for kw in INTANGIBLE_KW)


# ===================== RAW =====================
# Sum per-PN over per-site (5..64) + Spares(65) + Maintenance(66) + Network(67) + License pts(72)
# from TO4 BoM v8, plus CBR v1 per-site (5..29) + License pts (35).
raw_pn: dict[str, dict] = {}
raw_wb = load_workbook(RAW, data_only=True)

def grab_raw(sheet, pn_col, desc_col, hdr_row, per_site_range, bucket_cols):
    ws = raw_wb[sheet]
    for r in range(hdr_row + 1, ws.max_row + 1):
        raw_pn_val = ws.cell(r, pn_col).value
        if not raw_pn_val: continue
        pn = norm(raw_pn_val)
        if not pn or len(pn) < 6: continue
        desc = ws.cell(r, desc_col).value
        desc_s = str(desc).split("\n", 1)[0].strip() if desc else ""
        # Skip banner rows by PN heuristic
        if "MAINTENANCE" in pn or "LICENSE" in pn:
            continue
        ps = sum(qty(ws.cell(r, c).value) for c in per_site_range)
        bk = {label: qty(ws.cell(r, c).value) for c, label in bucket_cols.items()}
        bk_sum = sum(bk.values())
        if ps + bk_sum == 0: continue
        # Skip intangibles by desc
        if is_intangible(desc_s):
            continue
        ck = canon(pn)
        entry = raw_pn.setdefault(ck, {"desc": desc_s, "per_site": 0, "buckets": defaultdict(int)})
        if not entry["desc"]: entry["desc"] = desc_s
        entry["per_site"] += ps
        for label, q in bk.items():
            if q:
                entry["buckets"][label] += q

# NOTE: License Points (col 72 / col 35) and Maintenance (col 66) are
# intentionally NOT counted — the production parser drops them as
# non-order buckets. Only per-site + Spares + Network are real order qty.
if "TO4 BoM v8" in raw_wb.sheetnames:
    grab_raw("TO4 BoM v8", pn_col=2, desc_col=3, hdr_row=11,
             per_site_range=range(5, 65),
             bucket_cols={65: "Spares", 67: "Network"})
if "CBR v1" in raw_wb.sheetnames:
    grab_raw("CBR v1", pn_col=2, desc_col=3, hdr_row=2,
             per_site_range=range(5, 30),
             bucket_cols={})

# Discontinued SKUs dropped everywhere in the COMPARE (CFP2 EOL).
EXCLUDED = {"3HE16718AA", "3HE12518AA"}
for pn in list(raw_pn.keys()):
    if pn in EXCLUDED:
        del raw_pn[pn]


# ===================== SANITIZED =====================
# Sum per-PN from the BOM aggregate "Total Ordered" col D, after row 10.
sanit_pn: dict[str, int] = defaultdict(int)
sanit_desc: dict[str, str] = {}
sanit_wb = load_workbook(SANIT, data_only=True)
ws = sanit_wb["BOM"]
for r in range(10, ws.max_row + 1):
    pn = norm(ws.cell(r, 2).value)
    if not pn: continue
    desc = ws.cell(r, 3).value
    tot = qty(ws.cell(r, 4).value)
    ck = canon(pn)
    sanit_pn[ck] += tot
    if desc and ck not in sanit_desc:
        sanit_desc[ck] = str(desc).split("\n", 1)[0].strip()


# ===================== LIVE =====================
# Per-site detail tabs: each row = 1 physical unit (col D = PN).
live_pn: dict[str, int] = defaultdict(int)
live_desc: dict[str, str] = {}
live_wb = load_workbook(LIVE, data_only=True)
for ws in live_wb.worksheets:
    if ws.title.upper() in ("BOM", "SUMMARY"): continue
    for r in range(15, (ws.max_row or 0) + 1):
        pn_raw = ws.cell(r, 4).value
        if not pn_raw: continue
        pn = norm(pn_raw)
        if not pn: continue
        desc = ws.cell(r, 6).value
        desc_s = str(desc).split("\n", 1)[0].strip() if desc else ""
        if is_intangible(desc_s):  # filter LICENSE / RTU N/A rows
            continue
        ck = canon(pn)
        live_pn[ck] += 1
        if desc_s and ck not in live_desc:
            live_desc[ck] = desc_s


# ===================== COMPARE =====================
cmp_ordered: dict[str, int] = {}
cmp_live: dict[str, int] = {}
cmp_desc: dict[str, str] = {}
cmp_wb = load_workbook(CMP, data_only=True)
ws = cmp_wb["Shortfall"]
for r in range(3, ws.max_row + 1):
    pn = norm(ws.cell(r, 2).value)
    if not pn or pn in ("PART NO", "PART NO.", "ART NO."): continue
    desc = ws.cell(r, 3).value
    to = qty(ws.cell(r, 4).value)
    li = qty(ws.cell(r, 5).value)
    ck = canon(pn)
    cmp_ordered[ck] = to
    cmp_live[ck] = li
    if desc and ck not in cmp_desc:
        cmp_desc[ck] = str(desc).split("\n", 1)[0].strip()


# ===================== REPORT =====================
all_pns = sorted(set(raw_pn) | set(sanit_pn) | set(live_pn) | set(cmp_ordered) | set(cmp_live))

print(f"PN counts: raw={len(raw_pn)}  sanit={len(sanit_pn)}  live={len(live_pn)}  cmp_ordered={sum(1 for v in cmp_ordered.values() if v>0)}  cmp_live={sum(1 for v in cmp_live.values() if v>0)}")
print()
print(f"{'PN':<22} {'desc':<48} | {'RAW':>5} {'SANIT':>5} {'LIVE':>5} | {'cmpORD':>6} {'cmpLIVE':>7} | flag")
print("-" * 130)

# Sort by largest absolute discrepancy first
def discrepancy_key(pn):
    raw_q = raw_pn.get(pn, {}).get("per_site", 0) + sum(raw_pn.get(pn, {}).get("buckets", {}).values())
    sn = sanit_pn.get(pn, 0)
    return -abs(raw_q - sn)

for pn in sorted(all_pns, key=discrepancy_key):
    raw_info = raw_pn.get(pn, {})
    raw_q = raw_info.get("per_site", 0) + sum(raw_info.get("buckets", {}).values())
    sn = sanit_pn.get(pn, 0)
    lv = live_pn.get(pn, 0)
    co = cmp_ordered.get(pn, 0)
    cl = cmp_live.get(pn, 0)
    desc = (raw_info.get("desc") or sanit_desc.get(pn) or live_desc.get(pn) or cmp_desc.get(pn) or "")[:46]
    flags = []
    if raw_q and not sn:
        flags.append("RAW->SANIT DROPPED")
    elif raw_q != sn and raw_q and sn:
        flags.append(f"RAW->SANIT delta={sn-raw_q:+d}")
    if sn != co and sn and co:
        flags.append(f"SANIT->CMP delta={co-sn:+d}")
    elif sn and not co:
        flags.append("SANIT->CMP DROPPED")
    if lv != cl and (lv or cl):
        flags.append(f"LIVE->CMP delta={cl-lv:+d}")
    flag_str = "; ".join(flags) if flags else "ok"
    print(f"{pn:<22} {desc:<48} | {raw_q:>5} {sn:>5} {lv:>5} | {co:>6} {cl:>7} | {flag_str}")
